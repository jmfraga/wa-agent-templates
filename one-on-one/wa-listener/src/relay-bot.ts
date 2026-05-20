/**
 * Iris v2 — relay-bot: canal a OWNER.
 *
 * TODO(OWNER): decidir whatsapp vs telegram en Sprint 0 finalize.
 *
 * Expone HTTP interno en RELAY_BOT_PORT (default 8098):
 *   POST /send-to-jmf  → manda ticket a OWNER por el canal elegido.
 *
 * Cuando OWNER responde (WA o Telegram), POSTea a brain /jmf/reply.
 */

import 'dotenv/config';
import http from 'node:http';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import fetch from 'node-fetch';
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  type WAMessage,
} from '@whiskeysockets/baileys';
import qrcode from 'qrcode-terminal';
import type { RelaySendRequest, JmfReplyRequest } from './types.js';

const BRAIN_URL = process.env.BRAIN_URL ?? 'http://localhost:8096';
const RELAY_CHANNEL = (process.env.RELAY_CHANNEL ?? 'telegram') as 'whatsapp' | 'telegram';
const PORT = Number(process.env.RELAY_BOT_PORT ?? 8098);
const LOG_LEVEL = process.env.LOG_LEVEL ?? 'info';

const log = pino({ level: LOG_LEVEL, name: 'iris-relay-bot' });

// ----------------------------------------------------------------------------
// Canal abstracto
// ----------------------------------------------------------------------------

interface RelayChannel {
  sendToJmf(req: RelaySendRequest): Promise<void>;
}

async function postJmfReplyToBrain(req: JmfReplyRequest): Promise<void> {
  try {
    const res = await fetch(`${BRAIN_URL}/jmf/reply`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(req),
    });
    if (!res.ok) {
      log.error({ status: res.status, text: await res.text() }, 'brain /jmf/reply non-2xx');
    }
  } catch (err) {
    log.error({ err }, 'failed POST /jmf/reply');
  }
}

// ----------------------------------------------------------------------------
// Canal Telegram
// ----------------------------------------------------------------------------

async function buildTelegramChannel(): Promise<RelayChannel> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) {
    throw new Error('TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID requeridos para RELAY_CHANNEL=telegram');
  }

  // optional dep — import dinámico
  const { default: TelegramBot } = await import('node-telegram-bot-api');
  const bot = new TelegramBot(token, { polling: true });

  // Map para correlacionar mensajes Telegram con tickets:
  // Telegram message_id → ticket_id (el bot pregunta vía reply al mensaje del ticket)
  const ticketByMsgId = new Map<number, { ticket_id: string; contact_phone: string }>();

  bot.on('message', async (msg) => {
    if (String(msg.chat.id) !== String(chatId)) return;
    const replyTo = msg.reply_to_message?.message_id;
    if (!replyTo) {
      log.warn({ msg_id: msg.message_id }, 'mensaje sin reply, ignorando');
      return;
    }
    const ticket = ticketByMsgId.get(replyTo);
    if (!ticket) {
      log.warn({ replyTo }, 'reply sin ticket conocido');
      return;
    }
    const text = msg.text ?? '';
    await postJmfReplyToBrain({
      ticket_id: ticket.ticket_id,
      contact_phone: ticket.contact_phone,
      text,
    });
  });

  return {
    async sendToJmf(req: RelaySendRequest): Promise<void> {
      const body = `🎫 Ticket ${req.ticket_id}\n📞 ${req.contact_phone}\n\n${req.summary}\n\n— Mensaje del paciente —\n${req.text}\n\n(Responde a este mensaje para contestarle al paciente.)`;
      const sent = await bot.sendMessage(chatId, body);
      ticketByMsgId.set(sent.message_id, {
        ticket_id: req.ticket_id,
        contact_phone: req.contact_phone,
      });
    },
  };
}

// ----------------------------------------------------------------------------
// Canal WhatsApp (segunda sesión Baileys)
// ----------------------------------------------------------------------------

