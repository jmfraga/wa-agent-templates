# wa-agent-templates

Templates open-source para construir **agentes conversacionales sobre WhatsApp** con [@whiskeysockets/baileys](https://github.com/WhiskeySockets/Baileys) + [Anthropic Claude](https://docs.anthropic.com/).

Dos patrones, dos directorios:

| Template | Para qué | Estado |
|---|---|---|
| **[`groups/`](./groups)** | Agente que **participa en grupos** de WhatsApp con SOUL específico por grupo + knowledge bases compartidas | ✅ Funcional end-to-end |
| **[`one-on-one/`](./one-on-one)** | Agente que mantiene **conversaciones 1:1** con contactos (tipo "assistant personal", soporte clientes, etc.) | 📝 Design doc + scaffolding (en construcción) |

Los dos comparten arquitectura: brain Python (FastAPI + Anthropic SDK + SQLAlchemy) + wa-listener Node (Baileys) + UI admin (FastAPI + HTMX + Tailwind). Solo cambia el modelo de datos y el gating.

## Características de los templates

### Comunes
- 🤖 **Brain con tool-use** de Anthropic (Haiku 4.5 / Sonnet 4.6 configurable).
- 💬 **WhatsApp via Baileys** con sesión persistente, soporte de LIDs (privacy IDs), QR pair desde la UI.
- 📚 **Knowledge bases versionadas** con facts CRUD y `pending_review` para aprendizaje automático.
- 🛡️ **Detector de crisis** Sonnet 4.6 con 7 categorías + notificación DM al owner.
- 🎛️ **UI admin** con dashboard, editor de SOULs, CRUD de KBs, audit log, settings.
- 🔒 Bind por defecto a `127.0.0.1` excepto la UI (expuesta opcionalmente por Tailscale).
- 🧰 **Slash commands** por DM del owner: `/lanza`, `/publica`, `/cancela`, `/edita`, `/drafts` (groups template).

### Solo en `groups/`
- 🎭 **SOUL por grupo** (versionado).
- 🚦 **Tres modos de gating** por grupo: `lurker` (responde si lo mencionan), `proactive` (clasificador Haiku decide intervenir + cooldown), `on_command_only`.
- 🚀 **Discussion launcher**: el owner puede pedir desde DM que Phoenix lance una discusión en un grupo específico.
- 🔧 **Tools owner-only de auto-configuración**: el agente puede ajustar su propio SOUL, crear KBs y suscribirlas en respuesta a comandos directos del owner.

### Solo en `one-on-one/` (planeado)
- 💼 **Threads** por contacto.
- 🎫 **Tickets** con handoff al owner para temas que requieren intervención humana.
- 👥 **Tipos de contacto** (owner, regular, prospect, etc.) con tono adaptado.

## Quickstart

```bash
git clone https://github.com/jmfraga/wa-agent-templates.git
cd wa-agent-templates/groups   # o one-on-one (cuando esté listo)
cp .env.example .env           # completa ANTHROPIC_API_KEY
make brain-install ui-install wa-install
make db-init
make dev
# abre http://localhost:8101/setup y escanea el QR con WhatsApp
```

Detalles completos en el README de cada template.

## Origen

Estos templates son la abstracción genérica de dos agentes en producción:

- **groups/** ← inspirado en **Phoenix**, un agente que vive en ~10 grupos de WhatsApp del equipo del autor.
- **one-on-one/** ← inspirado en **Iris**, asistente WhatsApp 1:1 para coordinación con contactos.

Los repos originales son privados; este repo es la versión abstracta, sin datos personales, lista para que clones, configures y despliegues tu propio agente.

## Stack

- **Brain**: Python 3.11+, FastAPI, Anthropic SDK, SQLAlchemy, Pydantic Settings.
- **wa-listener**: Node.js 20+, TypeScript, Baileys 6.7.x estable, Express.
- **UI**: FastAPI + Jinja2 + HTMX 2 + Tailwind CSS (CDN).
- **DB**: SQLite (default) o Postgres (opcional, via `psycopg`).
- **Modelos**: Haiku 4.5 default, Sonnet 4.6 para safety/proactive crítico.

## License

MIT. Ver [LICENSE](./LICENSE).

## Contribuciones

Issues y PRs bienvenidos. Por favor mantén los templates **libres de datos personales** — el patrón es genérico y debe seguirlo siendo.
