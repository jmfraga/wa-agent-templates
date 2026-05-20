"""Tests para tools.py — schema, execute(), to_text()."""
from __future__ import annotations

import json

import pytest

from iris_brain import tools
from iris_brain.db import get_session
from iris_brain.models import KbFact, KbFactSource


def _seed_facts():
    with get_session() as s:
        s.add_all([
            KbFact(
                kb_slug="blsacls",
                key="precio_mxn",
                value="$4,500 MXN + IVA",
                source=KbFactSource.jmf,
            ),
            KbFact(
                kb_slug="blsacls",
                key="landing_url",
                value="https://example.com/service-b/blsacls",
                source=KbFactSource.jmf,
            ),
            KbFact(
                kb_slug="eusim2",
                key="fechas",
                value="6 sesiones, jueves 7-9 pm",
                source=KbFactSource.jmf,
            ),
        ])


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_tools_schema_anthropic_shape():
    """Cada tool debe tener name, description, input_schema válido (JSON Schema object)."""
    names = {t["name"] for t in tools.TOOLS}
    assert "lookup_kb_fact" in names
    assert "list_kb_facts" in names
    assert "lookup_contact" in names
    assert "open_ticket" in names

    for t in tools.TOOLS:
        assert isinstance(t["name"], str) and t["name"]
        assert isinstance(t["description"], str) and len(t["description"]) > 20
        schema = t["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema and isinstance(schema["properties"], dict)
        assert "required" in schema and isinstance(schema["required"], list)


def test_lookup_kb_fact_description_lists_known_slugs():
    """La description debe mencionar slugs reales para que Haiku elija bien."""
    tool = next(t for t in tools.TOOLS if t["name"] == "lookup_kb_fact")
    desc = tool["description"].lower()
    for slug in ["blsacls", "eusim2", "scpa", "_global", "has-magia-con-claude"]:
        assert slug.lower() in desc, f"slug {slug} missing from description"


# ---------------------------------------------------------------------------
# _lookup_kb_fact
# ---------------------------------------------------------------------------


def test_lookup_kb_fact_returns_dict():
    _seed_facts()
    r = tools._lookup_kb_fact("blsacls", "precio_mxn")
    assert isinstance(r, dict)
    assert r["found"] is True
    assert r["kb_slug"] == "blsacls"
    assert r["key"] == "precio_mxn"
    assert r["value"] == "$4,500 MXN + IVA"


def test_lookup_kb_fact_unknown_slug_returns_empty():
    _seed_facts()
    r = tools._lookup_kb_fact("nope", "precio_mxn")
    assert r["found"] is False
    assert r["kb_slug"] == "nope"


def test_lookup_kb_fact_unknown_key_returns_empty():
    _seed_facts()
    r = tools._lookup_kb_fact("blsacls", "no_existe_key")
    assert r["found"] is False


# ---------------------------------------------------------------------------
# _list_kb_facts
# ---------------------------------------------------------------------------


def test_list_kb_facts_returns_all_slugs():
    _seed_facts()
    r = tools._list_kb_facts()
    assert r["found"] is True
    assert r["count"] == 3
    slugs = {item["kb_slug"] for item in r["items"]}
    assert {"blsacls", "eusim2"}.issubset(slugs)


def test_list_kb_facts_filter_by_slug():
    _seed_facts()
    r = tools._list_kb_facts(kb_slug="blsacls")
    assert r["count"] == 2
    assert all(item["kb_slug"] == "blsacls" for item in r["items"])


def test_list_kb_facts_empty_db():
    r = tools._list_kb_facts()
    assert r["found"] is False
    assert r["count"] == 0
    assert r["items"] == []


# ---------------------------------------------------------------------------
# execute() routing
# ---------------------------------------------------------------------------


def test_execute_routes_lookup_kb_fact():
    _seed_facts()
    r = tools.execute("lookup_kb_fact", {"kb_slug": "blsacls", "key": "precio_mxn"})
    assert r["found"] is True


def test_execute_routes_list_kb_facts():
    _seed_facts()
    r = tools.execute("list_kb_facts", {})
    assert r["found"] is True


def test_execute_unknown_tool_returns_error():
    r = tools.execute("does_not_exist", {})
    assert "error" in r


def test_execute_missing_arg_returns_error():
    r = tools.execute("lookup_kb_fact", {"kb_slug": "blsacls"})  # missing key
    assert "error" in r


# ---------------------------------------------------------------------------
# to_text
# ---------------------------------------------------------------------------


def test_to_text_serializes_dict():
    out = tools.to_text({"found": True, "value": "hola"})
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["found"] is True
    assert parsed["value"] == "hola"


def test_to_text_preserves_unicode():
    out = tools.to_text({"value": " — sesión jueves"})
    # ensure_ascii=False: el texto debe verse legible
    assert "" in out
    assert "sesión" in out
