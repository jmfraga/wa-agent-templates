import 'dotenv/config';
import {
  default as makeWASocket,
  DisconnectReason,
  Browsers,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  normalizeMessageContent,
  useMultiFileAuthState,
  type WAMessage,
  type WASocket,
  proto,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import pino from 'pino';
import express from 'express';
import { EventEmitter } from 'events';
import fetch from 'node-fetch';
import fs from 'fs';
import path from 'path';

const log = pino({ level: process.env.LOG_LEVEL || 'info' });

const BRAIN_URL = process.env.BRAIN_URL || 'http://localhost:8102';
const AUTH_DIR = process.env.AUTH_DIR || './auth/listener';
const LISTENER_PORT = parseInt(process.env.LISTENER_PORT || '8100', 10);
const OWNER_JID = process.env.OWNER_JID || '';
// LIDs (Linked IDs) que también identifican al owner. WhatsApp asigna LIDs
// anónimos por privacidad; el JID con número y el LID pueden coexistir.
// Comma-separated. Se autodescubren cuando llega un mensaje (ver más abajo).
const OWNER_LIDS_INITIAL = (process.env.OWNER_LIDS || '').split(',').map(s => s.trim()).filter(Boolean);
const ownerLids = new Set<string>(OWNER_LIDS_INITIAL);
const QR_IN_TERMINAL = process.env.PHOENIX_QR_TERMINAL === '1';

type PairState = 'connected' | 'pairing' | 'disconnected' | 'connecting';

let sock: WASocket | null = null;
let myJid: string | null = null;
let myLid: string | null = null; // LID propio de Phoenix (formato '<AGENT_LID>@lid')
let pairState: PairState = 'disconnected';
let lastQr: string | null = null;
let reconnectTimer: NodeJS.Timeout | null = null;

// Bus de eventos para SSE: 'qr' (string), 'connected' ({myJid}), 'state' (PairState)
const waEvents = new EventEmitter();
waEvents.setMaxListeners(0);

function isGroupJid(jid: string | undefined | null): boolean {
  return !!jid && jid.endsWith('@g.us');
}

// Quita el device-id (formato '+521XXXXXXXXXX:23@s.whatsapp.net' → '+521XXXXXXXXXX@s.whatsapp.net').
// WhatsApp multi-device asigna :N a cada dispositivo vinculado; comparamos contra el JID base.
function normalizeJid(jid: string | null | undefined): string {
  if (!jid) return '';
  return jid.replace(/:\d+@/, '@');
}

function setState(s: PairState) {
  if (pairState !== s) {
    pairState = s;
    waEvents.emit('state', s);
  }
}

type ExtractedMessage = {
  text: string;
  mediaHint?: string;
  hasImage?: boolean;
  imageMime?: string;
  imageSize?: number;
  hasDocument?: boolean;
  documentMime?: string;
  documentFilename?: string;
  documentSize?: number;
};

function extractText(msg: proto.IMessage | null | undefined): ExtractedMessage {
  if (!msg) return { text: '' };
  if (msg.conversation) return { text: msg.conversation };
  if (msg.extendedTextMessage?.text) return { text: msg.extendedTextMessage.text };
  if (msg.imageMessage) {
    return {
      text: msg.imageMessage.caption || '',
      mediaHint: 'image',
      hasImage: true,
      imageMime: msg.imageMessage.mimetype || 'image/jpeg',
      imageSize: Number(msg.imageMessage.fileLength || 0),
    };
  }
  if (msg.videoMessage?.caption) return { text: msg.videoMessage.caption, mediaHint: 'video' };
  if (msg.audioMessage) return { text: '', mediaHint: 'audio' };
  if (msg.documentMessage) {
    return {
      text: msg.documentMessage.caption || '',
      mediaHint: 'document',
      hasDocument: true,
      documentMime: msg.documentMessage.mimetype || 'application/octet-stream',
      documentFilename: msg.documentMessage.fileName || undefined,
      documentSize: Number(msg.documentMessage.fileLength || 0),
    };
  }
  if (msg.stickerMessage) return { text: '', mediaHint: 'sticker' };
  return { text: '' };
}

// Mime types que Anthropic acepta como image/document blocks.
const ALLOWED_IMAGE_MIMES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif']);
const ALLOWED_DOC_MIMES = new Set(['application/pdf']);
const MAX_MEDIA_BYTES = 4 * 1024 * 1024; // 4MB — Anthropic recomienda <5MB binarios

type MediaPayload = {
  kind: 'image' | 'document';
  mime: string;
  b64: string;
  filename?: string;
};

async function downloadIfEligible(m: proto.IWebMessageInfo, ext: ExtractedMessage): Promise<MediaPayload | null> {
  try {
    if (ext.hasImage) {
      if (!ext.imageMime || !ALLOWED_IMAGE_MIMES.has(ext.imageMime)) {
        log.info({ mime: ext.imageMime }, 'image mime no soportado, skip download');
        return null;
      }
      if (ext.imageSize && ext.imageSize > MAX_MEDIA_BYTES) {
        log.info({ size: ext.imageSize }, 'image too large, skip download');
        return null;
      }
    } else if (ext.hasDocument) {
      if (!ext.documentMime || !ALLOWED_DOC_MIMES.has(ext.documentMime)) {
        log.info({ mime: ext.documentMime }, 'document mime no soportado, skip download');
        return null;
      }
      if (ext.documentSize && ext.documentSize > MAX_MEDIA_BYTES) {
        log.info({ size: ext.documentSize }, 'document too large, skip download');
        return null;
      }
    } else {
      return null;
    }

    const buffer = (await downloadMediaMessage(
      m as WAMessage,
      'buffer',
      {},
      { reuploadRequest: sock!.updateMediaMessage, logger: pino({ level: 'warn' }) as any },
    )) as Buffer;

    if (buffer.length > MAX_MEDIA_BYTES) {
      log.info({ size: buffer.length }, 'downloaded media exceeds limit, drop');
      return null;
    }

    if (ext.hasImage) {
      return { kind: 'image', mime: ext.imageMime!, b64: buffer.toString('base64') };
    }
    return {
      kind: 'document',
      mime: ext.documentMime!,
      b64: buffer.toString('base64'),
      filename: ext.documentFilename,
    };
  } catch (e) {
    log.error({ err: String(e) }, 'downloadMediaMessage failed');
    return null;
  }
}

// contextInfo puede venir en cualquier tipo de mensaje (texto, imagen, video, o
// DOCUMENTO/PDF). Antes sólo se miraba texto/imagen/video → una mención sobre un PDF
// no se detectaba. `content` debe venir ya normalizado (sin wrappers).
function ctxInfo(content: proto.IMessage | null | undefined): proto.IContextInfo | undefined {
  if (!content) return undefined;
  return (
    content.extendedTextMessage?.contextInfo ||
    content.imageMessage?.contextInfo ||
    content.videoMessage?.contextInfo ||
    content.documentMessage?.contextInfo ||
    undefined
  );
}

function detectMention(content: proto.IMessage | null | undefined, selfJid: string | null, selfLid: string | null): boolean {
  const contextInfo = ctxInfo(content);
  const mentioned = (contextInfo?.mentionedJid || []).map(normalizeJid);
  if (selfJid && mentioned.includes(normalizeJid(selfJid))) return true;
  if (selfLid && mentioned.includes(normalizeJid(selfLid))) return true;
  const text =
    content?.conversation ||
    content?.extendedTextMessage?.text ||
    content?.imageMessage?.caption ||
    content?.videoMessage?.caption ||
    content?.documentMessage?.caption ||
    '';
  if (/@phoenix\b/i.test(text)) return true;
  // WhatsApp puede escribir el tag como '@<LID-number>' (ej. '@<AGENT_LID>').
  if (selfLid) {
    const lidNum = selfLid.split('@')[0];
    if (lidNum && text.includes(`@${lidNum}`)) return true;
  }
  if (selfJid) {
    const jidNum = selfJid.split('@')[0].split(':')[0];
    if (jidNum && text.includes(`@${jidNum}`)) return true;
  }
  return false;
}

function quotedFromSelf(content: proto.IMessage | null | undefined, selfJid: string | null): boolean {
  if (!selfJid) return false;
  const ctx = ctxInfo(content);
  if (!ctx?.participant) return false;
  return ctx.participant === selfJid;
}

async function postToBrain(payload: any): Promise<{ reply: string | null; gating: any } | null> {
  try {
    const r = await fetch(`${BRAIN_URL}/chat`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      log.warn({ status: r.status }, 'brain non-200');
      return null;
    }
    return (await r.json()) as any;
  } catch (e) {
    log.error({ err: String(e) }, 'brain call failed');
    return null;
  }
}

async function handleIncoming(m: WAMessage) {
  if (m.key.fromMe) return;
  const remoteJid = m.key.remoteJid;
  if (!remoteJid) return;
  if (remoteJid === 'status@broadcast') return;

  const inGroup = isGroupJid(remoteJid);
  const rawSender = (inGroup ? m.key.participant : remoteJid) || remoteJid;
  const senderJid = normalizeJid(rawSender);
  // Owner: JID canónico (con número) O cualquier LID asociado.
  const isOwner = !!OWNER_JID && (
    senderJid === normalizeJid(OWNER_JID) ||
    ownerLids.has(senderJid)
  );
  // Si llegó por LID, reescribimos al JID canónico antes de pasarlo al brain
  // para que slash commands, detector de owner, etc. funcionen consistente.
  const senderJidForBrain = isOwner && senderJid.endsWith('@lid') ? OWNER_JID : senderJid;

  if (!inGroup && !isOwner) {
    log.info({ remoteJid, senderJid, rawSender, OWNER_JID, knownLids: [...ownerLids], fromMe: m.key.fromMe }, 'ignored non-owner DM');
    return;
  }

  // Desempaquetar wrappers (documentWithCaption [PDF con caption/mención], ephemeral,
  // viewOnce, edited). SIN esto los PDFs no se veían: WhatsApp los manda como
  // documentWithCaptionMessage y `documentMessage` quedaba undefined.
  const content = normalizeMessageContent(m.message);
  const normMsg = { ...m, message: content } as WAMessage;

  const ext = extractText(content);
  const { text, mediaHint } = ext;
  if (!text && !mediaHint) return;

  const mentionsPhoenix = inGroup ? detectMention(content, myJid, myLid) : true;
  const quotedIsPhoenix = quotedFromSelf(content, myJid);
  const contactName = m.pushName || undefined;
  const quotedMsgId = ctxInfo(content)?.stanzaId || undefined;

  // Bajamos binarios solo cuando Phoenix va a procesar el mensaje activamente:
  // mention explícita, reply a Phoenix, o el owner habló. Evita gasto en
  // imágenes/PDFs random del grupo.
  const shouldDownloadMedia = (ext.hasImage || ext.hasDocument) &&
    (mentionsPhoenix || quotedIsPhoenix || isOwner);
  let media: MediaPayload[] | undefined;
  if (shouldDownloadMedia) {
    const m1 = await downloadIfEligible(normMsg, ext);
    if (m1) media = [m1];
  }

  log.info(
    {
      group: inGroup ? remoteJid : null,
      sender: senderJid,
      mention: mentionsPhoenix,
      isOwner,
      mediaHint,
      mediaProcessed: media ? media[0].kind : null,
      text: text.slice(0, 80),
    },
    'incoming',
  );

  const resp = await postToBrain({
    group_jid: inGroup ? remoteJid : null,
    contact_jid: senderJidForBrain,
    contact_name: contactName,
    text,
    media_hint: mediaHint,
    mentions_phoenix: mentionsPhoenix,
    quoted_msg_id: quotedMsgId,
    quoted_is_phoenix: quotedIsPhoenix,
    media,
  });

  if (!resp) return;
  log.info({ gating: resp.gating?.reason, willReply: !!resp.reply }, 'brain decision');

  if (resp.reply && sock) {
    await sock.sendMessage(remoteJid, { text: resp.reply });
  }
}

async function start() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  setState('connecting');

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  let waVersion: [number, number, number] | undefined;
  try {
    const { version, isLatest } = await fetchLatestBaileysVersion();
    waVersion = version;
    log.info({ version, isLatest }, 'baileys WA version');
  } catch (e) {
    log.warn({ err: String(e) }, 'no pude fetch latest WA version, uso default');
  }

  sock = makeWASocket({
    version: waVersion,
    auth: state,
    browser: Browsers.ubuntu('Chrome'),
    printQRInTerminal: false,
    logger: pino({ level: 'warn' }) as any,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      // Baileys 7+ emite el QR como deep-link 'https://wa.me/settings/linked_devices#<payload>'.
      // WhatsApp en 'Dispositivos vinculados' espera el payload puro (2@base64,...).
      const cleaned = qr.includes('#') ? qr.split('#').slice(1).join('#') : qr;
      lastQr = cleaned;
      setState('pairing');
      waEvents.emit('qr', cleaned);
      if (QR_IN_TERMINAL) {
        log.info('QR pairing (también en /wa/qr-stream):');
        qrcode.generate(cleaned, { small: true });
      } else {
        log.info('QR disponible en /wa/qr-stream (terminal print desactivado)');
      }
    }
    if (connection === 'open') {
      const userId = sock?.user?.id || '';
      const rawJid = userId.includes('@') ? userId : `${userId.split(':')[0]}@s.whatsapp.net`;
      myJid = normalizeJid(rawJid);
      // Baileys 6.7+ expone sock.user.lid si WA emitió uno.
      const rawLid = (sock?.user as any)?.lid;
      if (rawLid && typeof rawLid === 'string') {
        myLid = normalizeJid(rawLid);
        persistMyLid(myLid);
      }
      lastQr = null;
      setState('connected');
      waEvents.emit('connected', { myJid, myLid });
      log.info({ myJid, myLid }, 'WA connected');
    }
    if (connection === 'close') {
      const reason = (lastDisconnect?.error as Boom)?.output?.statusCode;
      const loggedOut = reason === DisconnectReason.loggedOut;
      log.warn({ reason, loggedOut }, 'WA disconnected');
      myJid = null;
      lastQr = null;
      if (loggedOut) {
        setState('disconnected');
      } else {
        setState('disconnected');
        reconnectTimer = setTimeout(() => start().catch((e) => log.error({ err: String(e) }, 'reconnect failed')), 3000);
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    log.info({ type, count: messages.length }, 'messages.upsert');
    // Procesamos tipos relevantes — 'notify' (mensajes nuevos en tiempo real) y
    // 'append' (que Baileys 6.7+ usa en algunos casos). Excluimos sync histórico.
    if (type !== 'notify' && type !== 'append') return;
    for (const m of messages) {
      try {
        await handleIncoming(m);
      } catch (e) {
        log.error({ err: String(e) }, 'handleIncoming failed');
      }
    }
  });
}

async function logoutAndReset(): Promise<void> {
  try {
    if (sock) {
      try {
        await sock.logout();
      } catch {
        // ya cerrada o sin auth
      }
      sock = null;
    }
  } catch (e) {
    log.error({ err: String(e) }, 'logout failed (continúa con reset)');
  }
  myJid = null;
  lastQr = null;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  // Limpia auth directory
  try {
    const resolved = path.resolve(AUTH_DIR);
    if (fs.existsSync(resolved)) {
      fs.rmSync(resolved, { recursive: true, force: true });
      log.info({ dir: resolved }, 'auth dir limpiado');
    }
  } catch (e) {
    log.error({ err: String(e) }, 'rm auth dir failed');
  }
  setState('disconnected');
}

// ─── HTTP server ────────────────────────────────────────────────────
const app = express();
app.use(express.json());

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', connected: !!myJid, my_jid: myJid, my_lid: myLid, pair_state: pairState });
});

