"""Decide si Phoenix responde un mensaje, según mode del grupo."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class GatingDecision:
    respond: bool
    reason: str  # human-readable


def decide(
    mode: Optional[str],
    *,
    is_owner: bool,
    mentions_phoenix: bool,
    quoted_is_phoenix: bool,
    text: str,
) -> GatingDecision:
    """Gating mínimo. mode None = DM (no grupo)."""

    # DM al owner: siempre responde (sin grupo).
    if mode is None:
        if is_owner:
            return GatingDecision(True, "owner_dm")
        return GatingDecision(False, "non_owner_dm_ignored")

    if mode == "on_command_only":
        if is_owner and text.strip().startswith(("/phoenix", "@phoenix")):
            return GatingDecision(True, "owner_command")
        return GatingDecision(False, "command_only_not_owner")

    if mode == "lurker":
        if mentions_phoenix or quoted_is_phoenix or is_owner:
            return GatingDecision(True, "mention" if mentions_phoenix else ("reply_to_phoenix" if quoted_is_phoenix else "owner_in_group"))
        return GatingDecision(False, "lurker_silent")

    if mode == "proactive":
        # Mention/owner/reply: respond directo, igual que lurker (no necesitamos clasificar).
        if mentions_phoenix or quoted_is_phoenix or is_owner:
            return GatingDecision(True, "mention" if mentions_phoenix else ("reply_to_phoenix" if quoted_is_phoenix else "owner_in_group"))
        # Sin mention: sentinel para que chat.py corra el clasificador proactivo.
        return GatingDecision(False, "proactive_pending")

    return GatingDecision(False, f"unknown_mode_{mode}")
