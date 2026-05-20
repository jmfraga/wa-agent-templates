# Baileys 6.7+ — gotchas que vas a encontrar

Notas operativas que pueden ahorrarte horas. Aprendidas durante la construcción de los templates.

## 1. NO uses `@whiskeysockets/baileys": "latest"`

La versión `latest` puede ser un release candidate inestable (`7.0.0-rcXX`). Síntoma: ciclos de QR → 408 timeout cada ~3 min sin pasar nunca a `connected`.

**Fix**: pinea a `^6.7.18` o superior estable.

## 2. Llama siempre a `fetchLatestBaileysVersion()`

WhatsApp Web actualiza su protocolo periódicamente. Baileys hardcodea una versión por release, y se queda desactualizada. Síntoma: `reason: 405 Method Not Allowed`.

```ts
import { fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
const { version, isLatest } = await fetchLatestBaileysVersion();
sock = makeWASocket({ version, ... });
```

## 3. Identifícate como navegador desktop

```ts
import { Browsers } from '@whiskeysockets/baileys';
makeWASocket({ browser: Browsers.ubuntu('Chrome'), ... });
```

Sin esto WhatsApp marca la sesión como sospechosa y a veces no la acepta.

## 4. El QR cambió de formato en Baileys 7+

Baileys 7 emite el QR como deep-link URL (`https://wa.me/settings/linked_devices#<payload>`). WhatsApp en "Dispositivos vinculados" espera el payload puro (`2@base64,...`). Si lo pasas tal cual al generador de QR, el escaneo no completa.

**Fix**: extrae el payload antes de pintar:

```ts
const cleaned = qr.includes('#') ? qr.split('#').slice(1).join('#') : qr;
```

(Solo aplicable si decides usar Baileys 7+. En 6.7.x el QR ya viene puro.)

## 5. WhatsApp ahora usa LIDs (Linked IDs)

Por privacidad, WhatsApp asigna LIDs anónimos (`200000000000001@lid`) a algunos contactos en vez del JID con número (`5219991110001@s.whatsapp.net`).

Impactos:
- Tu detector de owner por JID no matchea. Necesitas también guardar el LID del owner como sinónimo.
- En grupos, las mentions vienen con LID. Si comparas `mentionedJid` contra tu JID con número, no detectas.
- Tu propio agente también tiene un LID (`sock.user.lid`).

**Fix**: mantén `Set<string> ownerLids` y `myLid` persistentes (en archivo o DB), y compara contra ambos en todos los gating.

## 6. `messages.upsert` también con `type === 'append'`

Baileys 6.7+ usa `type='append'` para algunos mensajes nuevos (no solo `notify`). Si solo procesas `notify`, pierdes mensajes en ciertos casos.

```ts
sock.ev.on('messages.upsert', ({ messages, type }) => {
  if (type !== 'notify' && type !== 'append') return;
  // ...
});
```

## 7. JIDs vienen con device-id

WhatsApp multi-device anexa `:N` al JID (`5219991110003:23@s.whatsapp.net`). Normaliza siempre antes de comparar:

```ts
function normalizeJid(jid: string): string {
  return jid.replace(/:\d+@/, '@');
}
```

## 8. `init queries` puede timeout sin romper la sesión

Después del primer pair, Baileys hace queries de sincronización (`fetchProps`, `executeInitQueries`). Pueden dar `Timed Out` (408 boom) sin que la conexión se caiga. Si filtras por error, ignora 408 en init queries — Baileys recupera solo.

## 9. Reason 515 post-pair es esperado

Justo después del primer pair exitoso, Baileys frecuentemente recibe un disconnect con `reason: 515` (stream:error). Es normal: WhatsApp re-establece el socket con la nueva sesión. El reconnect siguiente debe tener `connection: 'open'` con tu JID correcto. No hagas restart ni reset manual.

## 10. NO uses `printQRInTerminal: true` si tienes una UI

`printQRInTerminal` dibuja el QR ASCII en stdout cada rotación. Si servís el QR desde una UI (vía SSE u otro), tener también el terminal print contamina logs. Usa una env flag para activarlo solo en debug:

```ts
const QR_IN_TERMINAL = process.env.QR_IN_TERMINAL === '1';
// ...
if (QR_IN_TERMINAL) qrcode.generate(qr, { small: true });
```

## 11. Genera el QR como PNG server-side

Las librerías de QR client-side (vía CDN) pueden ser bloqueadas por adblockers o caer la red. Mucho más confiable: el listener emite el QR string raw, y el backend de tu UI lo convierte a PNG con `qrcode[pil]` (Python) o equivalente, y la UI lo renderiza como `<img>` con polling. Detalles en `groups/docs/pair-qr-via-ui.md`.
