"""CLI de administración. Hasta que llegue la UI (S3).

Subcomandos:
  Grupos:        list, add, mode, set-soul
  KBs:           kb-list, kb-add, kb-rm
  Facts:         fact-add, fact-list, fact-pending, fact-approve, fact-rm
  Suscripciones: subscribe, unsubscribe, kb-of-group
"""
import argparse
import sys
from datetime import datetime

from sqlalchemy import select

from .db import SessionLocal, create_all
from .models import GROUP_MODES, Group, GroupKb, GroupSoul, KbFact, KnowledgeBase
from .soul import invalidate_cache


# ── Grupos ───────────────────────────────────────────────────────────
def cmd_list(_args) -> int:
    with SessionLocal() as s:
        for g in s.execute(select(Group).order_by(Group.joined_at.desc())).scalars():
            kb_count = s.execute(
                select(GroupKb).where(GroupKb.group_id == g.id)
            ).scalars().all()
            print(f"{g.wa_jid}\t{g.mode}\tkbs={len(kb_count)}\t{g.display_name}\tactive={g.is_active}")
    return 0


def cmd_add(args) -> int:
    with SessionLocal() as s:
        if s.execute(select(Group).where(Group.wa_jid == args.jid)).scalar_one_or_none():
            print(f"ya existe: {args.jid}", file=sys.stderr)
            return 1
        s.add(Group(wa_jid=args.jid, display_name=args.name, mode=args.mode, is_active=True))
        s.commit()
        print(f"agregado {args.jid} ({args.mode})")
    return 0


def cmd_mode(args) -> int:
    if args.mode not in GROUP_MODES:
        print(f"mode inválido. Opciones: {GROUP_MODES}", file=sys.stderr)
        return 2
    with SessionLocal() as s:
        g = s.execute(select(Group).where(Group.wa_jid == args.jid)).scalar_one_or_none()
        if not g:
            print(f"no encontrado: {args.jid}", file=sys.stderr)
            return 1
        g.mode = args.mode
        s.commit()
        print(f"{args.jid} -> {args.mode}")
    invalidate_cache(args.jid)
    return 0


def cmd_set_soul(args) -> int:
    with SessionLocal() as s:
        g = s.execute(select(Group).where(Group.wa_jid == args.jid)).scalar_one_or_none()
        if not g:
            print(f"no encontrado: {args.jid}", file=sys.stderr)
            return 1
        active = s.execute(
            select(GroupSoul).where(GroupSoul.group_id == g.id, GroupSoul.is_active == True)  # noqa: E712
        ).scalar_one_or_none()
        new_version = (active.version + 1) if active else 1
        if active:
            active.is_active = False
        text = open(args.path, "r", encoding="utf-8").read()
        s.add(GroupSoul(group_id=g.id, soul_md=text, version=new_version, is_active=True))
        s.commit()
        print(f"{args.jid} SOUL v{new_version} cargado desde {args.path}")
    invalidate_cache(args.jid)
    return 0


# ── KBs ─────────────────────────────────────────────────────────────
def cmd_kb_list(_args) -> int:
    with SessionLocal() as s:
        for kb in s.execute(select(KnowledgeBase).order_by(KnowledgeBase.slug)).scalars():
            n_facts = len(
                s.execute(select(KbFact).where(KbFact.kb_id == kb.id, KbFact.status == "active")).scalars().all()
            )
            print(f"{kb.slug}\tfacts={n_facts}\t{kb.name}")
    return 0


def cmd_kb_add(args) -> int:
    with SessionLocal() as s:
        if s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == args.slug)).scalar_one_or_none():
            print(f"ya existe KB: {args.slug}", file=sys.stderr)
            return 1
        s.add(KnowledgeBase(slug=args.slug, name=args.name, description=args.description or ""))
        s.commit()
        print(f"KB agregada: {args.slug}")
    return 0


def cmd_kb_rm(args) -> int:
    with SessionLocal() as s:
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == args.slug)).scalar_one_or_none()
        if not kb:
            print(f"no encontrada: {args.slug}", file=sys.stderr)
            return 1
        if not args.force:
            print("usa --force para confirmar borrado (cascada de facts y suscripciones).", file=sys.stderr)
            return 2
        s.delete(kb)
        s.commit()
        print(f"KB borrada: {args.slug}")
    return 0


