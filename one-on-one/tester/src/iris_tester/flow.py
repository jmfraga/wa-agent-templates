"""Escenarios pre-armados para el tester.

Lee un YAML con secuencia de pasos y assertions, ejecuta contra el brain
y reporta resultado. Cada paso es:

  - `send: "texto"` → POST /chat
  - `jmf_reply: "texto"` → POST /jmf/reply (simula a OWNER contestando)
  - `expect:` con keys: intent, ticket_created, reply_contains, ticket_status

Ejemplo de uso programático:

    from iris_tester.flow import run_scenario
    result = run_scenario("scenarios/01-saludo.yaml", "http://localhost:8096")
    print(result.summary())
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from rich.console import Console

console = Console()


@dataclass
class StepResult:
    step_index: int
    kind: str  # "send" | "jmf_reply"
    ok: bool
    failures: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    steps: list[StepResult] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Scenario: {self.name} → {'PASS' if self.ok else 'FAIL'}"]
        for s in self.steps:
            mark = "✓" if s.ok else "✗"
            lines.append(f"  {mark} step {s.step_index} ({s.kind})")
            for f in s.failures:
                lines.append(f"      ! {f}")
        return "\n".join(lines)


def _extract_intent(resp: dict) -> Optional[str]:
    return resp.get("intent") or (resp.get("meta") or {}).get("intent")


def _extract_ticket_id(resp: dict) -> Optional[str]:
    for key in ("escalate", "ticket"):
        val = resp.get(key)
        if isinstance(val, dict) and val.get("ticket_id"):
            return val["ticket_id"]
    return resp.get("ticket_id")


def _check_expect(expect: dict, resp: dict, ticket: Optional[dict]) -> list[str]:
    """Valida assertions tolerantes. Devuelve lista de fallas (vacía = ok)."""
    failures: list[str] = []

    if "intent" in expect:
        actual = _extract_intent(resp)
        if actual != expect["intent"]:
            failures.append(f"intent: expected={expect['intent']!r} actual={actual!r}")

    if "ticket_created" in expect:
        has_ticket = _extract_ticket_id(resp) is not None
        if has_ticket != bool(expect["ticket_created"]):
            failures.append(
                f"ticket_created: expected={expect['ticket_created']} actual={has_ticket}"
            )

    if "reply_contains" in expect:
        reply = (resp.get("reply") or "").lower()
        needles = expect["reply_contains"]
        if isinstance(needles, str):
            needles = [needles]
        for needle in needles:
            if needle.lower() not in reply:
                failures.append(f"reply_contains: missing {needle!r}")

    if "ticket_status" in expect:
        if ticket is None:
            failures.append(f"ticket_status: expected={expect['ticket_status']} but no ticket")
        else:
            actual = ticket.get("status")
            if actual != expect["ticket_status"]:
                failures.append(
                    f"ticket_status: expected={expect['ticket_status']!r} actual={actual!r}"
                )

    return failures


def _post_chat(client: httpx.Client, brain_url: str, phone: str, name: str, text: str) -> dict:
    payload = {"contact_phone": phone, "text": text}
    if name:
        payload["contact_name"] = name
    r = client.post(f"{brain_url}/chat", json=payload)
    r.raise_for_status()
    return r.json()


def _post_jmf_reply(
    client: httpx.Client, brain_url: str, ticket_id: str, contact_phone: str, body: str
) -> dict:
    payload = {
        "ticket_id": ticket_id,
        "contact_phone": contact_phone,
        "text": body,
    }
    r = client.post(f"{brain_url}/jmf/reply", json=payload)
    r.raise_for_status()
    return r.json()


def _get_ticket(client: httpx.Client, brain_url: str, ticket_id: str) -> Optional[dict]:
    try:
        r = client.get(f"{brain_url}/tickets/{ticket_id}")
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError:
        return None


def run_scenario(
    yaml_path: str | Path,
    brain_url: str,
    auto_jmf_response: Optional[str] = None,
    settle_ms: int = 250,
) -> ScenarioResult:
    """Ejecuta un escenario YAML.

    Args:
        yaml_path: ruta al YAML.
        brain_url: brain base URL.
        auto_jmf_response: si se da, cualquier ticket abierto recibe esta
            respuesta automáticamente vía POST /jmf/reply (override del
            campo `jmf_reply` del YAML).
        settle_ms: pausa entre pasos para que el brain procese.
    """
    data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    name = data.get("name", str(yaml_path))
    phone = data["phone"]
    contact_name = data.get("name_contact", "")
    steps = data.get("steps", [])

    result = ScenarioResult(name=name, ok=True)
    current_ticket_id: Optional[str] = None

    with httpx.Client(timeout=30.0) as client:
        for idx, step in enumerate(steps):
            time.sleep(settle_ms / 1000)
            expect = step.get("expect", {}) or {}

            if "send" in step:
                try:
                    resp = _post_chat(client, brain_url, phone, contact_name, step["send"])
                except httpx.HTTPError as err:
                    sr = StepResult(idx, "send", ok=False, failures=[f"HTTP: {err}"])
                    result.steps.append(sr)
                    result.ok = False
                    continue

                ticket_id = _extract_ticket_id(resp)
                if ticket_id:
                    current_ticket_id = ticket_id

                ticket_obj = (
                    _get_ticket(client, brain_url, current_ticket_id)
                    if current_ticket_id and "ticket_status" in expect
                    else None
                )

                failures = _check_expect(expect, resp, ticket_obj)
                sr = StepResult(
                    idx,
                    "send",
                    ok=not failures,
                    failures=failures,
                    detail={"response": resp, "ticket": ticket_obj},
                )
                result.steps.append(sr)
                if failures:
                    result.ok = False

            elif "jmf_reply" in step:
                body = auto_jmf_response or step["jmf_reply"]
                if not current_ticket_id:
                    sr = StepResult(
                        idx,
                        "jmf_reply",
                        ok=False,
                        failures=["no current ticket to reply to"],
                    )
                    result.steps.append(sr)
                    result.ok = False
                    continue
                try:
                    resp = _post_jmf_reply(
                        client, brain_url, current_ticket_id, phone, body
                    )
                except httpx.HTTPError as err:
                    sr = StepResult(idx, "jmf_reply", ok=False, failures=[f"HTTP: {err}"])
                    result.steps.append(sr)
                    result.ok = False
                    continue

                ticket_obj = _get_ticket(client, brain_url, current_ticket_id)
                failures = _check_expect(expect, resp, ticket_obj)
                sr = StepResult(
                    idx,
                    "jmf_reply",
                    ok=not failures,
                    failures=failures,
                    detail={"response": resp, "ticket": ticket_obj},
                )
                result.steps.append(sr)
                if failures:
                    result.ok = False

            else:
                sr = StepResult(
                    idx, "unknown", ok=False, failures=[f"unrecognized step keys: {list(step)}"]
                )
                result.steps.append(sr)
                result.ok = False

    return result


def run_scenario_cli(yaml_path: str, brain_url: str) -> int:
    res = run_scenario(yaml_path, brain_url)
    console.print(res.summary())
    return 0 if res.ok else 1
