"""Pipeline: gating → carga SOUL → llamada Anthropic (tool-use loop) → persiste."""
import json
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from . import anthropic_client
from .config import top
from .db import get_session
from .gating import GatingDecision, decide
from .models import Contact, Group
from . import app_config, audit, proactive, safety
from .discussions import maybe_handle_owner_command
from .sessions import history_for_contact, history_for_group, record_inbound, record_outbound
from .soul import default_soul, load_group_soul
from .tools import TOOL_DEFINITIONS, ToolContext, dispatch, subscribed_kbs_summary

MAX_TOOL_LOOPS = 5


@dataclass
class ChatRequest:
    group_jid: Optional[str]
    contact_jid: Optional[str]
    contact_name: Optional[str]
    text: str
    media_hint: Optional[str] = None
    mentions_phoenix: bool = False
    quoted_msg_id: Optional[str] = None
    quoted_is_phoenix: bool = False
    # Lista de bloques de media descargados por el listener: image | document (PDF).
    # Cada item: {kind, mime, b64, filename?}. None si no hay media downloadable.
    media: Optional[list[dict]] = None


@dataclass
class ChatResponse:
    reply: Optional[str]
    model: Optional[str]
    usage: dict
    gating: GatingDecision
    tool_calls: list[dict]


@dataclass
class _GroupSnapshot:
    id: int
    display_name: str
    mode: str


def _is_owner(contact_jid: Optional[str]) -> bool:
    owner = app_config.get("owner_jid", default=top.phoenix_owner_jid)
    return bool(owner) and contact_jid == owner


def _resolve_group(group_jid: Optional[str]) -> Optional[_GroupSnapshot]:
    if not group_jid:
        return None
    with get_session() as s:
        g = s.execute(select(Group).where(Group.wa_jid == group_jid)).scalar_one_or_none()
        if g is None:
            g = Group(
                wa_jid=group_jid,
                display_name=group_jid.split("@")[0],
                mode="lurker",
                is_active=True,
            )
            s.add(g)
            s.commit()
            s.refresh(g)
        return _GroupSnapshot(id=g.id, display_name=g.display_name, mode=g.mode)


def _upsert_contact(contact_jid: Optional[str], contact_name: Optional[str], is_owner: bool) -> None:
    if not contact_jid:
        return
    with get_session() as s:
        ct = s.execute(select(Contact).where(Contact.wa_jid == contact_jid)).scalar_one_or_none()
        if ct is None:
            s.add(Contact(wa_jid=contact_jid, display_name=contact_name, is_owner=is_owner))
            s.commit()