app.post('/wa/my-lid', (req, res) => {
  const { lid } = req.body as { lid?: string };
  if (!lid) return res.status(400).json({ error: 'lid required' });
  myLid = normalizeJid(lid);
  persistMyLid(myLid);
  res.json({ status: 'ok', my_lid: myLid });
});

async function sendToJid(jid: string, text: string, res: express.Response) {
  if (!sock || !myJid) return res.status(503).json({ error: 'WA not connected' });
  try {
    await sock.sendMessage(jid, { text });
    res.json({ status: 'ok' });
  } catch (e) {
    log.error({ err: String(e), jid }, 'send failed');
    res.status(500).json({ error: String(e) });
  }
}

app.post('/post-to-jid', async (req, res) => {
  const { jid, text } = req.body as { jid?: string; text?: string };
  if (!jid || !text) return res.status(400).json({ error: 'jid and text required' });
  await sendToJid(jid, text, res);
});

app.post('/post-to-group', async (req, res) => {
  const { group_jid, text } = req.body as { group_jid?: string; text?: string };
  if (!group_jid || !text) return res.status(400).json({ error: 'group_jid and text required' });
  await sendToJid(group_jid, text, res);
});

// ─── Pair / QR endpoints ─────────────────────────────────────────────
app.get('/wa/state', (_req, res) => {
  res.json({
    state: pairState,
    my_jid: myJid,
    has_qr: !!lastQr,
  });
});

