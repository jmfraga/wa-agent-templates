# Phoenix v2 — Arquitectura

```
┌─────────────────────────────────────────────────────────────────┐
│ WhatsApp (red Meta)                                             │
│   - Grupos en los que Phoenix participa                         │
│   - DM directo de <OWNER> (owner) a Phoenix                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                  pair persistente (Baileys)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ wa-listener  (Node + Baileys + Express)            :8100        │
│  - messages.upsert → filtra @g.us + owner DM                    │
│  - extrae texto, mediaHint, mentions, quotedMsg                 │
│  - POST /chat al brain                                          │
│  - HTTP POST /post-to-group (para outbound del brain)           │
└──────┬─────────────────────────────────────────────────▲────────┘
       │ POST /chat                                       │ POST /post-to-group
       ▼                                                  │
┌─────────────────────────────────────────────────────────┴───────┐
│ phoenix-brain  (FastAPI + Anthropic SDK + SQLAlchemy)  :8102    │
│  - /chat: gating → SOUL load → Anthropic → persist               │
│  - /groups, /health, /soul/reload                                │
│  - Stub: /group/start-discussion (S4)                            │
└──────┬──────────────────────────────────────────────────────────┘
       │
       ├── Anthropic Messages API (Haiku 4.5 default, Sonnet 4.6 safety)
       │   con prompt caching del SOUL
       └── SQLite (dev) / Postgres (prod)
```

## Decisiones técnicas

### SOUL en DB, no en disco
Cada grupo tiene su SOUL versionado (`group_souls.is_active`). Razón: permitirá UI
(S3) editar SOULs sin tocar archivos en la Pi. Cache 60s en memoria por grupo.

### Sesiones por grupo
La unidad de "historia conversacional" es el `group_id`, no el contacto. Window
`PHOENIX_HISTORY_WINDOW` (default 30). El nombre del autor se prefija en cada
mensaje `in` para que el modelo distinga quién dijo qué.

### Gating
Tres modos:
- **lurker**: responde si lo mencionan, hacen reply a Phoenix, o si el owner habla.
- **on_command_only**: responde sólo si el owner escribe `/phoenix ...` o `@phoenix ...`.
- **proactive**: por ahora fallback a lurker. En S4: clasificador Sonnet de
  relevancia + cooldown por grupo + audit_log.

### Auto-registro de grupos
La primera vez que llega un mensaje de un grupo desconocido, el brain lo registra
en modo `lurker` con `display_name = jid.split('@')[0]`. Lo personalizas luego con
`make admin -- mode` y `set-soul`.

### Prompt caching
El SOUL se manda como un bloque `system` con `cache_control: {type: "ephemeral"}`.
Esperado: turno 1 cache_creation; turnos siguientes (mismo SOUL) cache_read.
Refuerza el patrón medido en canela-brain (~$0.0036/turno con cache hit).

## Puertos

| Puerto | Servicio | Bind | Notas |
|--------|----------|------|-------|
| 8102   | phoenix-brain | 127.0.0.1 | sólo local, llamadas internas UI/listener |
| 8100   | phoenix-listener HTTP | 127.0.0.1 | sólo local (brain/UI lo consultan en localhost) |
| 8101   | phoenix-ui | 0.0.0.0 | expuesto por Tailscale, accesible en `<HOST_IP>:8101` |

Sin conflicto con: <ECOSYSTEM> gateway 18789, oc-hub 8080, Iris brain 8096,
Iris UI 8097, Iris relay-bot 8098.

## Lo que NO está implementado todavía

- KBs (tablas existen, sin recall ni CRUD).
- Modo proactive real (clasificador).
- Discussion starters end-to-end (endpoint stub pendiente).
- Detector de crisis (port desde HC Sprint 1).
- UI admin (S3) — por ahora `scripts/admin.py` / `make admin`.
- Audit log (tabla existe, sin escrituras automáticas).

Cada uno tiene TODO comentado en el código correspondiente.
