# Deploy Phoenix v2 a RPi5 <ECOSYSTEM>

Host: `<user>@<HOST_IP>` (<pi-host>).

> **Nota cutover**: el número actual de Phoenix en <ECOSYSTEM> es +521XXXXXXXXXX. Mientras pruebas, pair contra un número *sandbox* primero. Sólo cuando confirmes que Phoenix v2 funciona bien en grupos de prueba, paras Phoenix en <ECOSYSTEM> (`openclaw agents stop phoenix`) y re-pair con el número real.

## 1. Pre-requisitos en la Pi

```bash
ssh <user>@<HOST_IP>

# uv (si no está)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Node 20+
node -v   # debería ser >=20

# (Opcional para migrar de SQLite a Postgres luego)
docker ps  # si quieres reusar el container de iris
```

## 2. Primer deploy

Desde el M4:
```bash
cd ~/Projects/phoenix
./scripts/deploy-rpi5.sh
```

En la Pi:
```bash
cd ~/phoenix
mkdir -p logs
cp .env.example .env                   # completa ANTHROPIC_API_KEY
cp brain/.env.example brain/.env
cp wa-listener/.env.example wa-listener/.env

# Brain
cd brain && uv venv && source .venv/bin/activate && uv pip install -e .
python -m phoenix_brain.db_init        # crea SQLite + seed 3 grupos demo

# UI
cd ../ui && uv venv && source .venv/bin/activate && uv pip install -e .

# Listener
cd ../wa-listener && npm install
```

## 3. Servicios systemd user

```bash
mkdir -p ~/.config/systemd/user
cp ~/phoenix/deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now phoenix-brain phoenix-ui phoenix-listener
loginctl enable-linger jmfraga         # sobreviven al logout
```

## 4. Pair WhatsApp desde la UI

La UI Phoenix está expuesta por Tailscale (bind `0.0.0.0:8101` en el systemd unit). Desde cualquier máquina en tu Tailscale:

```
http://<HOST_IP>:8101/setup
```

(Atajo: ya está como tarjeta "Phoenix v2 Admin" en `http://<HOST_IP>:8080/` — <OWNER>'s Hub.)
1. La página `/setup` muestra el estado del listener.
2. Click **Vincular ahora** → aparece el QR en la página (se refresca solo cada ~20s vía SSE).
3. Escanea con WhatsApp del número que será Phoenix.
4. Cuando se conecte, la UI muestra "✅ Vinculado" con el JID. Copia ese JID.
5. Decide:
   - Si el número que pareaste **ES TU NÚMERO PERSONAL** (<OWNER>), usa ese JID también en `PHOENIX_OWNER_JID` (raro — normalmente Phoenix es otra cuenta).
   - Si el número que pareaste es **el de Phoenix** (otra cuenta WA), edita `PHOENIX_OWNER_JID` en `~/phoenix/.env` con TU número personal (`52XXX@s.whatsapp.net`) y reinicia el brain (`systemctl --user restart phoenix-brain`).

> Si por algún motivo la UI no muestra el QR (listener offline, etc.), puedes hacer pair "legacy" parando el servicio y corriéndolo en foreground con `PHOENIX_QR_TERMINAL=1`: `systemctl --user stop phoenix-listener && cd ~/phoenix/wa-listener && PHOENIX_QR_TERMINAL=1 npm run start`.

## 5. Smoke test

Desde el M4 (vía Tailscale) o local en la Pi:
```bash
curl -s http://<HOST_IP>:8102/health | jq   # si expones el puerto; default es 127.0.0.1
# o vía ssh tunnel:
ssh -L 8102:127.0.0.1:8102 <user>@<HOST_IP>
curl -s http://localhost:8102/health | jq
```

## 6. Configurar grupos reales (vía UI)

1. Agrega Phoenix a un grupo de WhatsApp.
2. El brain auto-registra el grupo en modo `lurker` la primera vez que llega un mensaje.
3. Abre `http://localhost:8101/` (con tunnel SSH), entra a **Grupos**, click en el grupo:
   - **Modo**: lurker / proactive / on_command_only (radio buttons, autosave).
   - **SOUL**: textarea grande con guardado versionado (cada save crea v+1).
   - **KBs**: suscribir desde dropdown con priority, quitar con un click.
4. **KBs**: crea una nueva en `/kbs` (slug + name + descripción para el clasificador).
   Entra a la KB y agrega facts (key + value). Facts añadidos por la UI quedan en `active` automáticamente.
5. **Audit**: revisa `/audit` para ver decisiones del detector de crisis y del clasificador proactivo.

## 7. Cutover desde <ECOSYSTEM>

Cuando estés convencido:
```bash
# Apaga phoenix en <ECOSYSTEM>
openclaw agents stop phoenix
# Verifica:
openclaw agents status

# Desvincula el canal WA de phoenix en <ECOSYSTEM> (vía UI o config) para liberar el número.
# En la app de WhatsApp del número +521XXXXXXXXXX, cierra la sesión de "WhatsApp Web/Devices" vinculada a <ECOSYSTEM>.

# Pair Phoenix v2 contra +521XXXXXXXXXX:
ssh <user>@<HOST_IP>
systemctl --user stop phoenix-listener
rm -rf ~/phoenix/wa-listener/auth/listener
cd ~/phoenix/wa-listener && npm run start  # escanea QR con +521XXXXXXXXXX
# Ctrl-C tras 'WA connected'
systemctl --user start phoenix-listener
```

## 8. Migrar a Postgres (opcional, cuando quieras)

Hoy SQLite es suficiente. Cuando quieras Postgres:
```bash
# Reusa el container de iris (mismo Docker) creando DB nueva:
docker exec -it <iris-pg-container> createdb -U iris phoenix

# Cambia PHOENIX_BRAIN_DB_URL en ~/phoenix/brain/.env a:
# postgresql+psycopg://iris:iris@localhost:5432/phoenix

# Instala extra:
cd ~/phoenix/brain && uv pip install -e '.[postgres]'

# Re-crear tablas y re-seed (los datos quedaron en SQLite — exportar a mano si vale la pena):
python -m phoenix_brain.db_init

systemctl --user restart phoenix-brain
```
