/**
 * Iris v2 — WhatsApp listener principal.
 *
 * ⚠️  NO escanear el QR del número real de Iris hasta cutover Sprint 5.
 *     Usar un número sandbox por ahora.
 *
 * Flujo:
 *   1. Recibe mensaje WA → extrae text/media.
 *   2. POST a ${BRAIN_URL}/chat con {contact_phone, text, media_url?}.
 *   3. Envía la respuesta sincrónica del brain de vuelta vía Baileys.
 *
 * Sprint 2: además expone un HTTP server (puerto WA_LISTENER_PORT, default
 * 8099) con `POST /send-to-contact` para que el brain pueda entregar
 * respuestas async de Owner al paciente sin pasar por el flujo síncrono.
 */

import 'dotenv/config';
import { Boom } from '@hapi/boom';
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  normalizeMessageContent,
  useMultiFileAuthState,
  type WAMessage,
  type proto,
} from '@whiskeysockets/baileys';
import qrcode from 'qrcode-terminal';
import qrPng from 'qrcode';
import pino from 'pino';
import fetch from 'node-fetch';
import http from 'node:http';
import type {
  BrainChatRequest,
  BrainChatResponse,
  SendToContactRequest,
  SendToContactResponse,
} from './types.js';

type WASock = ReturnType<typeof makeWASocket>;

const BRAIN_URL = process.env.BRAIN_URL ?? 'http://localhost:8096';
const AUTH_DIR = process.env.AUTH_DIR ?? './auth/listener';
const LOG_LEVEL = process.env.LOG_LEVEL ?? 'info';
const WA_LISTENER_PORT = parseInt(process.env.WA_LISTENER_PORT ?? '8099', 10);
const OWNER_PHONES_ENV = process.env.OWNER_PHONES ?? '';

const log = pino({ level: LOG_LEVEL, name: 'iris-wa-listener' });

// Set de phones del owner (dígitos puros, sin '+'). Se siembra del env y se
// refresca cada 5 min consultando al brain /contacts?kind=owner.
const OWNER_PHONES = new Set<string>(
  OWNER_PHONES_ENV.split(',').map((p) => p.replace(/[^\d]/g, '')).filter(Boolean),
);

async function refreshOwnerPhones(): Promise<void> {
  try {
    const res = await fetch(`${BRAIN_URL}/contacts?kind=owner&limit=10&page_size=10`);
    if (!res.ok) {
      log.warn({ status: res.status }, 'owner phones refresh: non-2xx');
      return;
    }
    const data = (await res.json()) as { items?: Array<{ phone?: string }> };
    const items = data?.items ?? [];
    for (const it of items) {
      const digits = (it.phone || '').replace(/[^\d]/g, '');
      if (digits) OWNER_PHONES.add(digits);
    }
    log.info({ count: OWNER_PHONES.size }, 'owner phones refreshed');
  } catch (err) {
    log.warn({ err }, 'failed to refresh owner phones');
  }
}

function isOwnerPhone(phone: string): boolean {
  const digits = phone.replace(/[^\d]/g, '');
  return OWNER_PHONES.has(digits);
}

/**
 * Estado compartido entre el listener Baileys y el HTTP server.
 * `sock` se reescribe en cada reconexión; los handlers HTTP siempre
 * leen el valor actual a través de este objeto.
 */
const state: { sock: WASock | null; connected: boolean; lastQr: string | null } = {
  sock: null,
  connected: false,
  lastQr: null,
};

function extractText(msg: proto.IMessage | null | undefined): string {
  if (!msg) return '';
  return (
    msg.conversation ??
    msg.extendedTextMessage?.text ??
    msg.imageMessage?.caption ??
    msg.videoMessage?.caption ??
    msg.documentMessage?.caption ??
    ''
  );
}

function extractMediaHint(msg: proto.IMessage | null | undefined): string | undefined {
  if (!msg) return undefined;
  if (msg.imageMessage) return 'image';
  if (msg.audioMessage) return 'audio';
  if (msg.videoMessage) return 'video';
  if (msg.documentMessage) return 'document';
  return undefined;
}