# ── Facts ───────────────────────────────────────────────────────────
def _resolve_kb_or_exit(s, slug: str) -> KnowledgeBase:
    kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == slug)).scalar_one_or_none()
    if not kb:
        print(f"KB no encontrada: {slug}", file=sys.stderr)
        sys.exit(1)
    return kb


def cmd_fact_add(args) -> int:
    with SessionLocal() as s:
        kb = _resolve_kb_or_exit(s, args.kb)
        value = args.value
        if value == "-":
            value = sys.stdin.read().rstrip()
        existing = s.execute(
            select(KbFact)
            .where(KbFact.kb_id == kb.id, KbFact.key == args.key, KbFact.status == "active")
            .order_by(KbFact.version.desc())
        ).scalar_one_or_none()
        v = (existing.version + 1) if existing else 1
        if existing:
            existing.status = "superseded"
        s.add(KbFact(kb_id=kb.id, key=args.key, value=value, source="jmf", version=v, status="active"))
        s.commit()
        print(f"{args.kb}/{args.key} v{v} guardado.")
    return 0


def cmd_fact_list(args) -> int:
    with SessionLocal() as s:
        kb = _resolve_kb_or_exit(s, args.kb)
        status = "active" if not args.all else None
        q = select(KbFact).where(KbFact.kb_id == kb.id)
        if status:
            q = q.where(KbFact.status == status)
        q = q.order_by(KbFact.key, KbFact.version.desc())
        for f in s.execute(q).scalars():
            v = (f.value[:80] + "…") if len(f.value) > 80 else f.value
            print(f"{f.key}\tv{f.version}\t{f.status}\t{f.source}\t{v}")
    return 0


def cmd_fact_pending(_args) -> int:
    with SessionLocal() as s:
        q = (
            select(KnowledgeBase.slug, KbFact)
            .join(KbFact, KbFact.kb_id == KnowledgeBase.id)
            .where(KbFact.status == "pending_review")
            .order_by(KnowledgeBase.slug, KbFact.key)
        )
        n = 0
        for slug, f in s.execute(q).all():
            print(f"#{f.id}\t{slug}/{f.key}\tv{f.version}\t{f.value[:120]}")
            n += 1
        if n == 0:
            print("(sin facts pending_review)")
    return 0


def cmd_fact_approve(args) -> int:
    with SessionLocal() as s:
        f = s.get(KbFact, args.id)
        if not f:
            print(f"fact #{args.id} no encontrado", file=sys.stderr)
            return 1
        if f.status != "pending_review":
            print(f"fact #{args.id} no está en pending_review (status={f.status})", file=sys.stderr)
            return 1
        # Activa este y supersede activos previos con la misma key.
        prev_active = s.execute(
            select(KbFact)
            .where(KbFact.kb_id == f.kb_id, KbFact.key == f.key, KbFact.status == "active")
        ).scalar_one_or_none()
        if prev_active:
            prev_active.status = "superseded"
        f.status = "active"
        f.valid_from = datetime.utcnow()
        s.commit()
        print(f"fact #{f.id} aprobado y activo.")
    return 0


def cmd_fact_rm(args) -> int:
    with SessionLocal() as s:
        f = s.get(KbFact, args.id)
        if not f:
            print(f"fact #{args.id} no encontrado", file=sys.stderr)
            return 1
        s.delete(f)
        s.commit()
        print(f"fact #{args.id} borrado.")
    return 0


# ── Suscripciones ──────────────────────────────────────────────────
def cmd_subscribe(args) -> int:
    with SessionLocal() as s:
        g = s.execute(select(Group).where(Group.wa_jid == args.jid)).scalar_one_or_none()
        if not g:
            print(f"grupo no encontrado: {args.jid}", file=sys.stderr)
            return 1
        kb = _resolve_kb_or_exit(s, args.kb)
        existing = s.execute(
            select(GroupKb).where(GroupKb.group_id == g.id, GroupKb.kb_id == kb.id)
        ).scalar_one_or_none()
        if existing:
            existing.priority = args.priority
            print(f"actualizada prioridad: {args.jid} ↔ {args.kb} (priority={args.priority})")
        else:
            s.add(GroupKb(group_id=g.id, kb_id=kb.id, priority=args.priority))
            print(f"{args.jid} ahora usa {args.kb} (priority={args.priority})")
        s.commit()
    invalidate_cache(args.jid)
    return 0


