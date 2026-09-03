"""Privacy-safe lifecycle reporting for long-running agents."""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
from pathlib import Path


CHANNEL_ID_RE = re.compile(r"[CG][A-Z0-9]+")


def configured_status_channel(env: dict[str, str] | None = None) -> str:
    """Return the optional configured Slack channel ID, rejecting unsafe values."""
    values = os.environ if env is None else env
    channel = values.get("AGENT_STATUS_CHANNEL_ID", "").strip()
    if channel and not CHANNEL_ID_RE.fullmatch(channel):
        raise ValueError("AGENT_STATUS_CHANNEL_ID must be a Slack channel ID beginning with C or G")
    return channel


def source_revision(source_dir: Path) -> str:
    """Return a short source revision without making startup depend on Git metadata."""
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        revision = result.stdout.strip()
        return revision if re.fullmatch(r"[0-9a-fA-F]{7,40}", revision) else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class AgentStatusReporter:
    """Post fixed lifecycle messages; never accept task or customer detail."""

    def __init__(self, client, channel: str, agent_name: str, source_dir: Path, *, logger=None):
        self.client = client
        self.channel = channel
        self.agent_name = agent_name.strip() or "Agent"
        self.source_dir = source_dir
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
        host = socket.gethostname().split(".", 1)[0] or "unknown"
        revision = source_revision(self.source_dir)
        return self._post(
            f"🟢 {self.agent_name} is online and ready · host `{host}` · revision `{revision}`"
        )

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
