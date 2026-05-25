# `one-on-one`

WhatsApp assistant template for **direct 1:1 conversations** with contacts: clients, leads, patients, colleagues. The assistant ("Iris" by default — rename in `SOUL.md`) acts as a warm bridge between the owner and the people who message them: it answers FAQs from a knowledge base, opens tickets for decisions only the owner can make, escalates urgencies, and can also send outbound messages on behalf of the owner via a 4-step confirmed agentic loop.

**Estado:** Funcional end-to-end. Brain + listener + relay-bot + admin UI corren juntos vía `make dev`. Postgres + Alembic migrations. Tests con pytest.

## Diferencias clave vs `groups/`

| Aspecto | `groups/` | `one-on-one/` |
|---|---|---|
| Unidad de sesión | grupo (`@g.us`) | contacto (`@s.whatsapp.net`) |
| SOUL | uno por grupo | global (`SOUL.md`) + ajustes por `kind` |
| Mention detection | sí | N/A — todo DM es para el agente |
| Tickets | no | sí (handoff al owner vía Telegram) |
| Owner channel | igual WA + slash commands DM | Telegram bot dedicado con botones inline |
| Agentic outbound | propuesta de discusión | 4-step plan/confirm/send/track |
| Crisis detector | igual | igual |

## Quickstart local

```bash
# 1. Clona y entra al template
git clone <this-repo>
cd one-on-one
cp .env.example .env             # completa ANTHROPIC_API_KEY y, si quieres relay, TELEGRAM_BOT_TOKEN
cp brain/.env.example brain/.env
cp wa-listener/.env.example wa-listener/.env
cp relay-bot/.env.example relay-bot/.env
cp ui/.env.example ui/.env

# 2. Postgres (Docker)
docker run --name iris-pg -e POSTGRES_USER=iris -e POSTGRES_PASSWORD=iris \
  -e POSTGRES_DB=iris -p 5432:5432 -d postgres:16

# 3. Migrations + KB seed (opcional)
make db-migrate
psql -h localhost -U iris -d iris -f db/seed/kb-example.sql

# 4. Tu SOUL
cp SOUL-default.md brain/SOUL.md
# edita brain/SOUL.md y reemplaza {{PLACEHOLDERS}} con tus datos reales

# 5. Levanta todo
make brain-install ui-install wa-install relay-install
make dev                        # arranca brain :8096, listener :8099, relay :8098, ui :8097
# escanea QR de wa-listener con un número SANDBOX antes de usar el de producción
```

Smoke test sin WhatsApp:

```bash
curl -s http://localhost:8096/health | jq
curl -s -X POST http://localhost:8096/chat \
  -H 'content-type: application/json' \
  -d '{"phone":"+15550001234","name":"Test","text":"hola, ¿cuándo es el próximo workshop?"}' | jq
```

## Estructura

```
one-on-one/
├── README.md
├── Makefile
├── SOUL-default.md         template con placeholders {{ASSISTANT_NAME}}, etc.
├── .env.example
├── brain/                  FastAPI :8096 — clasifica intent, llama Anthropic, tools
│   ├── pyproject.toml
│   ├── tests/              pytest harness
│   └── src/iris_brain/
│       ├── server.py       endpoints (/chat, /owner/instruct, /tickets, ...)
│       ├── chat.py         pipeline handle_message
│       ├── safety.py       detector de crisis (7 categorías)
│       ├── intents.py      clasificador Haiku
│       ├── tools.py        Anthropic tool definitions
│       ├── agentic.py      outbound loop (search → plan → confirm → send)
│       ├── soul.py         carga SOUL.md con cache + cache_control
│       ├── relay.py        cliente HTTP del relay-bot
│       ├── sessions.py     ventana de historial por thread
│       ├── models.py       SQLAlchemy ORM
│       ├── config.py       pydantic Settings
│       ├── db.py           engine + session factory
│       └── admin.py        rotación de API keys, regeneración de tokens
├── ui/                     FastAPI + Jinja2 + HTMX :8097
│   ├── pyproject.toml
│   └── src/iris_ui/
│       ├── server.py
│       ├── admin_routes.py
│       ├── brain_client.py
│       └── templates/      dashboard, contacts, tickets, KBs, SOUL editor, settings
├── wa-listener/            Node + Baileys :8099
│   ├── package.json
│   └── src/
│       ├── index.ts        QR pair + inbound → POST /chat
│       ├── relay-bot.ts    bridge (legacy WA #2 mode)
│       └── types.ts
├── relay-bot/              Python + python-telegram-bot + FastAPI :8098
│   └── src/iris_relay/
│       ├── server.py       /send-to-owner endpoint
│       ├── telegram.py     bot handlers (botones inline, /tickets, /help, libre)
│       ├── templates.py    renderizado de mensajes
│       ├── state.py        persistencia local de mapping
│       └── config.py
├── tester/                 harness de escenarios YAML
│   └── scenarios/
├── db/
│   ├── alembic.ini
│   ├── alembic/            migrations 0001-0007
│   └── seed/kb-example.sql
├── deploy/
│   ├── systemd/            iris-brain, iris-ui, iris-wa-listener, iris-relay
│   └── iris-watchdog.sh    cron-friendly health-check con alertas Telegram
└── docs/
    └── architecture.md
```

## Pipeline `/chat`

