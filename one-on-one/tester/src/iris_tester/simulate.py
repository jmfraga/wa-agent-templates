"""CLI para simular un paciente conversando con el brain de Iris.

Ejemplo:
    python -m iris_tester.simulate \\
        --brain http://localhost:8096 \\
        --phone +5215512345678 \\
        --name "María Tester" \\
        --message "Hola, quiero agendar una consulta"

Hace POST al brain `/chat`, imprime respuesta + intent + ticket_id. Si hay
ticket, hace polling cada 2s al `/tickets/{id}` hasta que el status sea
`awaiting_patient` o `closed`. Imprime resumen final.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    add_completion=False,
    help="Simula un paciente contra el brain de Iris.",
)
console = Console()


def _post_chat(
    brain_url: str,
    phone: str,
    name: Optional[str],
    message: str,
    timeout: float = 30.0,
) -> dict:
    """POST al brain /chat. Devuelve el JSON parseado."""
    payload = {
        "contact_phone": phone,
        "text": message,
    }
    # TODO(OWNER): confirmar si el contrato de /chat acepta `contact_name`
    # como hint para crear contactos. Lo mandamos por si acaso; el brain
    # puede ignorarlo.
    if name:
        payload["contact_name"] = name
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{brain_url}/chat", json=payload)
        r.raise_for_status()
        return r.json()


def _get_ticket(brain_url: str, ticket_id: str, timeout: float = 10.0) -> dict:
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{brain_url}/tickets/{ticket_id}")
        r.raise_for_status()
        return r.json()


def _poll_ticket(
    brain_url: str,
    ticket_id: str,
    interval: float = 2.0,
    max_wait: float = 120.0,
) -> dict:
    """Polling hasta status terminal (`awaiting_patient` | `closed`) o timeout."""
    deadline = time.time() + max_wait
    terminal = {"awaiting_patient", "closed"}
    last: dict = {}
    while time.time() < deadline:
        try:
            last = _get_ticket(brain_url, ticket_id)
        except httpx.HTTPError as err:
            console.print(f"[yellow]ticket poll error:[/yellow] {err}")
            time.sleep(interval)
            continue
        status = last.get("status")
        console.print(f"  [dim]ticket {ticket_id} status={status}[/dim]")
        if status in terminal:
            return last
        time.sleep(interval)
    console.print("[yellow]ticket poll timeout[/yellow]")
    return last


@app.command()
def main(
    brain: str = typer.Option("http://localhost:8096", help="Brain HTTP base URL"),
    phone: str = typer.Option(..., help="Teléfono E.164 del paciente simulado"),
    name: Optional[str] = typer.Option(None, help="Nombre del paciente"),
    message: str = typer.Option(..., help="Mensaje a enviar"),
    wait_ticket: bool = typer.Option(
        True, help="Si hay ticket, hacer polling hasta status terminal."
    ),
    poll_interval: float = typer.Option(2.0, help="Segundos entre polls."),
    poll_timeout: float = typer.Option(120.0, help="Timeout total del polling."),
) -> None:
    """Envía un mensaje al brain y muestra la respuesta + ciclo de ticket."""
    console.print(
        Panel.fit(
            f"[bold]Paciente:[/bold] {name or '(sin nombre)'} <{phone}>\n"
            f"[bold]Brain:[/bold] {brain}\n"
            f"[bold]Mensaje:[/bold] {message}",
            title="iris-tester",
        )
    )

    try:
        resp = _post_chat(brain, phone, name, message)
    except httpx.HTTPError as err:
        console.print(f"[red]brain /chat falló:[/red] {err}")
        raise typer.Exit(code=1)

    reply = resp.get("reply", "")
    intent = resp.get("intent") or resp.get("meta", {}).get("intent")
    ticket = resp.get("escalate") or resp.get("ticket")
    ticket_id = (ticket or {}).get("ticket_id") if isinstance(ticket, dict) else None

    table = Table(show_header=False, box=None)
    table.add_row("[bold]reply[/bold]", reply or "[dim](vacío)[/dim]")
    table.add_row("[bold]intent[/bold]", str(intent))
    table.add_row("[bold]ticket_id[/bold]", str(ticket_id))
    console.print(table)

    if ticket_id and wait_ticket:
        console.print(f"\n[bold]Esperando ticket {ticket_id}...[/bold]")
        final = _poll_ticket(brain, ticket_id, poll_interval, poll_timeout)
        console.print(Panel(str(final), title=f"ticket final ({ticket_id})"))


if __name__ == "__main__":
    app()