function jidToPhone(jid: string): string {
  // 5219991110002@s.whatsapp.net -> +5219991110002
  const bare = jid.split('@')[0]?.split(':')[0] ?? jid;
  return bare.startsWith('+') ? bare : `+${bare}`;
}

// In-memory: phone → JID original (incluye @lid o @s.whatsapp.net) según vino del contacto.
// Necesario porque WhatsApp Multi-Device usa LIDs y phoneToJid no puede inferir el suffix.
const PHONE_TO_JID = new Map<string, string>();

function phoneToJid(phone: string): string {
  // Si recordamos el JID original del contacto, úsalo.
  const bare = phone.replace(/[^\d]/g, '');
  const remembered = PHONE_TO_JID.get(bare) || PHONE_TO_JID.get(phone);
  if (remembered) return remembered;
  // Fallback: asumimos s.whatsapp.net (numero real)
  return `${bare}@s.whatsapp.net`;
}

async function postToBrain(payload: BrainChatRequest): Promise<BrainChatResponse | null> {
  try {
    const res = await fetch(`${BRAIN_URL}/chat`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      log.error({ status: res.status, text: await res.text() }, 'brain returned non-2xx');
      return null;
    }
    return (await res.json()) as BrainChatResponse;
  } catch (err) {
    log.error({ err }, 'failed to POST to brain');
    return null;
  }
}

// ──────────────────────────────────────────────────────────────────────────
// HTTP server (Sprint 2)
// ──────────────────────────────────────────────────────────────────────────

