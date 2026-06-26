"""Async HTTP client wrapper around the Iris brain service.

All methods degrade gracefully: if the brain is unreachable we return an
empty payload plus ``brain_offline=True`` so the templates can show a
red banner instead of a 500 page.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import settings


class BrainClient:
    """Thin async wrapper over the brain HTTP API."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.BRAIN_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.BRAIN_TIMEOUT_SECONDS

    def _admin_headers(self) -> dict[str, str]:
        token = settings.IRIS_ADMIN_TOKEN
        return {"X-Iris-Admin-Token": token} if token else {}

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        admin: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._admin_headers() if admin else None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 401:
                    return {"items": [], "brain_offline": False, "admin_unauthorized": True}
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    return {"items": data, "brain_offline": False}
                if isinstance(data, dict):
                    data.setdefault("brain_offline", False)
                    return data
                return {"value": data, "brain_offline": False}
        except (httpx.HTTPError, ValueError):
            return {"items": [], "brain_offline": True}

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        admin: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._admin_headers() if admin else None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 401:
                    return {"brain_offline": False, "admin_unauthorized": True, "ok": False}
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
                if isinstance(data, dict):
                    data.setdefault("brain_offline", False)
                    return data
                return {"value": data, "brain_offline": False}
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
            except ValueError:
                body = {"error": exc.response.text[:500]}
            body.update({"ok": False, "brain_offline": False, "http_status": exc.response.status_code})
            return body
        except (httpx.HTTPError, ValueError):
            return {"brain_offline": True, "error": "brain_unreachable"}

    async def _put(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        admin: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._admin_headers() if admin else None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.put(url, json=payload, headers=headers)
                if resp.status_code == 401:
                    return {"brain_offline": False, "admin_unauthorized": True, "ok": False}
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
                if isinstance(data, dict):
                    data.setdefault("brain_offline", False)
                    return data
                return {"value": data, "brain_offline": False}
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
            except ValueError:
                body = {"error": exc.response.text[:500]}
            body.update({"ok": False, "brain_offline": False, "http_status": exc.response.status_code})
            return body
        except (httpx.HTTPError, ValueError):
            return {"brain_offline": True, "error": "brain_unreachable"}

    # --- Public API --------------------------------------------------

    async def list_contacts(
        self,
        q: str | None = None,
        kind: str | None = None,
        page: int = 1,
        page_size: int = 50,
        sort: str = "name",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size, "sort": sort}
        if q:
            params["q"] = q
        if kind:
            params["kind"] = kind
        return await self._get("/contacts", params=params)

    async def get_contact(self, phone: str) -> dict[str, Any]:
        # TODO(Owner): confirm exact brain route shape; assuming /contacts/{phone}.
        return await self._get(f"/contacts/{phone}")

    async def get_contact_media(self, phone: str) -> dict[str, Any]:
        """Expediente del contacto (CRM): documentos/imágenes que ha enviado."""
        return await self._get(f"/contacts/{phone}/media")

    async def list_tickets(self, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        return await self._get("/tickets", params=params)

    async def list_kb_facts(self) -> dict[str, Any]:
        data = await self._get("/kb-facts")
        # Brain devuelve {"kb_facts": [...]}; UI espera {"items": [...]}.
        if "items" not in data and "kb_facts" in data:
            data = {**data, "items": data["kb_facts"]}
        return data

    async def upsert_kb_fact(self, slug: str, key: str, value: str) -> dict[str, Any]:
        return await self._post(
            "/kb-facts",
            {"kb_slug": slug, "key": key, "value": value},
        )

    async def admin_kb_ingest_url(
        self, url: str, slug: str | None = None, dry_run: bool = True
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": url, "dry_run": dry_run}
        if slug:
            payload["slug"] = slug
        return await self._post(
            "/admin/kb-facts/ingest-url", payload, admin=True
        )

    async def admin_kb_ingest_selected(
        self, slug: str, facts: dict[str, str]
    ) -> dict[str, Any]:
        return await self._post(
            "/admin/kb-facts/ingest-selected",
            {"slug": slug, "facts": facts},
            admin=True,
        )

    async def admin_kb_ingest_log(self, limit: int = 50) -> dict[str, Any]:
        return await self._get(
            "/admin/kb-facts/log", params={"limit": limit}, admin=True
        )

    async def get_health(self) -> dict[str, Any]:
        return await self._get("/health")

    # --- Admin API ---------------------------------------------------

    async def admin_get_config(self) -> dict[str, Any]:
        return await self._get("/admin/config", admin=True)

    async def admin_update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._put("/admin/config", payload, admin=True)

    async def admin_rotate_key(self, api_key: str) -> dict[str, Any]:
        return await self._post("/admin/rotate-key", {"api_key": api_key}, admin=True)

    async def admin_regenerate_token(self) -> dict[str, Any]:
        return await self._post("/admin/regenerate-token", {}, admin=True)

    async def admin_get_soul(self) -> dict[str, Any]:
        return await self._get("/admin/soul", admin=True)

    async def admin_put_soul(self, text: str) -> dict[str, Any]:
        return await self._put("/admin/soul", {"text": text}, admin=True)

    async def admin_get_soul_backup(self, ts: str) -> dict[str, Any]:
        return await self._get(f"/admin/soul/backup/{ts}", admin=True)

    async def admin_reload_soul(self) -> dict[str, Any]:
        return await self._post("/admin/soul/reload", {}, admin=True)

    async def admin_tickets_live(self, since: str | None = None) -> dict[str, Any]:
        params = {"since": since} if since else None
        return await self._get("/admin/tickets/live", params=params, admin=True)

    async def admin_ticket_reply(self, ticket_id: str, body: str, close_after: bool) -> dict[str, Any]:
        return await self._post(
            f"/admin/tickets/{ticket_id}/reply",
            {"body": body, "close_after": close_after},
            admin=True,
        )

    async def admin_ticket_close(self, ticket_id: str) -> dict[str, Any]:
        # Note: close endpoint vive en brain bajo /tickets/{id}/close (no /admin)
        return await self._post(
            f"/tickets/{ticket_id}/close",
            {},
            admin=False,
        )

    async def admin_ticket_set_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        return await self._post(
            f"/tickets/{ticket_id}/status",
            {"status": status},
            admin=False,
        )

    async def admin_ticket_reassign(self, ticket_id: str, kind: str) -> dict[str, Any]:
        return await self._post(
            f"/admin/tickets/{ticket_id}/reassign-kind",
            {"kind": kind},
            admin=True,
        )

    async def admin_metrics_today(self) -> dict[str, Any]:
        return await self._get("/admin/metrics/today", admin=True)

    async def admin_health_components(self) -> dict[str, Any]:
        return await self._get("/admin/health/components", admin=True)


brain_client = BrainClient()
