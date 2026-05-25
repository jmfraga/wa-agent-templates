"""Tests para kb_ingest: whitelist, slug derive, parser Haiku, dry_run."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from iris_brain import kb_ingest


# ---------------------------------------------------------------------------
# is_whitelisted
# ---------------------------------------------------------------------------


def test_is_whitelisted_acepta_dominios_conocidos():
    assert kb_ingest.is_whitelisted("https://info.simacademy.lat/curso/") is True
    assert kb_ingest.is_whitelisted("https://blog.emergencias.com.mx/post") is True
    assert kb_ingest.is_whitelisted("https://marketing.simacademy.lat/promo.jpg") is True
    # http también vale (mismo host)
    assert kb_ingest.is_whitelisted("http://info.simacademy.lat/") is True
    # dominio externo NO vale, aunque incluya el host en la query
    assert kb_ingest.is_whitelisted("https://evil.com/?host=info.simacademy.lat") is False
    # input basura
    assert kb_ingest.is_whitelisted("not-a-url") is False


# ---------------------------------------------------------------------------
# derive_slug
# ---------------------------------------------------------------------------


def test_derive_slug_casos():
    assert (
        kb_ingest.derive_slug("https://info.simacademy.lat/has-magia-con-claude/")
        == "has-magia-con-claude"
    )
    assert kb_ingest.derive_slug("https://info.simacademy.lat/curso-acls") == "curso-acls"
    assert (
        kb_ingest.derive_slug("https://info.simacademy.lat/cursos/avanzado/") == "avanzado"
    )
    assert kb_ingest.derive_slug("https://info.simacademy.lat/page.html") == "page"


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
    data = kb_ingest._extract_with_haiku("https://info.simacademy.lat/x/", "Texto fixture")
    assert data == {
        "nombre": "Curso ACLS",
        "precio": "$3500",
        "modalidad": "presencial",
    }


def test_extract_with_haiku_parsea_json_directo(monkeypatch):
    raw = '{"nombre": "Curso BLS", "precio": "$1500"}'
    _patch_anthropic_with_text(monkeypatch, raw)
    data = kb_ingest._extract_with_haiku("https://info.simacademy.lat/y/", "Texto")
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
        lambda url, text: {"nombre": "X", "precio": "$100"},
    )
    r = kb_ingest.ingest_url(
        "https://info.simacademy.lat/x/", slug="x-slug", dry_run=True
    )
    assert r["ok"] is True
    assert r["dry_run"] is True
    assert r["slug"] == "x-slug"
    assert r["facts"] == {"nombre": "X", "precio": "$100"}
    assert r["would_upsert"] == 2
    assert "diff" in r
