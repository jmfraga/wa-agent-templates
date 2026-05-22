"""Tests para módulo media (Phase 1c).

Cubre: ingest_from_bytes, ingest_from_url (con httpx mock), find_media,
dedupe, whitelist, mime reject, size reject, soft_delete, caption parsing.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _tmp_storage(monkeypatch):
    """Aísla MEDIA_STORAGE_DIR a un tmp dir por test."""
    tmp = tempfile.mkdtemp(prefix="iris-media-test-")
    from iris_brain.config import settings as _s
    monkeypatch.setattr(_s, "MEDIA_STORAGE_DIR", Path(tmp), raising=False)
    yield tmp


# ---------------- ingest_from_bytes ----------------


def test_ingest_from_bytes_persiste_archivo_y_row():
    from iris_brain import media as m

    data = b"\x89PNG\r\n\x1a\n" + b"x" * 100  # fake PNG bytes
    r = m.ingest_from_bytes(
        data=data,
        mime_type="image/png",
        source="ui_upload",
        label="test-png",
        tags=["test", "png"],
    )
    assert r["id"] is not None
    assert r["mime_type"] == "image/png"
    assert r["source"] == "ui_upload"
    assert r["label"] == "test-png"
    assert r["dedupe"] is False
    # Archivo existe en disco
    got = m.get_storage_path(r["id"])
    assert got is not None
    sp, mime, fname = got
    assert Path(sp).exists()
    assert Path(sp).read_bytes() == data


def test_ingest_from_bytes_dedupe_por_sha256():
    from iris_brain import media as m

    data = b"identical-bytes-payload"
    r1 = m.ingest_from_bytes(data=data, mime_type="image/png", source="ui_upload", label="A")
    r2 = m.ingest_from_bytes(data=data, mime_type="image/png", source="telegram", label="B")
    assert r1["id"] == r2["id"]
    assert r2["dedupe"] is True


def test_ingest_rechaza_mime_invalido():
    from iris_brain import media as m

    with pytest.raises(m.MediaError) as exc:
        m.ingest_from_bytes(data=b"hello", mime_type="text/html", source="ui_upload")
    assert exc.value.code == "bad_mime"


def test_ingest_rechaza_size_excedido(monkeypatch):
    from iris_brain import media as m
    from iris_brain.config import settings as _s

    monkeypatch.setattr(_s, "MEDIA_MAX_BYTES", 100, raising=False)
    with pytest.raises(m.MediaError) as exc:
        m.ingest_from_bytes(
            data=b"x" * 200, mime_type="image/png", source="ui_upload"
        )
    assert exc.value.code == "too_large"


def test_ingest_rechaza_vacio():
    from iris_brain import media as m

    with pytest.raises(m.MediaError) as exc:
        m.ingest_from_bytes(data=b"", mime_type="image/png", source="ui_upload")
    assert exc.value.code == "empty"


# ---------------- ingest_from_url ----------------


def _mock_httpx_get(data: bytes, content_type: str = "image/jpeg"):
    """Helper: patch httpx.Client para devolver `data` con content-type."""
    fake_resp = MagicMock()
    fake_resp.content = data
    fake_resp.headers = {"content-type": content_type}
    fake_resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.get = MagicMock(return_value=fake_resp)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=fake_client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_ingest_from_url_whitelist_ok():
    from iris_brain import media as m

    data = b"fake-jpeg-bytes" + b"\xff" * 50
    cm = _mock_httpx_get(data, "image/jpeg")
    with patch("iris_brain.media.httpx.Client", return_value=cm):
        r = m.ingest_from_url(
            "https://marketing.simacademy.lat/promo.jpg",
            label="Promo ACLS",
        )
    assert r["id"] is not None
    assert r["mime_type"] == "image/jpeg"
    assert r["source"] == "marketing"
    assert r["label"] == "Promo ACLS"
    assert r["origin_url"] == "https://marketing.simacademy.lat/promo.jpg"


def test_ingest_from_url_rechaza_fuera_de_whitelist():
    from iris_brain import media as m

    with pytest.raises(m.MediaError) as exc:
        m.ingest_from_url("https://random.example.com/img.jpg", label="evil")
    assert exc.value.code == "not_whitelisted"


def test_ingest_from_url_disable_whitelist():
    """Para fuente WA owner pasamos enforce_whitelist=False."""
    from iris_brain import media as m

    data = b"baileys-internal-binary" + b"\xff" * 30
    cm = _mock_httpx_get(data, "image/jpeg")
    with patch("iris_brain.media.httpx.Client", return_value=cm):
        r = m.ingest_from_url(
            "http://baileys-internal.local/123",
            label="WA Promo",
            source="whatsapp",
            enforce_whitelist=False,
        )
    assert r["source"] == "whatsapp"


# ---------------- find_media ----------------


def test_find_media_busca_por_label():
    from iris_brain import media as m

    m.ingest_from_bytes(data=b"a" * 50, mime_type="image/png", source="ui_upload", label="Promo ACLS Verano")
    m.ingest_from_bytes(data=b"b" * 50, mime_type="image/png", source="ui_upload", label="Promo BLS Otoño")
    m.ingest_from_bytes(data=b"c" * 50, mime_type="image/png", source="ui_upload", label="Banner congresos")

    r = m.find_media("ACLS")
    assert r["found"]
    assert r["count"] == 1
    assert r["items"][0]["label"] == "Promo ACLS Verano"


def test_find_media_busca_por_tag():
    from iris_brain import media as m

    m.ingest_from_bytes(
        data=b"x" * 50, mime_type="image/png", source="ui_upload",
        label="Foo", tags=["acls", "promo"],
    )
    r = m.find_media("acls")
    assert r["found"]
    assert r["count"] >= 1


def test_find_media_excluye_soft_deleted():
    from iris_brain import media as m

    r1 = m.ingest_from_bytes(data=b"y" * 50, mime_type="image/png", source="ui_upload", label="ToDelete")
    m.soft_delete(r1["id"])
    r = m.find_media("ToDelete")
    assert r["count"] == 0


# ---------------- caption parsing ----------------


def test_looks_like_ingest_caption_positivos():
    from iris_brain import media as m

    assert m.looks_like_ingest_caption("guarda como promo ACLS")
    assert m.looks_like_ingest_caption("guardar como flyer julio")
    assert m.looks_like_ingest_caption("save as ACLS_promo")
    assert m.looks_like_ingest_caption("guarda como promo X #acls #verano")


def test_looks_like_ingest_caption_negativos():
    from iris_brain import media as m

    assert not m.looks_like_ingest_caption("")
    assert not m.looks_like_ingest_caption("mira esta foto")
    assert not m.looks_like_ingest_caption("guarda esto para luego")


def test_parse_ingest_caption_extrae_label_y_tags():
    from iris_brain import media as m

    label, tags = m.parse_ingest_caption("guarda como Promo Verano 2026 #acls #promo")
    assert label == "Promo Verano 2026"
    assert set(tags) == {"acls", "promo"}


# ---------------- soft_delete ----------------


def test_soft_delete_borra_archivo_y_marca_row():
    from iris_brain import media as m

    r = m.ingest_from_bytes(
        data=b"z" * 50, mime_type="image/png", source="ui_upload", label="DelMe",
    )
    info = m.get_storage_path(r["id"])
    assert info is not None
    sp = Path(info[0])
    assert sp.exists()
    out = m.soft_delete(r["id"])
    assert out["ok"]
    assert not sp.exists()
    # get_media excluye soft-deleted
    assert m.get_media(r["id"]) is None
