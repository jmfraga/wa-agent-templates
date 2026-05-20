# Phoenix v2

Agente WhatsApp **de grupos** con SOUL por grupo y knowledge bases compartidas.

- Repo: `jmfraga/phoenix` (privado)
- Host destino: RPi5 <ECOSYSTEM> (`<HOST_IP>`)
- Número WA: +521XXXXXXXXXX (cutover S5)
- Plan completo: `~/.claude/plans/ya-viene-el-momento-structured-jellyfish.md`

## Arquitectura
```
WhatsApp (groups + DM owner)
   │
   ▼
wa-listener (Node+Baileys :8100)  ⇄  brain (FastAPI :8102)  ⇄  SQLite/Postgres
        ▲                                   ↓
        │                            Anthropic (Haiku/Sonnet)
        │
        └───────────  ui (FastAPI+HTMX :8101)
                       /         · dashboard
                       /setup    · pair QR vía SSE
                       /groups   · editar modo/SOUL/KBs
                       /kbs      · CRUD facts
                       /audit    · log de decisiones
```

## Estado actual
- ✅ Brain con `/chat`, `/groups`, `/kbs`, `/audit`, prompt caching SOUL+KBs.
- ✅ Listener filtra grupos + DM owner; endpoints pair (`/wa/state`, `/wa/pair`, `/wa/qr-stream` SSE, `/wa/logout`).
- ✅ 3 modos gating: `lurker`, `proactive` (clasificador Haiku 4.5 + cooldown + audit), `on_command_only`.
- ✅ SOULs versionados en DB, recargables con cache 60s.
- ✅ KBs N:M con priority. Tools: `lookup_kb_fact`, `list_kb_facts`, `remember_fact` (con `pending_review`).
- ✅ Discussion launcher: slash commands DM owner (`/lanza`, `/publica`, `/cancela`, `/edita`, `/drafts`).
- ✅ Detector de crisis Sonnet 4.6 con 7 categorías + DM al owner.
- ✅ UI admin completa: dashboard, pair-QR-via-UI, edición grupos/SOULs/KBs/facts, audit.

## Quickstart local (M4)

```bash
cp .env.example .env  # completa ANTHROPIC_API_KEY
make brain-install
make db-init           # crea SQLite + seed SOUL default + grupo demo
make brain-dev         # :8102
# En otra terminal:
make wa-install
make wa-dev            # imprime QR; escanéalo con un WhatsApp sandbox
```

Smoke test sin WhatsApp:
```bash
curl -s http://localhost:8102/health | jq
curl -s -X POST http://localhost:8102/chat \
  -H 'content-type: application/json' \
  -d '{"group_jid":"demo@g.us","contact_jid":"+521XXXXXXXXXX@s.whatsapp.net","contact_name":"Test","text":"@phoenix hola","mentions_phoenix":true}' | jq
```

## Deploy a RPi5 (cuando estés listo)
Ver `docs/deploy-rpi5.md`. Pasos resumidos:

1. `scripts/deploy-rpi5.sh` hace `rsync` excluyendo `auth/`, `.venv/`, `node_modules/`, `*.db`.
2. En la Pi: `cd ~/phoenix && make brain-install wa-install && make db-init`.
3. Copia units: `sudo cp deploy/systemd/*.service /etc/systemd/system/`.
4. `systemctl --user enable --now phoenix-brain phoenix-listener`.
5. Escanea QR en `journalctl --user -fu phoenix-listener` (una sola vez; sesión persiste en `auth/listener/`).
6. Agrega Phoenix a tus grupos. Edita SOULs por grupo con `scripts/admin.py` (o cuando S3 esté, vía UI).

## Estructura
- `brain/` — FastAPI + Anthropic SDK (:8102)
- `wa-listener/` — Node+Baileys (:8100, HTTP `/wa/*` y `/post-to-*`)
- `ui/` — FastAPI+Jinja+HTMX (:8101, admin)
- `db/` — schemas / migraciones futuras Alembic
- `scripts/` — deploy, admin CLI helpers
- `deploy/systemd/` — units (`phoenix-brain`, `phoenix-listener`, `phoenix-ui`)
- `docs/` — SOUL template, arquitectura, pair-via-UI, deploy
