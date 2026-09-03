"""Privacy-safe lifecycle reporting for long-running agents."""

from __future__ import annotations

import logging
import os
import re
import threading


CHANNEL_ID_RE = re.compile(r"[CG][A-Z0-9]+")


def configured_status_channel(env: dict[str, str] | None = None) -> str:
    """Return the optional configured Slack channel ID, rejecting unsafe values."""
    values = os.environ if env is None else env
    channel = values.get("AGENT_STATUS_CHANNEL_ID", "").strip()
    if channel and not CHANNEL_ID_RE.fullmatch(channel):
        raise ValueError("AGENT_STATUS_CHANNEL_ID must be a Slack channel ID beginning with C or G")
    return channel


class AgentStatusReporter:
    """Post fixed lifecycle messages; never accept task or customer detail."""

    def __init__(self, client, channel: str, agent_name: str, *, logger=None):
        self.client = client
        self.channel = channel
        configured_name = agent_name.strip()
        self.agent_name = (
            configured_name
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", configured_name)
            else "Agent"
        )
        self.logger = logger or logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return bool(self.channel)

    def _post(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            self.client.chat_postMessage(channel=self.channel, text=text)
            return True
        except Exception as exc:
            # Log only the exception class. Slack errors can contain request details.
            self.logger.warning("Agent status delivery failed (%s)", type(exc).__name__)
            return False

    def ready(self) -> bool:
        return self._post(f"🟢 {self.agent_name} is online and ready.")

    def ready_async(self) -> None:
        """Start delivery without delaying the newly bound HTTP listener."""
        if self.enabled:
            threading.Thread(target=self.ready, daemon=True).start()

    def fatal(self) -> bool:
        return self._post(
            f"🔴 {self.agent_name} stopped after a fatal runtime error. "
            "No task or customer details were included; check the private service logs."
        )

    def reboot_needed(self) -> bool:
        return self._post(
            f"🟡 {self.agent_name} needs an operator reboot. "
            "No task or customer details were included."
        )
