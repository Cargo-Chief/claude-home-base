"""Pure parsing and routing rules for named-approver provider controls."""

from __future__ import annotations

import re


PROVIDERS = {"claude", "openai"}


def parse_provider_command(text: str) -> str | None:
    """Return an exact provider control action, ignoring an optional Slack mention."""
    normalized = re.sub(r"<@[A-Z0-9]+>", "", text).strip().lower()
    match = re.fullmatch(r"provider (status|claude|openai|auto)", normalized)
    return match.group(1) if match else None


def use_openai_provider(
    override: str | None, *, limit_paused: bool, has_openai_session: bool,
) -> bool:
    """Resolve an explicit per-thread choice before automatic fallback state."""
    if override == "openai":
        return True
    if override == "claude":
        return False
    return limit_paused or has_openai_session


def format_provider_audit(
    *, user: str, channel: str, thread: str, action: str, provider: str,
) -> str:
    """Build a content-free provider-control audit record."""
    return (
        f"PROVIDER_CONTROL | USER:{user} | CHANNEL:{channel} | THREAD:{thread} "
        f"| ACTION:{action} | PROVIDER:{provider}"
    )