def cmd_unsubscribe(args) -> int:
    with SessionLocal() as s:
        g = s.execute(select(Group).where(Group.wa_jid == args.jid)).scalar_one_or_none()
        if not g:
            print(f"grupo no encontrado: {args.jid}", file=sys.stderr)
            return 1
        kb = _resolve_kb_or_exit(s, args.kb)
        existing = s.execute(
            select(GroupKb).where(GroupKb.group_id == g.id, GroupKb.kb_id == kb.id)
        ).scalar_one_or_none()
        if not existing:
            print("no estaba suscrito.")
            return 0
        s.delete(existing)
        s.commit()
        print(f"{args.jid} ya no usa {args.kb}")
    invalidate_cache(args.jid)
    return 0


def cmd_kb_of_group(args) -> int:
    with SessionLocal() as s:
        g = s.execute(select(Group).where(Group.wa_jid == args.jid)).scalar_one_or_none()
        if not g:
            print(f"grupo no encontrado: {args.jid}", file=sys.stderr)
            return 1
        rows = s.execute(
            select(KnowledgeBase.slug, GroupKb.priority)
            .join(GroupKb, GroupKb.kb_id == KnowledgeBase.id)
            .where(GroupKb.group_id == g.id)
            .order_by(GroupKb.priority.desc())
        ).all()
        if not rows:
            print("(sin KBs suscritas)")
            return 0
        for slug, prio in rows:
            print(f"{slug}\tpriority={prio}")
    return 0


# ── Parser ─────────────────────────────────────────────────────────
def main() -> int:
    create_all()
    p = argparse.ArgumentParser(prog="phoenix-admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    pa = sub.add_parser("add")
    pa.add_argument("jid"); pa.add_argument("name")
    pa.add_argument("--mode", default="lurker", choices=GROUP_MODES)
    pa.set_defaults(func=cmd_add)

    pm = sub.add_parser("mode")
    pm.add_argument("jid"); pm.add_argument("mode", choices=GROUP_MODES)
    pm.set_defaults(func=cmd_mode)

    ps = sub.add_parser("set-soul")
    ps.add_argument("jid"); ps.add_argument("path", help="ruta a .md")
    ps.set_defaults(func=cmd_set_soul)

    # KBs
    sub.add_parser("kb-list").set_defaults(func=cmd_kb_list)
    pka = sub.add_parser("kb-add")
    pka.add_argument("slug"); pka.add_argument("name")
    pka.add_argument("--description", default="")
    pka.set_defaults(func=cmd_kb_add)
    pkr = sub.add_parser("kb-rm")
    pkr.add_argument("slug"); pkr.add_argument("--force", action="store_true")
    pkr.set_defaults(func=cmd_kb_rm)

    # Facts
    pfa = sub.add_parser("fact-add")
    pfa.add_argument("kb"); pfa.add_argument("key")
    pfa.add_argument("value", help="texto del fact (usa '-' para leer stdin)")
    pfa.set_defaults(func=cmd_fact_add)

    pfl = sub.add_parser("fact-list")
    pfl.add_argument("kb"); pfl.add_argument("--all", action="store_true", help="incluye superseded/pending")
    pfl.set_defaults(func=cmd_fact_list)

    sub.add_parser("fact-pending").set_defaults(func=cmd_fact_pending)

    pfap = sub.add_parser("fact-approve")
    pfap.add_argument("id", type=int)
    pfap.set_defaults(func=cmd_fact_approve)

    pfr = sub.add_parser("fact-rm")
    pfr.add_argument("id", type=int)
    pfr.set_defaults(func=cmd_fact_rm)

    # Suscripciones
    psub = sub.add_parser("subscribe")
    psub.add_argument("jid"); psub.add_argument("kb")
    psub.add_argument("--priority", type=int, default=0)
    psub.set_defaults(func=cmd_subscribe)

    pun = sub.add_parser("unsubscribe")
    pun.add_argument("jid"); pun.add_argument("kb")
    pun.set_defaults(func=cmd_unsubscribe)

    pog = sub.add_parser("kb-of-group")
    pog.add_argument("jid")
    pog.set_defaults(func=cmd_kb_of_group)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