def _accumulate_usage(acc: dict, u: object) -> None:
    acc["input_tokens"] += getattr(u, "input_tokens", 0)
    acc["output_tokens"] += getattr(u, "output_tokens", 0)
    acc["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    acc["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0


def _extract_text(content: list) -> str:
    return "".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


def handle_chat(req: ChatRequest) -> ChatResponse:
    group = _resolve_group(req.group_jid)
    is_owner = _is_owner(req.contact_jid)
    _upsert_contact(req.contact_jid, req.contact_name, is_owner)

    record_inbound(
        group_id=group.id if group else None,
        contact_jid=req.contact_jid,
        contact_name=req.contact_name,
        text=req.text,
        media_hint=req.media_hint,
        quoted_msg_id=req.quoted_msg_id,
        mentions_phoenix=req.mentions_phoenix,
    )

    decision = decide(
        group.mode if group else None,
        is_owner=is_owner,
        mentions_phoenix=req.mentions_phoenix,
        quoted_is_phoenix=req.quoted_is_phoenix,
        text=req.text,
    )

    # Proactive: clasificador decide si flip de respond=False → True
    proactive_intent: Optional[str] = None
    if not decision.respond and decision.reason == "proactive_pending" and group is not None:
        soul_for_classify = (load_group_soul(req.group_jid) if req.group_jid else None) or default_soul()
        kb_sum = subscribed_kbs_summary(group.id)
        hist_for_classify = history_for_group(group.id, limit=20)
        pd = proactive.classify_relevance(
            group_id=group.id,
            group_display_name=group.display_name,
            soul=soul_for_classify,
            kb_summary=kb_sum,
            history=hist_for_classify,
            current_author=req.contact_name or req.contact_jid or "alguien",
            current_text=req.text,
        )
        audit.log(
            "proactive_classified",
            group_jid=req.group_jid,
            payload={
                "should_respond": pd.should_respond,
                "confidence": pd.confidence,
                "intent_kind": pd.intent_kind,
                "reasoning": pd.reasoning,
                "blocked_by_cooldown": pd.blocked_by_cooldown,
                "contact_jid": req.contact_jid,
                "contact_name": req.contact_name,
                "text_snippet": (req.text or "")[:200],
            },
        )
        if pd.should_respond:
            decision = GatingDecision(True, f"proactive_confirmed:{pd.intent_kind}")
            proactive_intent = pd.intent_kind
        else:
            # Silencio proactivo (cooldown, off-topic, low-value, below-threshold, error)
            return ChatResponse(reply=None, model=None, usage={}, gating=decision, tool_calls=[])

    if not decision.respond:
        return ChatResponse(reply=None, model=None, usage={}, gating=decision, tool_calls=[])

    # Intercept: slash commands del owner (sólo en DM, sin LLM tool loop).
    if is_owner and group is None:
        cmd_result = maybe_handle_owner_command(req.text)
        if cmd_result is not None:
            record_outbound(
                group_id=None,
                contact_jid=req.contact_jid,
                text=cmd_result.reply,
                model_used="(slash-command)",
                tokens_in=0,
                tokens_out=0,
            )
            return ChatResponse(
                reply=cmd_result.reply,
                model="(slash-command)",
                usage={"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                gating=decision,
                tool_calls=[],
            )

    # Safety: detector de crisis. Skip cuando is_owner (<OWNER> participa en grupos),
    # cuando mode=on_command_only (mensajes del owner ya filtrados arriba), o
    # cuando el texto está vacío (solo media).
    skip_crisis = is_owner or (group is not None and group.mode == "on_command_only") or not (req.text or "").strip()
    if not skip_crisis:
        crisis = safety.detect_crisis(req.text)
        if crisis.detected:
            audit.log(
                "crisis_detected",
                group_jid=req.group_jid,
                payload={
                    "categories": crisis.categories,
                    "severity": crisis.severity,
                    "reasoning": crisis.reasoning,
                    "contact_jid": req.contact_jid,
                    "contact_name": req.contact_name,
                    "text_snippet": (req.text or "")[:300],
                },
            )
            notify_ok, notify_err = safety.notify_owner(
                result=crisis,
                group_display_name=group.display_name if group else None,
                group_jid=req.group_jid,
                contact_jid=req.contact_jid,
                contact_name=req.contact_name,
                text_snippet=req.text or "",
            )
            if not notify_ok:
                audit.log(
                    "crisis_notify_failed",
                    group_jid=req.group_jid,
                    payload={"error": notify_err},
                )
            silent = GatingDecision(False, "crisis_silenced")
            return ChatResponse(reply=None, model=None, usage={}, gating=silent, tool_calls=[])

    soul_text = (load_group_soul(req.group_jid) if req.group_jid else None) or default_soul()
    if group:
        history: list[dict] = history_for_group(group.id)
    elif req.contact_jid:
        history = history_for_contact(req.contact_jid)
    else:
        history = []

    client = anthropic_client.get_client()
    model = anthropic_client.model_default()

    # System: SOUL (cacheable) + KB index (cacheable, separado) + contexto (no cache, cambia por turno)
    system_blocks: list[dict] = [
        {"type": "text", "text": soul_text, "cache_control": {"type": "ephemeral"}},
    ]
    kb_summary = subscribed_kbs_summary(group.id) if group else None
    if kb_summary:
        system_blocks.append({"type": "text", "text": kb_summary, "cache_control": {"type": "ephemeral"}})
    if group:
        ctx = f"\n[Contexto] Grupo: {group.display_name}. Modo: {group.mode}."
        if is_owner:
            ctx += " Quien te escribe es <OWNER> (el owner)."
        system_blocks.append({"type": "text", "text": ctx})

    author = "<OWNER>" if is_owner else (req.contact_name or req.contact_jid or "alguien")
    if not history:
        history = [{"role": "user", "content": f"{author}: {req.text}"}]

    tool_ctx = ToolContext(group_id=group.id if group else None, is_owner=is_owner)
    messages = list(history)

    # Si hay media descargada por el listener, reemplazamos el último mensaje
    # user (el current turn) por un content array con bloques image/document
    # + el texto. Anthropic mezcla los modales en un solo turn.
    if req.media:
        user_content: list[dict] = []
        media_meta_for_audit: list[dict] = []
        for m_block in req.media:
            kind = m_block.get("kind")
            mime = m_block.get("mime") or ""
            b64 = m_block.get("b64") or ""
            if not b64 or not mime:
                continue
            if kind == "image":
                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                })
            elif kind == "document":
                user_content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                })
            else:
                continue
            media_meta_for_audit.append({
                "kind": kind,
                "mime": mime,
                "size_b64": len(b64),
                "filename": m_block.get("filename"),
            })
        # Apéndice de texto. Mantenemos el formato "<author>: <text>" si hay texto.
        text_for_block = f"{author}: {req.text or ''}".strip().rstrip(":")
        user_content.append({"type": "text", "text": text_for_block or f"[{author} envió media]"})
        # Reemplazar el último mensaje user
        if messages and messages[-1].get("role") == "user":
            messages[-1] = {"role": "user", "content": user_content}
        else:
            messages.append({"role": "user", "content": user_content})
        if media_meta_for_audit:
            audit.log(
                "media_processed",
                group_jid=req.group_jid,
                payload={"media": media_meta_for_audit, "contact_jid": req.contact_jid},
            )
    usage_acc = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    tool_calls_log: list[dict] = []
    reply_text = ""

    for _ in range(MAX_TOOL_LOOPS):
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_blocks,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        _accumulate_usage(usage_acc, resp.usage)

        if resp.stop_reason != "tool_use":
            reply_text = _extract_text(resp.content)
            break

        # Reúne tool_use blocks y ejecuta cada uno; arma el siguiente mensaje user con tool_result.
        assistant_blocks = []
        tool_results = []
        for blk in resp.content:
            kind = getattr(blk, "type", None)
            if kind == "text":
                assistant_blocks.append({"type": "text", "text": blk.text})
            elif kind == "tool_use":
                assistant_blocks.append(
                    {"type": "tool_use", "id": blk.id, "name": blk.name, "input": blk.input}
                )
                result = dispatch(blk.name, blk.input or {}, tool_ctx)
                tool_calls_log.append({"name": blk.name, "input": blk.input, "result": result})
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": blk.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        messages.append({"role": "assistant", "content": assistant_blocks})
        messages.append({"role": "user", "content": tool_results})
    else:
        # Salimos del loop sin break (MAX_TOOL_LOOPS alcanzado).
        reply_text = reply_text or "(tool-use loop limit reached)"

    if reply_text:
        record_outbound(
            group_id=group.id if group else None,
            contact_jid=req.contact_jid if group is None else None,
            text=reply_text,
            model_used=model,
            tokens_in=usage_acc["input_tokens"],
            tokens_out=usage_acc["output_tokens"],
        )
        # Si fue una intervención proactiva exitosa, marcar cooldown.
        if proactive_intent is not None and group is not None:
            proactive.mark_triggered(group.id)
            audit.log(
                "proactive_triggered",
                group_jid=req.group_jid,
                payload={"intent_kind": proactive_intent, "reply_preview": reply_text[:200]},
            )

    return ChatResponse(
        reply=reply_text or None,
        model=model,
        usage=usage_acc,
        gating=decision,
        tool_calls=tool_calls_log,
    )
