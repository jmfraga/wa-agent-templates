# Architecture — `one-on-one`

A WhatsApp assistant for **direct 1:1 conversations** with contacts (clients,
leads, colleagues, family). The assistant ("Iris" by default — rename it in
`SOUL.md`) acts as a warm, concise bridge between the owner and the people
who message them, escalating real decisions back to the owner via Telegram.

## End-to-end flow

```
                          +-----------------------------+
                          |        Anthropic API        |
                          | Haiku 4.5 (default)         |
                          | Sonnet 4.6 (safety paths)   |
                          +--------------+--------------+
                                         ^
                                         |  HTTPS
                                         |
+-------------+      +------------+      |     +-----------------+
|   Contact   | ---> | wa-listener| ---> +---> |   iris-brain    |
|  (WhatsApp) | <--- |  (Baileys) | <--- +<--- |  (FastAPI)      |
+-------------+      |   :8099    |            |   :8096         |
                     +-----+------+            +---+----+--------+
                           ^                       |    |
                           |                       |    | SQL
                           | owner reply           |    v
                           |                       | +------------+
                     +-----+------+                | | Postgres   |
                     | relay-bot  | <--------------+ | (8 tables) |
                     | (Telegram) |    open_ticket   +------------+
                     +-----+------+
                           |
                           v
                  +----------------------+
                  | Owner (Telegram DM)  |
                  +----------------------+

                  +---------------------+
                  | iris-ui (admin)     |
                  | FastAPI + HTMX      |
                  | :8097               |
                  +----------+----------+
                             |
                             v
                  +---------------------+
                  | iris-brain :8096    |
                  +---------------------+
```

## Components

| Component | Stack | Port | Role |
|---|---|---|---|
| `wa-listener` | Node + Baileys + TypeScript | `:8099` | Single WhatsApp session. Receives inbound messages, posts to brain, sends replies. Auth state in `./auth/`. |
| `relay-bot` | Python + FastAPI + Telegram Bot API | `:8098` | Owner channel. Sends ticket notifications to owner via Telegram with inline buttons. Webhook back to brain `/owner/reply`. |
| `iris-brain` | Python 3.12 + FastAPI + Anthropic SDK | `:8096` | Classifies intent, runs the safety detector, calls Haiku/Sonnet with tools, persists state. |
| `postgres` | Postgres 16 | `:5432` | Durable persistence: contacts, threads, messages, tickets, kb_facts, intents_log, tasks. |
| `iris-ui` | FastAPI + Jinja2 + HTMX + Tailwind | `:8097` | Admin UI: directory, tickets, KB editing, settings. |

## Communication model

- **Contact → Iris:** WA message → `wa-listener` → `POST /chat` → `iris-brain` → reply text → `wa-listener` sends to contact.
- **Iris → Owner (ticket):** brain opens ticket → calls `relay-bot` webhook → Telegram DM to owner with inline buttons.
- **Owner → Contact (via Iris):** owner replies in Telegram → `relay-bot` posts `/owner/reply` to brain → brain rephrases in Iris's voice → `wa-listener` delivers to contact.
- **Agentic outbound:** owner instructs Iris (free-form Telegram) → brain runs the 4-step agentic loop (search, plan, confirm, send).

## Pinned decisions

- Default model: `claude-haiku-4-5-20251001`. Safety paths: `claude-sonnet-4-6`.
- Persistence: Postgres in production, SQLite acceptable in local dev.
- Scope v1: 1:1 conversations + tickets + kb facts + agentic outbound.
- Hard rules: never schedule, never quote final prices, never give clinical opinions — always bridge to the owner.

## Environments

- **Dev (laptop):** all services local, Postgres in Docker, `wa-listener` paired against a sandbox WhatsApp number.
- **Prod:** an always-on host (RPi5, VPS, etc.) running the four systemd units in `deploy/systemd/`.

See the project root `README.md` for the quickstart.
