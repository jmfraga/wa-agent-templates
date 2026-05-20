"""Crea tablas y siembra estado mínimo."""
from datetime import datetime

from sqlalchemy import select

from .db import SessionLocal, create_all
from .models import Group, GroupSoul, KnowledgeBase
from .soul import DEFAULT_SOUL


SEED_GROUPS = [
    {
        "wa_jid": "demo-oficina@g.us",
        "display_name": "Oficina (demo)",
        "mode": "lurker",
        "soul_md": (
            "Eres Phoenix en el grupo de Oficina. Tu especialidad es marketing, redes sociales y "
            "operaciones de <ORG_1> / <ORG_2>. Tono mexicano, cálido, claro, "
            "orientado a acción. Conoces el calendario editorial, los productos (cursos), las "
            "plataformas (blogs, landings, Clientify) y el equipo. No cotizas en firme, no "
            "confirmas fechas, no agendas. Cuando dudes, dilo abiertamente."
        ),
    },
    {
        "wa_jid": "demo-ia@g.us",
        "display_name": "Inteligencia Artificial (demo)",
        "mode": "lurker",
        "soul_md": (
            "Eres Phoenix en el grupo de Inteligencia Artificial. Tu especialidad es IA aplicada "
            "a salud (oncología, urgencias, atención clínica) y a educación de profesionales de "
            "la salud (simulación, debriefing, OSCE, CBME). Lees papers, distingues hype de "
            "señal, citas evidencia (BEME, Cochrane, Kirkpatrick). Tono mexicano, técnico pero "
            "accesible, pragmático."
        ),
    },
    {
        "wa_jid": "demo-openclaw@g.us",
        "display_name": "<ECOSYSTEM> (demo)",
        "mode": "lurker",
        "soul_md": (
            "Eres Phoenix en el grupo de <ECOSYSTEM>. Tu especialidad es la plataforma <ECOSYSTEM>: "
            "agentes, gateway, canales (Telegram/WhatsApp), Synapse Router, NIM, MLX, modelos, "
            "deploys en RPi5/ThinkCentre. Tono técnico, directo. Conoces los incidentes, "
            "workarounds y bugs documentados. No prometas updates ni cambios sin que <OWNER> "
            "confirme."
        ),
    },
]


def seed() -> None:
    create_all()
    with SessionLocal() as s:
        for entry in SEED_GROUPS:
            existing = s.execute(select(Group).where(Group.wa_jid == entry["wa_jid"])).scalar_one_or_none()
            if existing:
                continue
            g = Group(
                wa_jid=entry["wa_jid"],
                display_name=entry["display_name"],
                mode=entry["mode"],
                is_active=True,
                joined_at=datetime.utcnow(),
            )
            s.add(g)
            s.flush()
            s.add(GroupSoul(group_id=g.id, soul_md=entry["soul_md"], version=1, is_active=True))

        # KB ejemplos vacías (sin facts).
        for slug, name in [
            ("marketing", "Marketing y RRSS"),
            ("ia-salud", "IA en Salud y Educación"),
            ("openclaw", "<ECOSYSTEM> — plataforma"),
        ]:
            if not s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == slug)).scalar_one_or_none():
                s.add(KnowledgeBase(slug=slug, name=name))

        s.commit()
    print("DB inicializada y sembrada (3 grupos demo + 3 KBs vacías).")


if __name__ == "__main__":
    seed()
