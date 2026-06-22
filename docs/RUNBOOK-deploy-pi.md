# Runbook genérico — Deploy & Rollback de un agente WhatsApp standalone en un host de producción

Patrón para agentes tipo Phoenix/Iris (brain FastAPI + wa-listener Baileys + UI) corriendo en un
host de producción (p. ej. un RPi5 o mini-PC) con systemd, desplegados **vía git** desde una máquina
de desarrollo.

## Principio: el host de producción es un repo git
El directorio desplegado en producción **es un clon git** que trackea el repo de desarrollo (`origin`)
rama `main`. Así obtienes:
- **Versión** del código corriendo (`git rev-parse HEAD` / tags).
- **Rollback limpio** (`git checkout <tag>`).
- **Detección de drift** (`git status` / `git diff origin/main`) — saber si alguien editó en prod.

`origin` puede ser GitHub (con deploy key) o la propia máquina de dev por SSH si está siempre disponible.

## Nunca versionar ni pisar (debe estar en `.gitignore`)
- `auth/` — estado de sesión de Baileys (credenciales WhatsApp). Pierdes esto = re-escanear QR.
- `.env`, secretos.
- Bases de datos: `*.db` (SQLite) o el volumen de Postgres/docker (vive **fuera** del repo).
- `.venv/`, `node_modules/`, `logs/`.

Verifica con `git check-ignore -v auth/ .env` que git efectivamente los ignora.

## Adoptar git sin destruir (one-time, in-place sobre un deploy existente)
```bash
cd <DIR_DESPLEGADO>
git init && git remote add origin <ORIGIN_URL>
git fetch origin
git reset --mixed origin/main    # NO toca el working tree; revela drift en `git status`
git branch -M main && git branch --set-upstream-to=origin/main main
git tag deployed-$(date +%F)     # punto de rollback
```
Si `git status` muestra cambios = drift real (código tocado en prod). Presérvalo en una rama
`git checkout -b pi-baseline-$(date +%F) && git add -A && git commit -m "snapshot"` **antes** de alinear.

## Deploy
```bash
# En dev: commit (push opcional), working tree limpio.
# En el host: fetch + reset --hard origin/main + rebuild si cambian deps + restart soft + health.
./scripts/deploy-pi.sh
```
Reglas: rebuild de venv/npm **solo si** cambió `pyproject.toml`/`uv.lock`/`package.json`.
Restart **soft** (`systemctl [--user] restart …`), nunca kill sucio. Aplica migraciones de DB
**antes** de dar por bueno el restart. Verifica `curl :<PORT>/health`.

## Rollback
```bash
git fetch -q origin --tags && git tag           # ver puntos disponibles
./scripts/deploy-pi.sh <tag-o-sha>              # volver atrás (HEAD detached, main intacto)
./scripts/deploy-pi.sh                          # volver a main cuando esté el fix
```
Un rollback de **código** no revierte la **DB**: las migraciones se revierten aparte (alembic downgrade).

## Detectar drift periódicamente
```bash
git fetch -q origin && git status --porcelain && git diff origin/main --stat
```
Vacío = sano. Cualquier salida = reconciliar a dev antes de seguir desplegando.
