"""Detector de crisis para Iris v2.

Portado de Health Companion Sprint 1 (apps/api/src/api/agents/crisis.py).
Diferencias vs HC:
  - Templates adaptados: Iris es asistente de un médico real (Owner), no companion.
    Cada template incluye "estoy avisando al doctor" para reforzar el puente humano.
  - Tras detección, Iris SIEMPRE abre ticket urgente con Owner en Telegram (HC no tenía relay).
  - Owner "casi no ve pacientes" → falsos positivos son baratos. La detección es agresiva.

Funcionamiento:
  1. detect(text) revisa keywords normalizadas (sin acentos, lowercase) por categoría.
  2. Devuelve CrisisMatch(category, level, matched) o None.
  3. get_response(match) devuelve el texto que Iris envía al usuario.
  4. handle_message en chat.py corre detect() ANTES de classify_intent.
     Si hay match high → envía template, abre ticket urgente, NO llama LLM.
     Si hay match medium → envía clarification, abre ticket awaiting_jmf, NO llama LLM.
     Si no hay match → flujo normal de intents.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase + strip accents + collapse punctuation to spaces."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9\s]", " ", ascii_text)


# ---------------------------------------------------------------------------
# Keyword tables  (normalised — no accents, lowercase)
# ---------------------------------------------------------------------------

_CAT_A_HIGH = [
    "me quiero matar", "me voy a matar", "quiero suicidarme", "me voy a suicidar",
    "ya no quiero vivir", "ya no quiero seguir vivo", "ya no quiero seguir viva",
    "quiero acabar con todo", "quiero acabar con mi vida", "mejor me muero",
    "no merezco vivir", "tengo un plan para matarme", "ya tengo como hacerlo",
    "guarde pastillas para", "tengo el arma lista", "tengo la pistola lista",
    "me voy a aventar", "me voy a tirar", "ya escribi la carta",
    "me voy a cortar las venas", "lo intente antes", "la otra vez lo intente",
    "prefiero morirme", "todos estarian mejor sin mi",
    "voy a desaparecer para siempre", "me quiero hacer dano",
    "me hago cortes", "me corto a proposito", "me quemo a proposito",
    "acabar conmigo", "quitarme la vida",
]

_CAT_A_MED = [
    "ya no puedo mas", "ya no aguanto", "quisiera dormir y no despertar",
    "no le veo sentido a nada", "ojala no despertara", "ojala me pasara algo",
    "siento que sobro", "soy una carga", "nadie me extranaria",
    "para que sigo", "estoy en el fondo", "ya toque fondo", "no veo salida",
    "estoy harto de todo", "estoy harta de todo",
]

_CAT_B_HIGH = [
    "ataque de panico", "no puedo respirar de la ansiedad",
    "siento que me voy a desmayar de los nervios",
    "estoy temblando y no puedo parar",
    "siento que me muero de ansiedad",
    "me siento fuera de mi cuerpo",
    "no siento mis manos", "no siento mis piernas",
    "estoy en crisis ahorita",
    "no puedo dejar de llorar desde hace",
    "siento que me voy a volver loco", "siento que me voy a volver loca",
    "me estoy desconectando", "estoy hiperventilando",
    "no reconozco nada", "no se donde estoy",
]

_CAT_B_MED = [
    "tengo mucha ansiedad", "estoy muy ansioso", "estoy muy ansiosa",
    "no puedo dormir de la angustia", "estoy desbordado", "estoy desbordada",
    "ya no puedo con esto", "estoy al limite", "no me reconozco",
]

_CAT_C_HIGH = [
    "me duele el pecho fuerte", "me duele mucho el pecho",
    "dolor en el pecho que se va al brazo", "dolor en el pecho que se va a la mandibula",
    "presion en el pecho", "peso en el pecho",
    "no puedo respirar", "me cuesta mucho respirar", "me estoy ahogando",
    "se me cierra la garganta",
    "se me hincho la cara", "se me hincho la lengua", "se me hincho la garganta",
    "no puedo mover el brazo de un lado", "no puedo mover la pierna de un lado",
    "se me doblo la boca", "no puedo hablar bien de repente",
    "perdi la fuerza de un lado", "perdi la vista de un ojo",
    "se me trabo la lengua",
    "me dio un derrame", "me dio un infarto",
    "me desmaye", "me desvaneci",
    "vomito sangre", "vomite sangre", "escupo sangre",
    "dolor de cabeza el peor de mi vida",
    "convulsione", "me convulsione",
    "los labios azules", "piel morada", "piel azul",
    "no me siento la cara",
]

_CAT_C_MED = [
    "me siento muy mal del pecho", "me cuesta respirar a veces",
    "me dio una punzada fuerte", "me siento debil de repente",
    "me siento raro del corazon", "me mareo feo",
]

_CAT_D_HIGH = [
    "me pega", "me golpea", "me golpeo",
    "me amenazo con matarme", "me amenazo con un cuchillo", "me amenazo con una pistola",
    "tiene un arma y la saco",
    "me esta siguiendo", "me persigue",
    "me tiene encerrado", "me tiene encerrada", "no me deja salir",
    "me viola", "me violo", "me forzo",
    "abusa de mi",
    "le pega a los ninos", "le pega a mi hijo", "le pega a mi hija",
    "tengo miedo de que me mate",
    "me lastimo fisicamente",
    "me esta atacando ahorita",
    "me quito el celular", "me quito la identificacion",
    "me quemo", "me empujo", "me ahorco",
    "tengo miedo de regresar a mi casa",
]

_CAT_D_MED = [
    "en mi casa hay mucha violencia", "mi pareja se pone agresiva",
    "me grita y me asusta", "me trata muy mal",
    "me siento insegura en mi casa", "me siento inseguro en mi casa",
    "me controla todo",
    "tengo miedo de mi pareja", "tengo miedo de mi papa",
    "tengo miedo de mi mama", "tengo miedo de mi hermano",
]

_CAT_E_HIGH = [
    "me atropello", "me atropellaron", "tuve un accidente de coche",
    "choque fuerte", "me cai de las escaleras", "me cai de altura",
    "me cai desde el techo", "me cayo algo encima", "me aplaston",
    "creo que me rompi algo", "siento que tengo algo roto",
    "no puedo mover la pierna despues del golpe",
    "no puedo mover el brazo despues del accidente",
    "me pege muy fuerte en la cabeza", "me golpee la cabeza y vi todo negro",
    "me desmaye despues del golpe", "me parti la cabeza",
    "tengo un hueso de fuera", "se me salio el hueso", "se le ve el hueso",
]

_CAT_E_MED = [
    "me pege fuerte", "tuve un porrazo", "me dieron un trancazo",
    "me cai mal", "me lastime feo", "me torci horrible",
    "no puedo pararme despues de la caida",
    "me siento raro desde que me golpee",
]

_CAT_F_HIGH = [
    "estoy sangrando mucho", "no puedo parar el sangrado", "se me sale mucha sangre",
    "estoy perdiendo mucha sangre",
    "me corte muy profundo", "me corte y no para de sangrar",
    "la sangre sale a chorros", "la sangre brinca", "sale a borbotones",
    "estoy vomitando sangre", "vomite sangre", "estoy escupiendo sangre",
    "heces con sangre", "estoy evacuando sangre",
    "popo negra como chapopote", "melena",
    "estoy orinando sangre",
    "me esta sangrando la nariz desde hace mucho",
    "estoy embarazada y estoy sangrando",
    "sangrando con coagulos grandes",
    "se me empopo la toalla de sangre", "se me empopo la ropa de sangre",
    "herida de bala", "herida de cuchillo", "me apunalaron", "me dispararon",
]

_CAT_F_MED = [
    "me salio sangre al toser", "tengo sangre en la saliva",
    "me sangra una herida vieja", "tengo un moreton que crece",
    "me sangra mucho la encia",
    "tuve un sangrado raro", "estoy manchando mas de lo normal",
    "me siento mareado y estoy sangrando",
]

_CAT_G_HIGH = [
    "no responde", "no reacciona", "no despierta", "no respira",
    "no llora", "no se mueve",
    "esta inconsciente", "no puedo despertarlo", "no puedo despertarla",
    "creo que le esta dando un infarto",
    "le esta dando un derrame",
    "se esta convulsionando", "esta convulsionando y no para",
    "se esta ahogando", "se atraganto y no puede respirar",
    "se tomo un monton de pastillas",
    "intento hacerse algo",
    "lo encontre tirado", "la encontre tirada",
    "lo apunalaron", "le dispararon",
    "mi bebe no respira", "mi hijo no respira", "mi hija no respira",
    "mi mama no respira", "mi papa no respira",
    "alguien se desmayo aqui",
]

_CAT_G_MED = [
    "se siente muy mal", "esta muy palido", "esta muy palida",
    "esta morado", "esta morada",
    "le cuesta respirar", "se ve muy mal",
    "no me contesta bien", "esta vomitando y no reacciona",
    "se golpeo fuerte la cabeza",
]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@dataclass
class CrisisMatch:
    category: str       # suicida | crisis_emocional | emergencia_medica | violencia | trauma | hemorragia | terceros
    level: str          # "high" | "medium"
    matched: str        # keyword que disparó (para logging)


CATEGORIES: list[tuple[str, list[str], list[str]]] = [
    ("suicida",            _CAT_A_HIGH, _CAT_A_MED),
    ("crisis_emocional",   _CAT_B_HIGH, _CAT_B_MED),
    ("emergencia_medica",  _CAT_C_HIGH, _CAT_C_MED),
    ("violencia",          _CAT_D_HIGH, _CAT_D_MED),
    ("trauma",             _CAT_E_HIGH, _CAT_E_MED),
    ("hemorragia",         _CAT_F_HIGH, _CAT_F_MED),
    ("terceros",           _CAT_G_HIGH, _CAT_G_MED),
]

CATEGORY_NAMES = [c[0] for c in CATEGORIES]


def detect(text: str) -> CrisisMatch | None:
    """Return a CrisisMatch if a red-flag pattern is found, else None.

    High-signal matches take priority; within the same level, category
    order matters (suicida checked first — highest severity).
    """
    norm = _normalize(text)

    # Pass 1: high signal — any category
    for cat, high_pats, _ in CATEGORIES:
        for p in high_pats:
            if p in norm:
                return CrisisMatch(category=cat, level="high", matched=p)

    # Pass 2: medium signal — any category
    for cat, _, med_pats in CATEGORIES:
        for p in med_pats:
            if p in norm:
                return CrisisMatch(category=cat, level="medium", matched=p)

    return None


# ---------------------------------------------------------------------------
# Response templates (adaptados de HC, ajustados para voz de Iris)
# ---------------------------------------------------------------------------

CRISIS_TEMPLATES: dict[str, str] = {
    "suicida": (
        "Lo que me cuentas importa, y quiero que estés a salvo en este momento. "
        "Por favor habla ahora con alguien preparado para acompañarte:\n\n"
        "• SAPTEL — 55 5259-8121 (24h, gratis, sin juzgar)\n"
        "• Emergencias — 911\n\n"
        "Le estoy avisando al Dr. Fraga en este momento. "
        "Lo más importante eres tú. Aquí estoy."
    ),
    "crisis_emocional": (
        "Lo que describes suena muy intenso y tiene sentido que estés así. "
        "Quiero que hables con alguien que sepa acompañarte:\n\n"
        "• SAPTEL — 55 5259-8121 (24h, gratis)\n"
        "• 911 si sientes que algo físico también está pasando\n\n"
        "Le estoy avisando al doctor en este momento. Aquí sigo cuando estés más tranquilo."
    ),
    "emergencia_medica": (
        "Lo que describes necesita atención médica YA, no después.\n\n"
        "• Llama al 911 ahora, o pide que alguien te lleve a urgencias.\n"
        "• Si estás solo, abre la puerta de tu casa para que puedan entrar.\n"
        "• IMSS Orientación Médica — 800 890 7060\n\n"
        "Le estoy avisando al Dr. Fraga en este momento. "
        "Lo que necesitas es un médico en persona ya."
    ),
    "violencia": (
        "Lo que me cuentas es serio, y nadie merece vivir así. "
        "Hay líneas que funcionan 24h:\n\n"
        "• INMUJERES — 800 911 2511 (24h, confidencial)\n"
        "• Línea de las Mujeres CDMX — 800 108 4053\n"
        "• 911 si el riesgo es ahora\n\n"
        "Le estoy avisando al doctor. Tu seguridad es lo primero."
    ),
    "trauma": (
        "Lo que me describes puede ser una lesión seria. Por favor:\n\n"
        "• No te muevas más de lo necesario — sobre todo si hay golpe en cabeza, cuello o espalda.\n"
        "• Llama al 911 ahora, o que alguien lo haga por ti.\n"
        "• Quédate en lugar seguro y espera a los servicios de emergencia.\n\n"
        "Le estoy avisando al Dr. Fraga. Cuando estés con los paramédicos, me avisas."
    ),
    "hemorragia": (
        "Necesitas ayuda médica AHORA. Por favor:\n\n"
        "• Llama al 911 ya.\n"
        "• Si el sangrado es externo: presiona directo sobre la herida con una tela limpia, mantén la presión sin soltar.\n"
        "• Si sospechas sangrado interno: recuéstate, no te muevas, espera a emergencias o pide que te trasladen.\n\n"
        "Le estoy avisando al doctor en este momento."
    ),
    "terceros": (
        "Esto es una emergencia. Actúa ahora:\n\n"
        "1. Ponte en zona segura (aleja del peligro si lo hay).\n"
        "2. Llama al 911 — diles dónde estás y qué pasa; quédate en la línea, ellos te guían.\n"
        "3. Mientras llega la ayuda:\n"
        "   • Si no respira / inconsciente: inicia RCP solo con manos — presiona fuerte y rápido en el centro del pecho, sin parar.\n"
        "   • Si está consciente: mantenlo seguro, abrigado, sin moverlo si sospechas lesión.\n\n"
        "El 911 te guía en tiempo real mejor que yo. Le estoy avisando al doctor."
    ),
}

# Medium signal — pide aclaración antes de escalar pero ya abre ticket awaiting_jmf
CLARIFICATION_TEMPLATE = (
    "Lo que me dijiste me llamó la atención y quiero entenderte bien. "
    "¿Es algo que estás sintiendo en serio, o es más una manera de decir "
    "lo agotado/a que estás? No hay respuesta mala. "
    "Mientras tanto le aviso al doctor para que esté pendiente."
)


def get_response(match: CrisisMatch) -> str:
    """Return the response text Iris sends to the contact for this match."""
    if match.level == "medium":
        return CLARIFICATION_TEMPLATE
    return CRISIS_TEMPLATES.get(match.category, CRISIS_TEMPLATES["emergencia_medica"])
