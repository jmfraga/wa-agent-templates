// Tipos compartidos entre listener y relay-bot.

export interface BrainChatRequest {
  contact_phone: string;
  text: string;
  media_url?: string;
  message_id?: string;
  timestamp?: number;
}

export interface BrainChatResponse {
  reply: string;
  // Si el brain decide escalar a owner, devuelve un ticket que el relay-bot enviará.
  escalate?: {
    ticket_id: string;
    summary: string;
    contact_phone: string;
  };
  meta?: Record<string, unknown>;
}

export interface RelaySendRequest {
  ticket_id: string;
  contact_phone: string;
  summary: string;
  text: string;
}

export interface JmfReplyRequest {
  ticket_id: string;
  contact_phone: string;
  text: string;
}

/**
 * Sprint 2: el brain llama al listener para entregar respuestas async de owner
 * al paciente (cuando owner ya contestó el ticket y necesitamos mandar
 * la respuesta por el mismo canal WA, fuera del flujo síncrono del inbound).
 */
export interface SendToContactMedia {
  type: 'image';
  url: string;
  mime_type?: string;
  caption?: string;
}

export interface SendToContactRequest {
  phone: string; // E.164, ej. "+5215512345678"
  body?: string;
  thread_id?: string | number;
  // Phase 1c: imagen outbound. Si se da, el listener envía como imageMessage
  // descargando `url` (típicamente http://127.0.0.1:8096/media/{id}/raw).
  media?: SendToContactMedia;
  // Tipo del payload (cosmetico, para logs)
  type?: string;
}

export interface SendToContactResponse {
  ok: boolean;
  message_id?: string;
  error?: string;
}
