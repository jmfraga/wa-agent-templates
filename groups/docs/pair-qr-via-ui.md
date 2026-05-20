# Pair WhatsApp QR vía UI (planeado, S3)

Hoy el pair QR se imprime en la terminal del listener (`npm run start`). Para
S3 queremos vincularlo desde la UI admin de Phoenix, mismo patrón discutido
para Iris v2.

## Flujo objetivo

1. Usuario entra a `https://phoenix-ui/setup` (puerto 8101, futuro).
2. La UI pregunta al `phoenix-listener` su estado (`/wa/state`):
   - `connected` → muestra "Phoenix vinculado a `+521XXXXXXXXXX`", botón "Re-vincular" (logout).
   - `pairing` → muestra el QR.
   - `disconnected` → botón "Vincular ahora" dispara `/wa/pair`.
3. Al disparar el pair, el listener:
   - Limpia `auth/listener/` (sólo si el usuario confirmó).
   - Reinicia el socket Baileys; el evento `qr` queda disponible.
4. La UI hace stream del QR vía **SSE** desde `/wa/qr-stream` (el QR rota cada
   ~20s; SSE evita polling).
5. Al detectar `connection === 'open'`, el listener cierra el stream con un
   evento `{type: "connected", jid: "5214426...@s.whatsapp.net"}`. La UI lo
   recibe, muestra "Vinculado" y guarda el JID en DB (`config.owner_jid` o
   `config.phoenix_jid` según corresponda).

## Endpoints a agregar al listener

```ts
GET  /wa/state          → { state: 'connected'|'pairing'|'disconnected', my_jid?: string }
POST /wa/pair           → inicia pairing (limpia auth si query ?reset=1)
GET  /wa/qr-stream      → SSE: data: <qr-string>\n\n (un evento cada vez que Baileys emite uno; al conectar manda data: {"type":"connected", ...})
POST /wa/logout         → cierra sesión y limpia auth/
```

Cambios en `wa-listener/src/index.ts`:
- Guardar el último `qr` recibido en una variable en memoria.
- Mantener un `EventEmitter` interno; `qr-stream` se suscribe y vuelca eventos.
- `connection.update` con `connection === 'open'` emite `connected` y limpia el QR.

## En la UI (Next.js o FastAPI+HTMX)

- Página `/setup` con un componente que abre `EventSource('/api/wa/qr-stream')`
  proxy al listener.
- Render del QR con `qrcode.react` (Next) o `<img>` server-side
  (FastAPI+HTMX).
- Botón "Re-vincular" llama `POST /wa/logout` y luego `POST /wa/pair?reset=1`.

## Notas operativas

- El pair-vía-UI requiere que la UI sea accesible (Tailscale en la Pi).
- Si la UI está caída pero el listener corre, se puede caer al método legacy
  (`journalctl --user -fu phoenix-listener` y ver el QR ASCII) — dejar
  `printQRInTerminal: process.env.PHOENIX_QR_TERMINAL === '1'` como flag.
- El listener no debe exponer `/wa/*` fuera de localhost a menos que la UI
  proxee con auth. Default bind 127.0.0.1; la UI hace el proxy autenticado.

## Cuando arranquemos S3

- Implementar endpoints en `wa-listener/src/wa-pair.ts`.
- Página `setup` en UI con SSE consumer.
- Persistir `phoenix_jid` y `owner_jid` en una tabla `config` (key/value) o
  pedir al usuario que confirme cuál es cuál (Phoenix vs <OWNER>).

Mismo patrón aplicable a Iris v2 si comparten convenciones cuando ambos
existan.
