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


def reboot_request_is_exact(argv: list[str]) -> bool:
    """The trusted reboot capability is exactly one flag, now and after future CLI additions."""
    return argv == ["--request-reboot-status"]


class AgentStatusReporter:
    """Post fixed lifecycle messages; never accept task or customer detail."""

    def __init__(self, client, channel: str, *, logger=None):
        self.client = client
        self.channel = channel
        self.logger = logger or logging.getLogger(__name__)
        self._ready_thread = None

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
        return self._post("🟢 Online and ready.")

    def ready_async(self) -> None:
        """Start delivery without delaying the newly bound HTTP listener."""
        if self.enabled:
            try:
                self._ready_thread = threading.Thread(target=self.ready, daemon=True)
                self._ready_thread.start()
            except RuntimeError:
                self._ready_thread = None
                self.logger.warning("Agent status delivery thread could not start")

    def fatal(self) -> bool:
        if self._ready_thread is not None:
            self._ready_thread.join(timeout=4)
            if self._ready_thread.is_alive():
                self.logger.warning("Fatal status skipped while ready delivery is still pending")
                return False
        return self._post(
            "🔴 Stopped after a fatal runtime error. "
            "No task or customer details were included; check the private service logs."
        )

    def reboot_needed(self) -> bool:
        return self._post(
            "🟡 Operator reboot needed. "
            "No task or customer details were included."
        )