1. **Resolve contact** por `phone`. Auto-register con `kind="regular"` si es nuevo.
2. **Gating según `kind`**: `owner` siempre responde; cualquier otro pasa por safety+intent.
3. **Safety: detector de crisis** (`safety.py`) — 7 categorías (suicida, crisis_emocional, emergencia_medica, violencia, trauma, hemorragia, terceros). Match high/medium → mensaje template fijo + ticket urgente, sin LLM.
4. **Slash commands** (opcionales): `/tickets`, `/draft`, etc. desde Telegram via relay-bot.
5. **Intent classifier**: Haiku 4.5 con prompt cacheado clasifica entre intents (`info_curso`, `consulta_cita`, `seguimiento`, `pago_facturacion`, `saludo_smalltalk`, `otro`, etc.).
6. **LLM loop**: SOUL + KBs + history del thread. Tool use con varios tools.
7. **Ticket flow**: si el agente abre ticket, dispara `POST /send-to-owner` al relay-bot → Telegram al owner. Owner responde → relay-bot hace `POST /owner/reply` → brain redacta versión final → wa-listener entrega al contacto.

## Tools disponibles

- `lookup_kb_fact(kb_slug, key)` — busca un dato concreto en la KB.
- `list_kb_facts(kb_slug?)` — lista keys disponibles.
- `lookup_contact(phone)` — ficha del contacto.
- `update_contact(phone, name?, kind?, notes_append?, notes_replace?)` — guarda lo aprendido.
- `open_ticket(thread_id, kind, summary, draft_for_owner?)` — escala al owner.
- `search_contacts(query, kind?, limit?)` — fuzzy search del directorio.
- `create_task(owner_id, kind, summary, target_contact_ids, expected_names, ...)` — plan agéntico.
- `send_outbound(task_id, target_id, body)` — envía después de confirmación.
- `report_to_owner(text)` — DM Telegram al owner.
- `update_task_status(task_id, status, reason?)`.
- `list_active_tasks()`.
- `remember_fact(kb_slug, key, value)` — propone nuevo fact (queda `pending_review`).

## Modelo de datos (Postgres)

- `contacts(id, phone, name, kind, notes, first_seen, last_seen)`
- `threads(id, contact_id, status, opened_at, closed_at)`
- `messages(id, thread_id, direction, body, ts, model_used, tokens_in, tokens_out, intent)`
- `tickets(id, thread_id, kind, summary, status, owner_response, created_at, updated_at)`
- `kb_facts(id, kb_slug, key, value, source, version, pending_review, created_at)`
- `tasks(id, owner_id, kind, summary, raw_instruction, status, created_at)` + `task_targets(task_id, contact_id, target_id, body, status)`
- `audit_log` (decisiones del clasificador, errores, escalaciones)
- `app_config(key, value)` (runtime settings editables desde la UI)

Migrations versionadas en `db/alembic/versions/0001-0007*.py`.

## Customization

- **Renombra el asistente**: edita `SOUL.md`. El código sigue refiriéndose al package como `iris_*` — esto es solo el package name, no aparece al usuario final.
- **Cambia las categorías `kind`**: edita `brain/src/iris_brain/models.py` (enum) y los templates de UI.
- **Define tus KBs**: usa la UI `/admin/kbs` o `psql` directo con tus inserts. Convención típica: un slug por producto/servicio + `_global`.
- **Crisis hotlines**: edita `brain/src/iris_brain/safety.py` — busca `<crisis hotline — TODO configure for your country>` y reemplaza con los números locales.
- **Modelo**: cambia `IRIS_BRAIN_MODEL_DEFAULT` / `IRIS_BRAIN_MODEL_SAFETY` en `.env`.

## Deploy

Cuatro systemd units en `deploy/systemd/`:

- `iris-brain.service` — FastAPI :8096
- `iris-wa-listener.service` — Node Baileys :8099
- `iris-relay.service` — Telegram bot :8098
- `iris-ui.service` — admin :8097

Más un watchdog en `deploy/iris-watchdog.sh` (cron cada 5 min, alerta vía Telegram cuando algún componente cae o se recupera).

Pasos típicos en una RPi5 / VPS:

```bash
# en tu máquina:
rsync -av --exclude='auth/' --exclude='.venv/' --exclude='node_modules/' \
  --exclude='*.db' --exclude='.env' \
  ./ user@host:/opt/iris/

# en el host:
cd /opt/iris
make brain-install ui-install wa-install relay-install
make db-migrate
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iris-brain iris-wa-listener iris-relay iris-ui
sudo journalctl -fu iris-wa-listener   # escanea QR la primera vez
```

## Configurar dominios de ingest (KB + media)

Las features de ingest desde URL (KB landing scrape y media outbound) usan whitelists
estrictas para evitar SSRF. Por defecto están en `example.com`:

- `brain/src/iris_brain/kb_ingest.py` → `WHITELIST_DOMAINS` (para `/courses` → "Procesar URL").
- `brain/src/iris_brain/config.py` → `MEDIA_WHITELIST_DOMAINS` (para `/admin/media` → "Ingest URL").

Cambia esos conjuntos a tus propios subdominios (`info.tudominio.com`, `blog.tudominio.com`,
`marketing.tudominio.com`, etc.) **antes** de usar estas features en producción. Los
placeholders en los templates HTML también referencian `example.com`; ajústalos si quieres
que tu equipo vea ejemplos reales.

## Origen

Este template fue derivado de un proyecto privado más amplio ("Iris") usado en producción real. Todo dato personal, número de teléfono, email, host IP y nombre real fue anonimizado o reemplazado con placeholders genéricos antes de la publicación. Si encuentras alguna referencia residual, abre un issue.

## License

MIT.