app.get('/wa/qr', (_req, res) => {
  res.json({ qr: lastQr, state: pairState, my_jid: myJid });
});

app.post('/wa/pair', async (req, res) => {
  const reset = req.query.reset === '1' || req.query.reset === 'true';
  try {
    if (reset || pairState === 'disconnected') {
      if (reset) await logoutAndReset();
      // arranca (o re-arranca) socket
      start().catch((e) => log.error({ err: String(e) }, 'pair start failed'));
      res.json({ status: 'ok', state: pairState, reset });
    } else {
      res.json({ status: 'ok', state: pairState, note: 'ya está activo, usa ?reset=1 para forzar re-pair' });
    }
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.get('/wa/qr-stream', (req, res) => {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.write(':\n\n'); // keep-alive comment

  // Empuja estado actual
  const sendEvent = (event: string, data: any) => {
    res.write(`event: ${event}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
  };

  sendEvent('state', { state: pairState, my_jid: myJid });
  if (lastQr) sendEvent('qr', { qr: lastQr });
  if (myJid) sendEvent('connected', { my_jid: myJid });

  const onQr = (qr: string) => sendEvent('qr', { qr });
  const onConnected = (p: any) => sendEvent('connected', p);
  const onState = (s: PairState) => sendEvent('state', { state: s });

  waEvents.on('qr', onQr);
  waEvents.on('connected', onConnected);
  waEvents.on('state', onState);

  // keep-alive cada 25s para que no se caiga el SSE
  const ka = setInterval(() => res.write(':keep-alive\n\n'), 25000);

  req.on('close', () => {
    clearInterval(ka);
    waEvents.off('qr', onQr);
    waEvents.off('connected', onConnected);
    waEvents.off('state', onState);
  });
});

app.post('/wa/logout', async (_req, res) => {
  await logoutAndReset();
  res.json({ status: 'ok', state: pairState });
});

// ─── My (Phoenix) LID persistence ───────────────────────────────────
const MY_LID_FILE = path.join(path.dirname(AUTH_DIR), 'my-lid.json');
function persistMyLid(lid: string) {
  try {
    fs.mkdirSync(path.dirname(MY_LID_FILE), { recursive: true });
    fs.writeFileSync(MY_LID_FILE, JSON.stringify({ lid }));
  } catch (e) {
    log.warn({ err: String(e) }, 'no pude persistir my-lid.json');
  }
}
function loadMyLid() {
  try {
    if (fs.existsSync(MY_LID_FILE)) {
      const data = JSON.parse(fs.readFileSync(MY_LID_FILE, 'utf8'));
      if (data?.lid) {
        myLid = data.lid;
        log.info({ myLid }, 'loaded persisted my-lid');
      }
    }
  } catch (e) {
    log.warn({ err: String(e) }, 'no pude cargar my-lid.json');
  }
}
loadMyLid();

// ─── Owner LID management ───────────────────────────────────────────
const LIDS_FILE = path.join(path.dirname(AUTH_DIR), 'owner-lids.json');
function loadPersistedLids() {
  try {
    if (fs.existsSync(LIDS_FILE)) {
      const data = JSON.parse(fs.readFileSync(LIDS_FILE, 'utf8'));
      if (Array.isArray(data)) {
        for (const lid of data) ownerLids.add(lid);
        log.info({ count: ownerLids.size }, 'loaded persisted owner LIDs');
      }
    }
  } catch (e) {
    log.warn({ err: String(e) }, 'no pude cargar owner-lids.json');
  }
}
function persistLids() {
  try {
    fs.mkdirSync(path.dirname(LIDS_FILE), { recursive: true });
    fs.writeFileSync(LIDS_FILE, JSON.stringify([...ownerLids]));
  } catch (e) {
    log.warn({ err: String(e) }, 'no pude persistir owner-lids.json');
  }
}
loadPersistedLids();

app.get('/wa/owner-lids', (_req, res) => {
  res.json({ lids: [...ownerLids], owner_jid: OWNER_JID });
});

app.post('/wa/owner-lids', (req, res) => {
  const { lid, lids } = req.body as { lid?: string; lids?: string[] };
  const toAdd = (lids || (lid ? [lid] : [])).filter(Boolean);
  for (const l of toAdd) ownerLids.add(l);
  persistLids();
  res.json({ status: 'ok', lids: [...ownerLids] });
});

app.delete('/wa/owner-lids/:lid', (req, res) => {
  ownerLids.delete(req.params.lid);
  persistLids();
  res.json({ status: 'ok', lids: [...ownerLids] });
});

app.listen(LISTENER_PORT, () => log.info({ port: LISTENER_PORT }, 'listener HTTP up'));

start().catch((e) => {
  log.error({ err: String(e) }, 'fatal');
  process.exit(1);
});
