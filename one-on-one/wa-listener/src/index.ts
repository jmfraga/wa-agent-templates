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
 * respuestas async de owner al paciente sin pasar por el flujo síncrono.
 */

import 'dotenv/config';
import { Boom } from '@hapi/boom';
import makeWASocket, {
  DisconnectReason,
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

const log = pino({ level: LOG_LEVEL, name: 'iris-wa-listener' });

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
      // Phase 1c: imagen outbound. Baileys descarga `url` internamente y la envía
      // como imageMessage con caption opcional.
      const caption = payload.media.caption ?? payload.body ?? '';
      sent = await state.sock.sendMessage(jid, {
        image: { url: payload.media.url },
        caption: caption || undefined,
      });
      const messageId = sent?.key?.id ?? undefined;
      log.info(
        {
          phone: payload.phone,
          thread_id: payload.thread_id,
          message_id: messageId,
          media_url: payload.media.url,
          caption_len: caption.length,
        },
        'send-to-contact (media) ok',
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

  const sock = makeWASocket({
    auth: authState,
    printQRInTerminal: false,
    logger: pino({ level: 'warn' }) as never,
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

      const text = extractText(m.message);
      const mediaHint = extractMediaHint(m.message);

      if (!text && !mediaHint) continue;

      const contact_phone = jidToPhone(jid);
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

startListener().catch((err) => {
  log.error({ err }, 'fatal');
  process.exit(1);
});
