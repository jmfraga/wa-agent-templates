"""Detector de crisis. 7 categorías, Sonnet 4.6 con tool-use forzado.

Política: si el detector marca cualquier categoría, Phoenix calla. La señal va al
owner por DM (no se replica al grupo). Falsos positivos son aceptables; falsos
negativos no.

Categorías (no exclusivas — un mensaje puede tener varias):
- suicidio_o_autoagresion
- violencia_o_abuso
- urgencia_medica
- crisis_salud_mental
- sustancias_o_intoxicacion
- menor_en_riesgo
- amenaza_a_terceros
"""
from dataclasses import dataclass, field
from typing import Optional

import httpx

from . import anthropic_client
from .config import top

CRISIS_CATEGORIES = [
    "suicidio_o_autoagresion",
    "violencia_o_abuso",
    "urgencia_medica",
    "crisis_salud_mental",
    "sustancias_o_intoxicacion",
    "menor_en_riesgo",
    "amenaza_a_terceros",
]


@dataclass
class CrisisResult:
    detected: bool
    categories: list[str] = field(default_factory=list)
    severity: str = "none"  # none | low | medium | high
    reasoning: str = ""


_SYSTEM = (
    "Eres un clasificador de seguridad para un agente conversacional que opera en "
    "grupos de WhatsApp. Tu único trabajo es marcar mensajes que indiquen una "
    "situación de crisis o urgencia. Eres conservador: ante la duda, marcas. "
    "Falsos positivos son aceptables; falsos negativos no.\n\n"
    "Marca cualquier mensaje que contenga:\n"
    "- suicidio_o_autoagresion: ideación, plan o intento de suicidio o autoagresión\n"
    "  (incluye lenguaje implícito como 'ya no quiero seguir', 'pensé en hacerme algo')\n"
    "- violencia_o_abuso: violencia doméstica, abuso físico/sexual/psicológico\n"
    "  reportado en primera o tercera persona, amenazas creíbles\n"
    "- urgencia_medica: síntomas que requieren atención inmediata (dolor de pecho,\n"
    "  pérdida de consciencia, sangrado intenso, dificultad respiratoria, ACV, etc.)\n"
    "- crisis_salud_mental: episodio agudo no-suicida (ataque de pánico severo,\n"
    "  brote psicótico, disociación, estado catatónico)\n"
    "- sustancias_o_intoxicacion: sospecha de overdose, intoxicación aguda,\n"
    "  abstinencia severa\n"
    "- menor_en_riesgo: menor en peligro (negligencia, abuso, riesgo inmediato)\n"
    "- amenaza_a_terceros: ideación o plan de dañar a otra persona\n\n"
    "NO marques: discusiones académicas sobre salud, casos clínicos hipotéticos,\n"
    "papers o noticias sobre el tema, sarcasmo evidente, frases hechas\n"
    "('me muero de hambre', 'me dio un infarto el examen'), preguntas profesionales\n"
    "entre clínicos. Tampoco preguntas de marketing/operativas/técnicas sin\n"
    "relación con la persona.\n\n"
    "Después de evaluar, llama OBLIGATORIAMENTE al tool `report` con tu veredicto."
)

_REPORT_TOOL = {
    "name": "report",
    "description": "Reporta el veredicto de crisis del mensaje analizado.",
    "input_schema": {
        "type": "object",
        "properties": {
            "detected": {"type": "boolean", "description": "true si el mensaje contiene alguna de las 7 categorías"},
            "categories": {
                "type": "array",
                "items": {"type": "string", "enum": CRISIS_CATEGORIES},
                "description": "subconjunto de las 7 categorías que aplican; vacío si detected=false",
            },
            "severity": {
                "type": "string",
                "enum": ["none", "low", "medium", "high"],
                "description": "none si detected=false; high para riesgo inmediato a la vida",
            },
            "reasoning": {
                "type": "string",
                "description": "una sola oración explicando el veredicto, español",
            },
        },
        "required": ["detected", "categories", "severity", "reasoning"],
    },
}


def detect_crisis(text: str, *, model: Optional[str] = None) -> CrisisResult:
    """Clasifica el texto. Falla cerrada: si el modelo no devuelve veredicto, asume no_detected
    (registramos el error en logs upstream, no en el resultado)."""
    if not text or not text.strip():
        return CrisisResult(detected=False, severity="none", reasoning="empty_text")

    client = anthropic_client.get_client()
    used = model or anthropic_client.model_safety()

    resp = client.messages.create(
        model=used,
        max_tokens=512,
        system=_SYSTEM,
        tools=[_REPORT_TOOL],
        tool_choice={"type": "tool", "name": "report"},
        messages=[{"role": "user", "content": text}],
    )

    for blk in resp.content:
        if getattr(blk, "type", None) == "tool_use" and blk.name == "report":
            args = blk.input or {}
            cats = args.get("categories", []) or []
            return CrisisResult(
                detected=bool(args.get("detected", False)),
                categories=[c for c in cats if c in CRISIS_CATEGORIES],
                severity=args.get("severity", "none"),
                reasoning=args.get("reasoning", "")[:500],
            )

    # No tool_use → fallback no detectado (mejor inocente que ruido).
    return CrisisResult(detected=False, severity="none", reasoning="no_tool_use_in_response")


def notify_owner(
    *,
    result: CrisisResult,
    group_display_name: Optional[str],
    group_jid: Optional[str],
    contact_jid: Optional[str],
    contact_name: Optional[str],
    text_snippet: str,
) -> tuple[bool, Optional[str]]:
    """Envía un DM al owner vía listener `/post-to-jid`. No-op si no hay owner_jid o listener_url."""
    if not top.phoenix_owner_jid:
        return False, "no_owner_jid_configured"

    where = (
        f"grupo *{group_display_name or group_jid}*"
        if group_jid
        else f"DM de {contact_name or contact_jid}"
    )
    cats = ", ".join(result.categories) or "(sin categoría)"
    snippet = (text_snippet[:300] + "…") if len(text_snippet) > 300 else text_snippet
    msg = (
        f"⚠️ *Crisis detectada* ({result.severity})\n"
        f"En: {where}\n"
        f"De: {contact_name or contact_jid or '?'}\n"
        f"Categorías: {cats}\n"
        f"Razón: {result.reasoning}\n"
        f"---\n{snippet}\n---\n"
        "Phoenix se mantuvo en silencio. Decide tú si responder."
    )

    url = top.phoenix_listener_url.rstrip("/") + "/post-to-jid"
    try:
        r = httpx.post(
            url,
            json={"jid": top.phoenix_owner_jid, "text": msg},
            timeout=15.0,
        )
        if r.status_code >= 400:
            # Fallback al endpoint legacy en caso de listener viejo.
            r2 = httpx.post(
                top.phoenix_listener_url.rstrip("/") + "/post-to-group",
                json={"group_jid": top.phoenix_owner_jid, "text": msg},
                timeout=15.0,
            )
            if r2.status_code >= 400:
                return False, f"listener_status_{r.status_code}_{r2.status_code}"
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, f"listener_unreachable: {e}"
