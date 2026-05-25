"""Tests para kb_ingest: whitelist, slug derive, parser Haiku, dry_run,
audit log y rate-limit del endpoint /admin/kb-facts/ingest-url."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from iris_brain import kb_ingest
from iris_brain.db import get_session
from iris_brain.models import KbIngestLog


# ---------------------------------------------------------------------------
# is_whitelisted
# ---------------------------------------------------------------------------


def test_is_whitelisted_acepta_dominios_conocidos():
    assert kb_ingest.is_whitelisted("https://info.example.com/curso/") is True
    assert kb_ingest.is_whitelisted("https://blog.example.com/post") is True
    assert kb_ingest.is_whitelisted("https://marketing.example.com/promo.jpg") is True
    # http también vale (mismo host)
    assert kb_ingest.is_whitelisted("http://info.example.com/") is True
    # dominio externo NO vale, aunque incluya el host en la query
    assert kb_ingest.is_whitelisted("https://evil.com/?host=info.example.com") is False
    # input basura
    assert kb_ingest.is_whitelisted("not-a-url") is False


# ---------------------------------------------------------------------------
# derive_slug
# ---------------------------------------------------------------------------


def test_derive_slug_casos():
    assert (
        kb_ingest.derive_slug("https://info.example.com/has-magia-con-claude/")
        == "has-magia-con-claude"
    )
    assert kb_ingest.derive_slug("https://info.example.com/curso-acls") == "curso-acls"
    assert (
        kb_ingest.derive_slug("https://info.example.com/cursos/avanzado/") == "avanzado"
    )
    assert kb_ingest.derive_slug("https://info.example.com/page.html") == "page"


# ---------------------------------------------------------------------------
# _html_to_text — limpia ruido
# ---------------------------------------------------------------------------


def test_html_to_text_quita_script_style_nav():
    pytest.importorskip("bs4")
    html = (
        "<html><body>"
        "<nav>menu superior</nav>"
        "<script>x=1</script>"
        "<main>Contenido principal<style>.x{color:red}</style> aqui.</main>"
        "<footer>pie</footer>"
        "</body></html>"
    )
    text = kb_ingest._html_to_text(html)
    assert "menu superior" not in text
    assert "x=1" not in text
    assert ".x{color:red}" not in text
    assert "pie" not in text
    assert "Contenido principal" in text


# ---------------------------------------------------------------------------
# _extract_with_haiku — parsea JSON envuelto en explicación/fences
# ---------------------------------------------------------------------------


def _fake_haiku_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _patch_anthropic_with_text(monkeypatch, text: str):
    """Hace que anthropic.Anthropic() devuelva un cliente cuyo
    messages.create() responde con `text`."""
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_haiku_response(text)

    def _fake_ctor(*a, **kw):
        return fake_client

    monkeypatch.setattr(kb_ingest.anthropic, "Anthropic", _fake_ctor)
    return fake_client


def test_extract_with_haiku_parsea_envuelto_en_fences(monkeypatch):
    """Haiku a veces devuelve 'Aquí tienes:\n```json {...} ```'."""
    wrapped = (
        "Aquí tienes el JSON solicitado:\n"
        "```json\n"
        '{"nombre": "Curso ACLS", "precio": "$3500", "modalidad": "presencial"}\n'
        "```\n"
        "Saludos."
    )
    _patch_anthropic_with_text(monkeypatch, wrapped)
    data = kb_ingest._extract_with_haiku("https://info.example.com/x/", "Texto fixture")
    assert data == {
        "nombre": "Curso ACLS",
        "precio": "$3500",
        "modalidad": "presencial",
    }


def test_extract_with_haiku_parsea_json_directo(monkeypatch):
    raw = '{"nombre": "Curso BLS", "precio": "$1500"}'
    _patch_anthropic_with_text(monkeypatch, raw)
    data = kb_ingest._extract_with_haiku("https://info.example.com/y/", "Texto")
    assert data == {"nombre": "Curso BLS", "precio": "$1500"}


# ---------------------------------------------------------------------------
# ingest_url dry_run — no escribe a DB
# ---------------------------------------------------------------------------


def test_ingest_url_dry_run_no_escribe(monkeypatch):
    """dry_run=True debe extraer y devolver preview, sin tocar DB."""
    # Mockear scrape + extract para evitar HTTP y Anthropic reales.
    monkeypatch.setattr(
        kb_ingest, "_scrape_html", lambda url: "<html><body><main>x</main></body></html>"
    )
    # _html_to_text necesita bs4; lo mockeamos para no depender de la dep en CI.
    monkeypatch.setattr(kb_ingest, "_html_to_text", lambda html: "texto plano fixture")
    monkeypatch.setattr(
        kb_ingest,
        "_extract_with_haiku",
        lambda url, text, _usage_sink=None: {"nombre": "X", "precio": "$100"},
    )
    r = kb_ingest.ingest_url(
        "https://info.example.com/x/", slug="x-slug", dry_run=True
    )
    assert r["ok"] is True
    assert r["dry_run"] is True
    assert r["slug"] == "x-slug"
    assert r["facts"] == {"nombre": "X", "precio": "$100"}
    assert r["would_upsert"] == 2
    assert "diff" in r


# ---------------------------------------------------------------------------
# Audit log — _log_ingest persiste row con cost_estimate y se ve en DB
# ---------------------------------------------------------------------------


def test_audit_log_persiste_con_cost_estimate(monkeypatch):
    """Una corrida dry_run exitosa deja una row en kb_ingest_log con
    facts_count, tokens y cost_usd_estimate > 0."""
    monkeypatch.setattr(
        kb_ingest, "_scrape_html", lambda url: "<html><body><main>x</main></body></html>"
    )
    monkeypatch.setattr(kb_ingest, "_html_to_text", lambda html: "texto plano fixture")

    # _extract_with_haiku acepta _usage_sink — simulamos que lo rellena.
    def _fake_extract(url, text, _usage_sink=None):
        if _usage_sink is not None:
            _usage_sink["tokens_input"] = 1234
            _usage_sink["tokens_output"] = 56
            _usage_sink["cost_usd_estimate"] = 0.001514
        return {"nombre": "X"}

    monkeypatch.setattr(kb_ingest, "_extract_with_haiku", _fake_extract)

    kb_ingest.ingest_url(
        "https://info.example.com/x/", slug="x-slug", dry_run=True
    )

    with get_session() as s:
        rows = list(
            s.scalars(select(KbIngestLog).order_by(KbIngestLog.id.desc()).limit(1))
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.slug == "x-slug"
        assert row.dry_run is True
        assert row.facts_count == 1
        assert row.tokens_input == 1234
        assert row.tokens_output == 56
        assert row.error_code is None
        assert float(row.cost_usd_estimate) > 0


# ---------------------------------------------------------------------------
# Rate-limit — 6ta llamada en <60s devuelve 429
# ---------------------------------------------------------------------------


def test_rate_limit_dispara_429_tras_5_calls_en_un_minuto(
    monkeypatch, client
):
    """El 6to POST a /admin/kb-facts/ingest-url debe devolver 429."""
    from iris_brain import server as server_mod

    # Aislar el deque del rate-limit (compartido global) entre tests.
    server_mod._ingest_call_times.clear()

    # Mockear ingest_url para que NO golpee Anthropic ni la red.
    monkeypatch.setattr(
        kb_ingest, "ingest_url",
        lambda url, slug=None, dry_run=False: {
            "ok": True, "dry_run": dry_run, "slug": "x", "url": url,
            "facts": {"nombre": "X"}, "diff": {}, "would_upsert": 1, "text_chars": 0,
        },
    )

    # Bypass admin auth en test vía dependency_overrides
    server_mod.app.dependency_overrides[server_mod.require_admin] = lambda: None
    try:
        payload = {"url": "https://info.example.com/x/", "dry_run": True}
        statuses = []
        for _ in range(6):
            r = client.post("/admin/kb-facts/ingest-url", json=payload)
            statuses.append(r.status_code)
        assert statuses[:5].count(200) == 5, f"primeras 5 != 200: {statuses}"
        assert statuses[5] == 429, f"6ta != 429: {statuses}"
    finally:
        server_mod._ingest_call_times.clear()
        server_mod.app.dependency_overrides.pop(server_mod.require_admin, None)