function readJson<T>(req: http.IncomingMessage): Promise<T> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c: Buffer) => chunks.push(c));
    req.on('end', () => {
      try {
        const raw = Buffer.concat(chunks).toString('utf8');
        resolve(raw ? (JSON.parse(raw) as T) : ({} as T));
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res: http.ServerResponse, status: number, body: unknown): void {
  res.statusCode = status;
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify(body));
}

async function handleSendToContact(
  req: http.IncomingMessage,
  res: http.ServerResponse,
): Promise<void> {
  let payload: SendToContactRequest;
  try {
    payload = await readJson<SendToContactRequest>(req);
  } catch (err) {
    log.warn({ err }, 'send-to-contact: invalid JSON');
    return sendJson(res, 400, {
      ok: false,
      error: 'invalid_json',
    } satisfies SendToContactResponse);
  }

  const hasMedia = payload.media?.type === 'image' && !!payload.media.url;
  if (!payload.phone || (!payload.body && !hasMedia)) {
    return sendJson(res, 400, {
      ok: false,
      error: 'missing_phone_or_body',
    } satisfies SendToContactResponse);
  }

  if (!state.sock || !state.connected) {
    log.warn({ phone: payload.phone }, 'send-to-contact: socket not ready');
    return sendJson(res, 503, {
      ok: false,
      error: 'wa_not_connected',
    } satisfies SendToContactResponse);
  }

  const jid = phoneToJid(payload.phone);
  try {
    let sent;
    if (hasMedia && payload.media) {
      // Phase 1c.fix: si vienen body Y media, mandamos PRIMERO el texto (con su URL preview)
      // y DESPUÉS la imagen. Caption de la imagen = payload.media.caption (opcional, corto).
      // Si solo viene media → un solo sendMessage con caption.
      const hasBody = !!(payload.body && payload.body.trim());
      const imgCaption = payload.media.caption ?? (hasBody ? undefined : payload.body) ?? undefined;
      if (hasBody) {
        await state.sock.sendMessage(jid, { text: payload.body as string });
      }
      sent = await state.sock.sendMessage(jid, {
        image: { url: payload.media.url },
        caption: imgCaption || undefined,
      });
      const messageId = sent?.key?.id ?? undefined;
      log.info(
        {
          phone: payload.phone,
          thread_id: payload.thread_id,
          message_id: messageId,
          media_url: payload.media.url,
          body_len: hasBody ? (payload.body as string).length : 0,
          caption_len: (imgCaption || '').length,
        },
        'send-to-contact (text+media) ok',
      );
      return sendJson(res, 200, {
        ok: true,
        message_id: messageId ?? undefined,
      } satisfies SendToContactResponse);
    }
    sent = await state.sock.sendMessage(jid, { text: payload.body as string });
    const messageId = sent?.key?.id ?? undefined;
    log.info(
      {
        phone: payload.phone,
        thread_id: payload.thread_id,
        message_id: messageId,
        len: (payload.body as string).length,
      },
      'send-to-contact ok',
    );
    return sendJson(res, 200, {
      ok: true,
      message_id: messageId ?? undefined,
    } satisfies SendToContactResponse);
  } catch (err) {
    log.error({ err, phone: payload.phone }, 'send-to-contact failed');
    return sendJson(res, 500, {
      ok: false,
      error: (err as Error)?.message ?? 'send_failed',
    } satisfies SendToContactResponse);
  }
}

async function handleQr(_req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  if (state.connected) {
    res.statusCode = 200;
    res.setHeader('content-type', 'text/html; charset=utf-8');
    res.end('<html><body style="font-family:sans-serif;padding:40px;text-align:center"><h2>✅ Iris ya está vinculada a WhatsApp</h2><p>No hay QR pendiente. Refresca si la sesión se cae.</p></body></html>');
    return;
  }
  if (!state.lastQr) {
    res.statusCode = 503;
    res.setHeader('content-type', 'text/html; charset=utf-8');
    res.end('<html><body style="font-family:sans-serif;padding:40px;text-align:center"><h2>Esperando QR…</h2><p>Recarga en unos segundos.</p><script>setTimeout(()=>location.reload(),2000)</script></body></html>');
    return;
  }
  const dataUrl = await qrPng.toDataURL(state.lastQr, { width: 380, margin: 2 });
  res.statusCode = 200;
  res.setHeader('content-type', 'text/html; charset=utf-8');
  res.end(`<html><head><meta http-equiv="refresh" content="20"></head><body style="font-family:sans-serif;padding:30px;text-align:center"><h2>🌸 Escanea con WhatsApp</h2><p>Ajustes → Dispositivos vinculados → Vincular dispositivo</p><img src="${dataUrl}" alt="QR" /><p style="color:#666;font-size:12px">Se refresca cada 20s. El QR rota cada ~60s.</p></body></html>`);
}

function handleHealth(_req: http.IncomingMessage, res: http.ServerResponse): void {
  sendJson(res, 200, {
    ok: true,
    connected: state.connected,
    port: WA_LISTENER_PORT,
  });
}

function startHttpServer(): void {
  const server = http.createServer((req, res) => {
    const url = req.url ?? '/';
    if (req.method === 'GET' && url === '/health') {
      return handleHealth(req, res);
    }
    if (req.method === 'GET' && url === '/qr') {
      handleQr(req, res).catch((err) => {
        log.error({ err }, 'qr handler crashed');
        if (!res.writableEnded) sendJson(res, 500, { ok: false });
      });
      return;
    }
    if (req.method === 'POST' && url === '/send-to-contact') {
      handleSendToContact(req, res).catch((err) => {
        log.error({ err }, 'send-to-contact handler crashed');
        if (!res.writableEnded) {
          sendJson(res, 500, { ok: false, error: 'internal_error' });
        }
      });
      return;
    }
    sendJson(res, 404, { ok: false, error: 'not_found' });
  });

  server.listen(WA_LISTENER_PORT, () => {
    log.info({ port: WA_LISTENER_PORT }, 'HTTP delivery server listening');
  });

  server.on('error', (err) => {
    log.error({ err }, 'HTTP server error');
  });
}

// ──────────────────────────────────────────────────────────────────────────
// Baileys listener
// ──────────────────────────────────────────────────────────────────────────

async function startListener(): Promise<void> {
  const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  // Gotcha #2 (Baileys): consultar la versión WA Web más reciente que Baileys
  // soporta para que el handshake no quede en una versión vieja hardcoded.
  // Tolerante a fallo de red: si falla, dejamos que Baileys use su default.
  let version: [number, number, number] | undefined;
  try {
    const fetched = await fetchLatestBaileysVersion();
    version = fetched.version as [number, number, number];
    log.info({ version, isLatest: fetched.isLatest }, 'fetched WA Web version');
  } catch (err) {
    log.warn({ err }, 'fetchLatestBaileysVersion failed, using Baileys default');
  }

  const sock = makeWASocket({
    auth: authState,
    printQRInTerminal: false,
    logger: pino({ level: 'warn' }) as never,
    ...(version ? { version } : {}),
  });

  state.sock = sock;
  state.connected = false;

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      state.lastQr = qr;
      log.warn('QR recibido — abre http://100.71.128.102:8099/qr en navegador para escanearlo');
      qrcode.generate(qr, { small: true });
    }
    if (connection === 'open') {
      state.connected = true;
      log.info('WhatsApp listener conectado');
    }
    if (connection === 'close') {
      state.connected = false;
      const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      log.warn({ statusCode, loggedOut }, 'conexión cerrada');
      if (loggedOut) {
        log.error('logged out — borra AUTH_DIR y re-escanea con número sandbox');
        return;
      }
      // backoff simple
      setTimeout(() => {
        startListener().catch((err) => log.error({ err }, 'reconnect failed'));
      }, 2000);
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const m of messages as WAMessage[]) {
      if (!m.message) continue;
      if (m.key.fromMe) continue;
      if (m.key.remoteJid?.endsWith('@broadcast')) continue;
      if (m.key.remoteJid === 'status@broadcast') continue;
      // Ignorar grupos, comunidades, newsletters, status, etc. Acepta DMs (s.whatsapp.net) y LIDs (@lid, privacidad WA).
      if (m.key.remoteJid?.endsWith('@g.us')) continue;
      if (m.key.remoteJid?.endsWith('@newsletter')) continue;
      if (m.key.remoteJid?.endsWith('@call')) continue;
      const jidSuffix = m.key.remoteJid?.split('@')[1] ?? '';
      if (jidSuffix && jidSuffix !== 's.whatsapp.net' && jidSuffix !== 'lid') continue;

      const jid = m.key.remoteJid;
      if (!jid) continue;

      // Desempaquetar wrappers de WhatsApp (documentWithCaption [PDF/imagen con caption],
      // ephemeral [chats temporales], viewOnce, edited). SIN esto los PDFs/imágenes con
      // caption no se veían: WA los manda como documentWithCaptionMessage y
      // documentMessage/imageMessage quedaban undefined → el mensaje se descartaba.
      const content = normalizeMessageContent(m.message);

      const text = extractText(content);
      const mediaHint = extractMediaHint(content);

      if (!text && !mediaHint) continue;

      const contact_phone = jidToPhone(jid);

      // ─── Phase 1c — Owner media ingest por WA ───────────────────────
      // Si quien manda es el owner Y trae imagen (imageMessage o documentMessage image/*),
      // descargamos el blob vía Baileys y lo subimos a brain /media/upload.
      // No pasamos por brain /chat para este flujo (evita dead code).
      const imageMsg = content?.imageMessage;
      const docMsg = content?.documentMessage;
      const docIsImage = !!(docMsg && (docMsg.mimetype || '').startsWith('image/'));
      const hasIngestableMedia = !!imageMsg || docIsImage;

      if (hasIngestableMedia && isOwnerPhone(contact_phone)) {
        try {
          const buffer = (await downloadMediaMessage(
            { ...m, message: content } as WAMessage,
            'buffer',
            {},
            {
              logger: log as never,
              reuploadRequest: sock.updateMediaMessage,
            },
          )) as Buffer;

          const mime = imageMsg
            ? 'image/jpeg'
            : (docMsg?.mimetype || 'application/octet-stream');
          const caption =
            imageMsg?.caption ?? docMsg?.caption ?? '';
          const filename = imageMsg
            ? `wa-${m.key.id ?? 'image'}.jpg`
            : (docMsg?.fileName || `wa-${m.key.id ?? 'doc'}`);

          // Construir multipart manual con boundary.
          const FormData = (await import('form-data')).default;
          const form = new FormData();
          form.append('file', buffer, { filename, contentType: mime });
          form.append('source', 'whatsapp');
          if (caption) form.append('label', caption.slice(0, 200));

          const res = await fetch(`${BRAIN_URL}/media/upload`, {
            method: 'POST',
            body: form as unknown as NodeJS.ReadableStream,
            headers: form.getHeaders(),
          });
          if (!res.ok) {
            const errText = await res.text();
            log.error({ status: res.status, errText, contact_phone }, 'owner WA media upload failed');
            await sock.sendMessage(jid, { text: `❌ No pude guardar la imagen: brain ${res.status}` });
            continue;
          }
          const out = (await res.json()) as { id: number; label?: string; dedupe?: boolean };
          const dedupe = out.dedupe ? ', dedupe' : '';
          const label = out.label || caption || filename;
          await sock.sendMessage(jid, {
            text: `📸 Guardada como '${label}' (id=${out.id}, source=whatsapp${dedupe}) ✓`,
          });
          log.info({ id: out.id, label, contact_phone }, 'owner WA media ingested');
          continue;
        } catch (err) {
          log.error({ err, contact_phone }, 'owner WA media ingest failed');
          try {
            await sock.sendMessage(jid, { text: `❌ No pude guardar la imagen: ${(err as Error)?.message ?? err}` });
          } catch (sendErr) {
            log.error({ sendErr }, 'failed to notify owner of ingest error');
          }
          continue;
        }
      }

      // Recordar el JID original para responder con el suffix correcto (@lid vs @s.whatsapp.net)
      const bareDigits = contact_phone.replace(/[^\d]/g, '');
      PHONE_TO_JID.set(bareDigits, jid);
      PHONE_TO_JID.set(contact_phone, jid);

      // Si vino con @lid, intenta resolver al phone real vía la mapping de Baileys.
      let real_phone: string | undefined;
      if (jid.endsWith('@lid')) {
        try {
          // Baileys 7+: signalRepository tiene utilidades de mapping LID↔PN.
          const repo = (sock as unknown as { signalRepository?: { lidMapping?: { getPNForLID?: (lid: string) => Promise<string | null> } } }).signalRepository;
          const pn = await repo?.lidMapping?.getPNForLID?.(jid);
          if (pn) {
            real_phone = jidToPhone(pn);
            PHONE_TO_JID.set(real_phone.replace(/[^\d]/g, ''), pn);
            log.info({ lid: jid, real_phone }, 'LID resolved to PN');
          }
        } catch (err) {
          log.debug({ err }, 'lid→pn resolution failed');
        }
      }

      // pushname = nombre de display del contacto en WA (mejor que un LID anónimo)
      const pushname = (m as { pushName?: string }).pushName?.trim();

      log.info({ contact_phone, jid, real_phone, pushname, hasMedia: !!mediaHint, len: text.length }, 'incoming');

      const payload: BrainChatRequest & { pushname?: string; real_phone?: string } = {
        contact_phone: real_phone || contact_phone,
        text,
        media_url: mediaHint,
        message_id: m.key.id ?? undefined,
        timestamp: typeof m.messageTimestamp === 'number' ? m.messageTimestamp : undefined,
        pushname,
        real_phone,
      };
      // Si resolvimos al real_phone, asegúrate que el wa-listener mapea desde el real_phone al JID @lid (para enviar)
      if (real_phone) {
        PHONE_TO_JID.set(real_phone.replace(/[^\d]/g, ''), jid);
      }

      const brainRes = await postToBrain(payload);
      if (!brainRes?.reply) {
        log.warn({ contact_phone }, 'brain no devolvió reply, skip');
        continue;
      }

      try {
        await sock.sendMessage(jid, { text: brainRes.reply });
        log.info({ contact_phone, len: brainRes.reply.length }, 'reply sent');
      } catch (err) {
        log.error({ err, contact_phone }, 'failed to send reply');
      }
    }
  });
}

// HTTP server arranca de inmediato — responderá 503 hasta que `sock` esté
// conectado. Permite que el brain detecte estado vía /health.
startHttpServer();

// Sembrar owner phones del brain al arranque y refrescar cada 5 min.
refreshOwnerPhones().catch((err) => log.warn({ err }, 'initial owner refresh failed'));
setInterval(() => {
  refreshOwnerPhones().catch((err) => log.warn({ err }, 'periodic owner refresh failed'));
}, 5 * 60 * 1000);

startListener().catch((err) => {
  log.error({ err }, 'fatal');
  process.exit(1);
});
