"""Anthropic tools — incluye agentic (search_contacts, create_task, send_outbound, etc)."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from .db import get_session
from .models import Contact, KbFact, Ticket, TicketStatus

log = logging.getLogger("iris_brain.tools")

TOOLS: list[dict] = [
    {
        "name": "lookup_kb_fact",
        "description": (
            "Consulta información de cursos vigentes del Dr. Fraga (fechas, precios, modalidad, "
            "duración, cupos, links, contactos). DEBES llamar esta tool ANTES de responder cualquier "
            "pregunta sobre cursos — nunca improvises.\n\n"
            "Slugs disponibles:\n"
            "- 'blsacls' — paquete BLS + ACLS Querétaro (si el usuario dice 'ACLS' o 'BLS', usa este slug)\n"
            "- 'scpa' — Heartsaver RCP + DEA (primeros respondientes)\n"
            "- 'eusim1' — EuSim Nivel 1 (simulación clínica básica)\n"
            "- 'eusim2' — EuSim Nivel 2 (simulación avanzada)\n"
            "- 'debriefing' — Debriefing con IA para instructores\n"
            "- 'has-magia-con-claude' — Curso de Claude/IA para personal de salud\n"
            "- 'actores' — Formación de Actores para Simulación\n"
            "- 'mindfulness1' — Mindfulness clínico\n"
            "- 'ortopedia' — Webinar de IA aplicada a Ortopedia\n"
            "- '_global' — info global no atada a un curso: tanya_phone, urgencia_telefono, sitios, etc.\n\n"
            "Keys comunes: 'nombre', 'precio_mxn', 'fechas', 'modalidad', 'duracion', 'cupo', "
            "'landing_url', 'admin_contact', 'audiencia', 'estatus_cupos', 'sede', 'requisitos'.\n\n"
            "Estrategia: si no estás 100% seguro del slug o key, llama esta tool con tu mejor guess. "
            "Puedes llamarla varias veces en el mismo turno. Si no encuentras nada con varios intentos, "
            "usa `list_kb_facts` para ver qué existe.\n\n"
            "Devuelve {found: bool, kb_slug, key, value?, source?, version?}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kb_slug": {
                    "type": "string",
                    "description": "Slug exacto del curso (ej. 'blsacls', 'eusim2', '_global').",
                },
                "key": {
                    "type": "string",
                    "description": "Campo a consultar (ej. 'precio_mxn', 'fechas', 'landing_url').",
                },
            },
            "required": ["kb_slug", "key"],
        },
    },
    {
        "name": "list_kb_facts",
        "description": (
            "Lista todos los kb_facts disponibles (slug + key + preview del value). "
            "Úsalo cuando `lookup_kb_fact` no encuentra lo que buscas y necesitas descubrir "
            "qué slugs/keys existen realmente. También útil al inicio de una conversación de "
            "cursos para tener panorama."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kb_slug": {
                    "type": "string",
                    "description": "Opcional. Si se da, filtra a las keys de ese curso.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "lookup_contact",
        "description": "Busca un contacto por teléfono. Devuelve ficha {found, name, kind, notes, last_seen} o {found: false}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
            },
            "required": ["phone"],
        },
    },
    {
        "name": "update_contact",
        "description": (
            "Actualiza los datos del contacto que escribe ahora — guarda nombre, kind y/o notas. "
            "Llama esta tool cuando aprendas algo útil del contacto en la conversación: nombre, "
            "relación con el doctor o paciente, motivo recurrente, preferencias, alergias mencionadas, "
            "nombre del paciente si quien escribe es familiar, etc.\n\n"
            "**Cuándo SÍ usar:**\n"
            "- Recién te dijo su nombre → name='Carlos Pérez'.\n"
            "- Aclaró si es paciente / prospecto de curso / asesoría → kind.\n"
            "- Te dio contexto útil para futuras conversaciones → notes_append.\n\n"
            "kinds válidos: paciente, prospecto_curso, asesoria, colega, amigo, familia, otro.\n\n"
            "Si el contacto NO tiene nombre todavía, PRESENTATE y pregunta el nombre antes de "
            "abrir tickets de citas o cursos: 'Hola, soy Iris, asistente del Dr. Fraga. ¿Con quién "
            "tengo el gusto?'. Una vez te lo diga, llama esta tool con name.\n\n"
            "notes_append agrega texto al campo notes existente (no lo reemplaza). Usa frases cortas. "
            "Ejemplos buenos: 'hija de paciente Sra. González', 'instructora SimAcademy 2025', "
            "'pidió info ACLS sept', 'prefiere viernes', 'sin IVA por ser ext'.\n\n"
            "Si solo quieres reemplazar todo notes, usa notes_replace.\n\n"
            "Devuelve {ok, contact_id, fields_updated: [...]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Teléfono del contacto (el del thread actual)."},
                "name": {"type": "string", "description": "Nombre completo o como se presentó."},
                "kind": {
                    "type": "string",
                    "enum": ["owner", "paciente", "prospecto_curso", "asesoria", "colega", "amigo", "familia", "otro"],
                    "description": (
                        "Categoría del contacto. "
                        "paciente = consulta o quiere consultar al doctor. "
                        "prospecto_curso = interesado en cursos. "
                        "asesoria = busca asesoría profesional (legal, gestoría). "
                        "colega = profesional de salud o educación (médico, enfermera, instructor, profesor). "
                        "amigo = amistad personal. "
                        "familia = familiar del doctor. "
                        "otro = no clasifica claramente."
                    ),
                },
                "notes_append": {"type": "string", "description": "Nota corta a sumar a las existentes."},
                "notes_replace": {"type": "string", "description": "Reemplaza completamente las notas. Úsalo solo si Owner lo pide."},
            },
            "required": ["phone"],
        },
    },
    # ============ TOOLS AGÉNTICOS (Phase 1a) ============
    {
        "name": "search_contacts",
        "description": (
            "Busca contactos en el directorio por nombre, teléfono o notas (fuzzy ILIKE). "
            "Úsalo cuando Owner te pida hacer algo con una persona y solo te dé su nombre o referencia parcial. "
            "Ej: 'manda mensaje a Roberto' → search_contacts('Roberto'). "
            "Si devuelve varios resultados, pregunta a Owner cuál antes de proceder. "
            "Si devuelve 0, díselo y pregunta el teléfono.\n\n"
            "Devuelve {found, count, items: [{id, name, phone, kind, notes}]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto a buscar (nombre, teléfono o palabra clave)."},
                "kind": {"type": "string", "description": "Opcional: filtrar por kind (paciente, colega, etc)."},
                "limit": {"type": "integer", "description": "Máx resultados (default 10, max 20)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_task",
        "description": (
            "Crea una task agéntica con N destinatarios. Status inicial 'pending' — NO envía mensajes aún. "
            "Después llama send_outbound para cada target (uno por uno), tras confirmación de Owner.\n\n"
            "Usa esto cuando Owner te pide ejecutar una acción outbound (mandar mensajes, coordinar, invitar). "
            "owner_id debe ser el contact_id de Owner (lo tienes en el system block).\n\n"
            "kind: 'invitar' | 'coordinar_cita' | 'enviar_info' | 'recordatorio' | 'otro'.\n\n"
            "**OBLIGATORIO:** pasa `expected_names` con los nombres EXACTOS que Owner nombró, en el MISMO orden que target_contact_ids. "
            "El server valida que cada contact_id corresponde a un contact.name que comparte palabras con expected_names[i]. "
            "Si hay mismatch, el server REJECT la operación. Esto previene confundir contactos (ej. usar id de 'María' cuando Owner dijo 'José Luis').\n\n"
            "Devuelve {ok, task_id, target_count, targets: [{target_id, contact_id, contact_name, contact_phone}]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner_id": {"type": "integer", "description": "contact_id del owner (Owner). Lo tienes en el system block."},
                "kind": {"type": "string", "description": "Categoría de la task."},
                "summary": {"type": "string", "description": "1-2 frases describiendo qué pidió Owner."},
                "raw_instruction": {"type": "string", "description": "Texto literal que Owner dijo."},
                "target_contact_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Lista de contact_id de los destinatarios.",
                },
                "expected_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "OBLIGATORIO. Nombres de los destinatarios (mismo orden que target_contact_ids). Server valida que coincidan.",
                },
                "context": {
                    "type": "object",
                    "description": "Metadata libre: {lugar, fecha_propuesta, hora, etc}. Si la task involucra media o un mensaje template, USA mejor los campos `asset_id`/`caption`/`message_template` de abajo — el server los persiste en context para que el executor pueda dispararlos sin volver a pedir confirmación.",
                },
                "asset_id": {
                    "type": "integer",
                    "description": "OPCIONAL: id del MediaAsset (de find_media o import_marketing_asset). Si lo pasas, queda guardado en task.context.asset_id y send_outbound_media puede dispararse desde el executor (botón 'Ejecutar ahora' o scheduler).",
                },
                "caption": {
                    "type": "string",
                    "description": "OPCIONAL: caption asociado al asset_id. Se persiste en task.context.caption. Max 1024 chars. Acepta placeholders {{name}}, {{first_name}}.",
                },
                "message_template": {
                    "type": "string",
                    "description": "OPCIONAL: cuerpo del mensaje de texto (sin asset). Acepta placeholders {{name}}, {{first_name}}, {{phone}}. Se persiste en task.context.message_template para que el executor pueda enviarlo.",
                },
            },
            "required": ["owner_id", "kind", "summary", "target_contact_ids", "expected_names"],
        },
    },
    {
        "name": "send_outbound",
        "description": (
            "Envía mensaje al destinatario via WhatsApp y registra envío en task_targets. "
            "REQUIERE que Owner haya confirmado el plan antes de llamar esto.\n\n"
            "**IMPORTANTE — IDs:**\n"
            "- `task_id` viene del response de create_task ({task_id: N, ...}).\n"
            "- `target_id` viene del array `targets` de create_task: cada item tiene {target_id, contact_id, contact_name}. USA EL `target_id`, NO EL `contact_id`. Son distintos.\n\n"
            "Personaliza el body con el nombre del destinatario y tono Iris (cálido, español MX, breve).\n\n"
            "Si recibes error 'task_target no existe o no pertenece a la task', revisa que estés usando target_id (no contact_id).\n\n"
            "Devuelve {ok, message_id, target_id, thread_id} o {ok: false, error}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "task_id devuelto por create_task."},
                "target_id": {"type": "integer", "description": "target_id (del array targets de create_task). NO confundir con contact_id."},
                "body": {"type": "string", "description": "Texto a enviar al destinatario."},
            },
            "required": ["task_id", "target_id", "body"],
        },
    },
    {
        "name": "report_to_owner",
        "description": (
            "Manda un mensaje a Owner en Telegram (NO en WhatsApp). Úsalo para reportar:\n"
            "- Plan listo, pidiendo confirmación.\n"
            "- Confirmaciones de envío exitoso.\n"
            "- Cuando un destinatario responde (en vivo, una respuesta a la vez).\n"
            "- Cuando una task se completa.\n\n"
            "Mantén el reporte breve (1-3 líneas). Útil para mantener a Owner al tanto sin spam."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "El mensaje al chat de Telegram de Owner."},
                "task_id": {"type": "integer", "description": "Opcional: contexto del task relacionado."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_active_tasks",
        "description": (
            "Lista tasks activas (no complete ni cancelled). Útil cuando Owner pregunta "
            "'qué pendientes tengo' o 'qué estás coordinando'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner_id": {"type": "integer", "description": "Opcional. Si se da, filtra a tasks del owner."},
                "limit": {"type": "integer", "description": "Máx items (default 20)."},
            },
            "required": [],
        },
    },
    {
        "name": "update_task_status",
        "description": (
            "Cambia el status de una task manualmente. Útil cuando Owner dice 'cancela X' o "
            "'ya terminé Y, márcala como completa'.\n\n"
            "Status válidos: pending | in_progress | awaiting_responses | complete | cancelled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "awaiting_responses", "complete", "cancelled"]},
                "note": {"type": "string", "description": "Razón opcional (queda en logs)."},
            },
            "required": ["task_id", "status"],
        },
    },
    # ============ TOOLS DE MEDIA (Phase 1c) ============
    {
        "name": "find_media",
        "description": (
            "Busca media (imágenes/PDFs) en el storage de Iris por label, tag o filename. "
            "Úsalo ANTES de enviar imágenes en una task: 'manda la promo de ACLS' → find_media('ACLS promo').\n\n"
            "Si devuelve 1 hit → úsalo. Si devuelve varios → preséntale a Owner las opciones. "
            "Si devuelve 0 → pregunta si subir URL (import_marketing_asset) o que Owner la mande por Telegram.\n\n"
            "Devuelve {found, count, items: [{id, label, source, mime_type, preview_url, use_count, ...}]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto a buscar (label, tag, filename)."},
                "limit": {"type": "integer", "description": "Máx items (default 5)."},
                "source": {
                    "type": "string",
                    "enum": ["marketing", "ui_upload", "telegram", "whatsapp"],
                    "description": "Opcional: filtra por fuente.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "import_marketing_asset",
        "description": (
            "Descarga una imagen desde una URL whitelisted (marketing.simacademy.lat, info.*, blog.*) "
            "y la guarda como MediaAsset en Iris. Úsalo cuando Owner te dé un link directo a una promo nueva.\n\n"
            "Dedupe automático: si el archivo ya existe (sha256), devuelve el id existente con dedupe=true.\n\n"
            "Devuelve {ok, asset_id, dedupe, label, source, preview_url} o {ok: false, error}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL whitelisted del asset (jpg/png/webp/pdf)."},
                "label": {"type": "string", "description": "Nombre lógico ej 'Promo ACLS Verano 2026'."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags ej ['acls','promo','simacademy'].",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "send_outbound_media",
        "description": (
            "Envía una imagen al target via WhatsApp con caption opcional. Igual que send_outbound pero "
            "con asset_id en vez de body de texto.\n\n"
            "**REQUIERE confirmación previa de Owner** (igual que send_outbound).\n\n"
            "Personaliza el caption con tono Iris (cálido, español MX, breve). Caption max 1024 chars (límite WA).\n\n"
            "task_id y target_id vienen de create_task — usa target_id (NO contact_id).\n"
            "asset_id viene de find_media o import_marketing_asset.\n\n"
            "Devuelve {ok, message_id, target_id, thread_id, asset_id} o {ok: false, error}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "target_id": {"type": "integer", "description": "target_id (no contact_id)."},
                "asset_id": {"type": "integer", "description": "id del MediaAsset a enviar."},
                "caption": {"type": "string", "description": "Caption opcional (max 1024 chars)."},
            },
            "required": ["task_id", "target_id", "asset_id"],
        },
    },
    # ============ FIN TOOLS DE MEDIA ============
    {
        "name": "forward_owner_answer",
        "description": (
            "Reenvía la respuesta de Owner a un contacto que Iris consultó previamente "
            "(típicamente cuando Iris ya dijo 'le paso tu duda al doctor' tras un task_response_ack). "
            "ÚSALO cuando Owner te dé info corta como respuesta a algo que ya reportaste (precio, fecha, "
            "sí/no, disponibilidad, etc).\n\n"
            "**NO crea task nueva. NO uses create_task/send_outbound.**\n"
            "**NO incluye saludo de presentación** porque ya hubo conversación previa con el contacto.\n\n"
            "answer_text debe ser CORTO (1-3 oraciones) con tono Iris cálido y personalizado al nombre "
            "del contacto. Envía al thread más reciente del contacto.\n\n"
            "Devuelve {ok, thread_id, contact_name, warning?} o {ok: false, error}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_phone": {
                    "type": "string",
                    "description": "Teléfono del contacto destinatario (al que Iris consultó previamente).",
                },
                "answer_text": {
                    "type": "string",
                    "description": "Texto corto (1-1024 chars) con la info que Owner dio. Tono Iris cálido, español MX. Incluye nombre del contacto.",
                },
            },
            "required": ["contact_phone", "answer_text"],
        },
    },
    # ============ FIN TOOLS AGÉNTICOS ============
    {
        "name": "open_ticket",
        "description": (
            "Abre un ticket para que Owner responda. Usar cuando Iris no puede resolver "
            "(precio final, agenda, cuestiones clínicas, decisiones que requieren al doctor)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "integer"},
                "kind": {"type": "string", "description": "Categoría libre: agenda, precio, clinico, asesoria, otro."},
                "summary": {"type": "string", "description": "1-2 frases describiendo qué necesita el contacto."},
                "draft_for_jmf": {"type": "string", "description": "Mensaje sugerido para que Owner apruebe/edite antes de relay."},
            },
            "required": ["thread_id", "kind", "summary"],
        },
    },
]


def _lookup_kb_fact(kb_slug: str, key: str) -> dict[str, Any]:
    from .sessions import sanitize_phone  # noqa: F401 (no, but keep import light)
    with get_session() as s:
        cf = s.scalar(
            select(KbFact)
            .where(KbFact.kb_slug == kb_slug, KbFact.key == key)
            .order_by(KbFact.updated_at.desc())
        )
        if cf is None:
            return {"found": False, "kb_slug": kb_slug, "key": key}
        return {
            "found": True,
            "kb_slug": kb_slug,
            "key": key,
            "value": cf.value,
            "source": cf.source.value,
            "version": cf.version,
        }


def _list_kb_facts(kb_slug: str | None = None) -> dict[str, Any]:
    with get_session() as s:
        q = select(KbFact)
        if kb_slug:
            q = q.where(KbFact.kb_slug == kb_slug)
        rows = list(s.scalars(q.order_by(KbFact.kb_slug, KbFact.key)))
        items = []
        for cf in rows:
            v = cf.value
            preview = v if isinstance(v, str) and len(v) <= 80 else (
                (v[:77] + "...") if isinstance(v, str) else str(v)[:80]
            )
            items.append({"kb_slug": cf.kb_slug, "key": cf.key, "preview": preview})
        return {"found": len(items) > 0, "count": len(items), "items": items}


def _lookup_contact(phone: str) -> dict[str, Any]:
    from .sessions import sanitize_phone
    p = sanitize_phone(phone)
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            return {"found": False, "phone": p}
        return {
            "found": True,
            "phone": c.phone,
            "name": c.name,
            "kind": c.kind.value,
            "notes": c.notes,
            "last_seen": c.last_seen.isoformat() if c.last_seen else None,
        }


def _update_contact(
    phone: str,
    name: str | None = None,
    kind: str | None = None,
    notes_append: str | None = None,
    notes_replace: str | None = None,
) -> dict[str, Any]:
    from .sessions import sanitize_phone
    from .models import ContactKind
    from datetime import datetime, timezone

    p = sanitize_phone(phone)
    fields: list[str] = []
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            return {"ok": False, "error": "contacto no existe", "phone": p}

        if name is not None and name.strip():
            c.name = name.strip()
            fields.append("name")

        if kind is not None:
            try:
                c.kind = ContactKind(kind)
                fields.append("kind")
            except ValueError:
                return {"ok": False, "error": f"kind inválido: {kind}", "phone": p}

        if notes_replace is not None:
            c.notes = notes_replace.strip() or None
            fields.append("notes")
        elif notes_append is not None and notes_append.strip():
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            new_line = f"[{stamp}] {notes_append.strip()}"
            c.notes = f"{c.notes}\n{new_line}" if c.notes else new_line
            fields.append("notes")

        s.flush()
        cid = c.id

    log.info("update_contact phone=%s fields=%s", p, fields)
    return {"ok": True, "contact_id": cid, "fields_updated": fields, "phone": p}


def _open_ticket(thread_id: int, kind: str, summary: str, draft_for_jmf: str | None = None) -> dict[str, Any]:
    with get_session() as s:
        t = Ticket(
            thread_id=thread_id,
            kind=kind,
            summary=summary,
            draft_for_jmf=draft_for_jmf,
            status=TicketStatus.awaiting_jmf,
        )
        s.add(t)
        s.flush()
        return {"ok": True, "ticket_id": t.id, "status": t.status.value}


def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "lookup_kb_fact":
            return _lookup_kb_fact(args["kb_slug"], args["key"])
        if name == "list_kb_facts":
            return _list_kb_facts(args.get("kb_slug"))
        if name == "lookup_contact":
            return _lookup_contact(args["phone"])
        if name == "update_contact":
            return _update_contact(
                args["phone"],
                name=args.get("name"),
                kind=args.get("kind"),
                notes_append=args.get("notes_append"),
                notes_replace=args.get("notes_replace"),
            )
        if name == "open_ticket":
            return _open_ticket(
                int(args["thread_id"]),
                args["kind"],
                args["summary"],
                args.get("draft_for_jmf"),
            )
        # ----- agentic tools -----
        if name == "search_contacts":
            from . import agentic
            return agentic.search_contacts(args["query"], args.get("kind"), int(args.get("limit", 10)))
        if name == "create_task":
            from . import agentic
            # Merge asset_id / caption / message_template into context para que el
            # executor (botón 'Ejecutar ahora', scheduler, send-all) los encuentre
            # sin pedir confirmación extra. Esto arregla el bug donde Iris creaba
            # la task pero el context quedaba sin asset_id y send_outbound_media
            # nunca se disparaba.
            ctx = dict(args.get("context") or {})
            if args.get("asset_id") is not None:
                try:
                    ctx["asset_id"] = int(args["asset_id"])
                except (TypeError, ValueError):
                    pass
            if args.get("caption") is not None:
                ctx["caption"] = str(args["caption"])[:1024]
            if args.get("message_template") is not None:
                ctx["message_template"] = str(args["message_template"])
            return agentic.create_task(
                int(args["owner_id"]),
                args["kind"],
                args["summary"],
                args.get("raw_instruction"),
                [int(x) for x in args.get("target_contact_ids", [])],
                ctx or None,
                expected_names=args.get("expected_names"),
            )
        if name == "send_outbound":
            from . import agentic
            return agentic.send_outbound(int(args["task_id"]), int(args["target_id"]), args["body"])
        if name == "report_to_owner":
            from . import agentic
            return agentic.report_to_owner(args["text"], args.get("task_id"))
        if name == "list_active_tasks":
            from . import agentic
            return agentic.list_active_tasks(args.get("owner_id"), int(args.get("limit", 20)))
        if name == "update_task_status":
            from . import agentic
            return agentic.update_task_status(int(args["task_id"]), args["status"], args.get("note"))
        # ----- media tools (Phase 1c) -----
        if name == "find_media":
            from . import agentic
            return agentic.find_media(
                args["query"],
                limit=int(args.get("limit", 5)),
                source=args.get("source"),
            )
        if name == "import_marketing_asset":
            from . import agentic
            return agentic.import_marketing_asset(
                args["url"],
                label=args.get("label"),
                tags=args.get("tags"),
            )
        if name == "send_outbound_media":
            from . import agentic
            return agentic.send_outbound_media(
                int(args["task_id"]),
                int(args["target_id"]),
                int(args["asset_id"]),
                caption=args.get("caption"),
            )
        if name == "forward_owner_answer":
            from . import agentic
            return agentic.forward_owner_answer(
                contact_phone=args["contact_phone"],
                answer_text=args["answer_text"],
            )
    except Exception as e:
        log.exception("tool %s failed", name)
        return {"error": str(e)}
    return {"error": f"tool desconocida: {name}"}


def to_text(result: dict[str, Any]) -> str:
    import json
    return json.dumps(result, ensure_ascii=False)