async function buildWhatsappChannel(): Promise<RelayChannel> {
  const authDir = process.env.RELAY_AUTH_DIR ?? './auth/relay';
  const jmfPhoneRaw = process.env.JMF_PHONE;
  if (!jmfPhoneRaw) {
    throw new Error('JMF_PHONE requerido para RELAY_CHANNEL=whatsapp');
  }
  const jmfJid = `${jmfPhoneRaw.replace(/[^0-9]/g, '')}@s.whatsapp.net`;

  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const sock = makeWASocket({
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: 'warn' }) as never,
  });
  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      log.warn('Relay QR (sesión OWNER) — escanea con tu número personal');
      qrcode.generate(qr, { small: true });
    }
    if (connection === 'open') log.info('Relay WhatsApp conectado');
    if (connection === 'close') {
      const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      if (!loggedOut) {
        setTimeout(() => {
          buildWhatsappChannel().catch((err) => log.error({ err }, 'relay reconnect failed'));
        }, 2000);
      }
    }
  });

  // Correlación por ticket en el cuerpo del mensaje (último ticket abierto).
  // Implementación mínima: regex en el mensaje de OWNER para detectar ticket_id.
  const openTickets = new Map<string, { contact_phone: string }>();

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const m of messages as WAMessage[]) {
      if (m.key.fromMe) continue;
      if (m.key.remoteJid !== jmfJid) continue;
      const text =
        m.message?.conversation ?? m.message?.extendedTextMessage?.text ?? '';
      if (!text) continue;

      const match = text.match(/#([A-Za-z0-9_-]+)/);
      if (!match) {
        log.warn('reply de OWNER sin #ticket_id, ignorando');
        continue;
      }
      const ticket_id = match[1];
      const ticket = openTickets.get(ticket_id);
      if (!ticket) {
        log.warn({ ticket_id }, 'ticket desconocido');
        continue;
      }
      const cleaned = text.replace(/#[A-Za-z0-9_-]+/, '').trim();
      await postJmfReplyToBrain({
        ticket_id,
        contact_phone: ticket.contact_phone,
        text: cleaned,
      });
    }
  });

  return {
    async sendToJmf(req: RelaySendRequest): Promise<void> {
      openTickets.set(req.ticket_id, { contact_phone: req.contact_phone });
      const body = `🎫 #${req.ticket_id}\n📞 ${req.contact_phone}\n\n${req.summary}\n\n— Paciente —\n${req.text}\n\n(Responde mencionando #${req.ticket_id} para contestar.)`;
      await sock.sendMessage(jmfJid, { text: body });
    },
  };
}

// ----------------------------------------------------------------------------
// HTTP server
// ----------------------------------------------------------------------------

function readJsonBody(req: http.IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c as Buffer));
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

async function main(): Promise<void> {
  log.info({ RELAY_CHANNEL, PORT }, 'iniciando relay-bot');

  const channel: RelayChannel =
    RELAY_CHANNEL === 'whatsapp' ? await buildWhatsappChannel() : await buildTelegramChannel();

  const server = http.createServer(async (req, res) => {
    if (req.method === 'POST' && req.url === '/send-to-jmf') {
      try {
        const body = (await readJsonBody(req)) as RelaySendRequest;
        if (!body?.ticket_id || !body?.contact_phone) {
          res.writeHead(400, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'missing fields' }));
          return;
        }
        await channel.sendToJmf(body);
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: true }));
      } catch (err) {
        log.error({ err }, '/send-to-jmf failed');
        res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: String(err) }));
      }
      return;
    }
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true, channel: RELAY_CHANNEL }));
      return;
    }
    res.writeHead(404);
    res.end();
  });

  server.listen(PORT, () => log.info({ PORT }, 'relay-bot HTTP listo'));
}

main().catch((err) => {
  log.error({ err }, 'fatal');
  process.exit(1);
});
