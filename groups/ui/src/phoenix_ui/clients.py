"""Clientes HTTP al brain y al listener."""
import httpx

from .config import top

_BRAIN_TIMEOUT = 10.0
_LISTENER_TIMEOUT = 5.0


def brain() -> httpx.Client:
    return httpx.Client(base_url=top.phoenix_brain_url, timeout=_BRAIN_TIMEOUT)


def listener() -> httpx.Client:
    return httpx.Client(base_url=top.phoenix_listener_url, timeout=_LISTENER_TIMEOUT)
