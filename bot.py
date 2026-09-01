#!/usr/bin/env python3
"""
Your AI Employee — Slack bot powered by Claude Code.

HTTP Events API version (production-standard). Uses Flask + Cloudflare Tunnel
instead of Socket Mode. Slack sends stateless HTTP POSTs to your public URL.

Key difference from Socket Mode: Slack requires a 200 response within 3 seconds.
Claude Code calls take minutes, so we respond immediately and process in a
background thread, posting the result when ready.

Also supports proactive messaging and read access via CLI:
    python bot.py --send USER_ID "message"
    python bot.py --channel "#general" "message"
    echo '{"result":"..."}' | python bot.py --send-result USER_ID
    python bot.py --history CHANNEL_ID [--limit 50] [--thread THREAD_TS]
    python bot.py --find-channel SUBSTRING
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_sdk import WebClient

from cargo_chief_safety import (
    AuthorityPolicy,
    RuntimePolicy,
    SafetyError,
    build_authority_envelope,
    build_claude_command,
    build_codex_command,
    build_codex_prompt,
    cleanup_thread_workspaces,
    consume_bundle_claim,
    consume_parking_claim,
    find_workspace_root,
    format_audit_metadata,
    preflight_room,
    resolve_room_policy,
    prepare_thread_workspace,
    private_escalation_status,
    resolve_thread_bundle,
    ThreadWorkspace,
    validate_secret_env_path,
    validate_codex_runtime,
    write_private_json,
)
from slack_prompting import (
    contains_escalation_file_write,
    needs_relevance_prefix,
    relevance_prefix,
)
from http_server import serve_http
from delegation_observability import DelegationTracker, format_delegation_audit
from openai_fallback import (
    is_claude_limit_notice,
    model_notice_text,
    run_codex_turn,
)
from governed_delegation import budget_status, update_budget

# ---------------------------------------------------------------------------
# External configuration and controlled runtime storage
# ---------------------------------------------------------------------------

SOURCE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = find_workspace_root(SOURCE_DIR)
ENV_FILE = Path(
    os.environ.get(
        "CARGO_CHIEF_ENV_FILE",
        str(Path.home() / ".config/cargo-chief/home-base.env"),
    )
).expanduser()
ENV_FILE = validate_secret_env_path(ENV_FILE, workspace_root=WORKSPACE_ROOT)
load_dotenv(dotenv_path=ENV_FILE)

os.umask(0o077)
RUNTIME_POLICY = RuntimePolicy.from_env(source_dir=SOURCE_DIR, workspace_root=WORKSPACE_ROOT)
RUNTIME_POLICY.prepare()
LOG_DIR = RUNTIME_POLICY.log_dir
STATE_DIR = RUNTIME_POLICY.state_dir
TEMP_DIR = RUNTIME_POLICY.temp_dir

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")

_rotating_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "bot.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_rotating_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_rotating_handler)

AUDIT_LOG = LOG_DIR / "audit.log"
audit_handler = logging.handlers.RotatingFileHandler(
    AUDIT_LOG,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
audit_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
audit_logger = logging.getLogger("bot.audit")
audit_logger.addHandler(audit_handler)
audit_logger.setLevel(logging.INFO)

for private_log in (LOG_DIR / "bot.log", AUDIT_LOG):
    private_log.chmod(0o600)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
CLAUDE_TIMEOUT = RUNTIME_POLICY.claude_timeout

# Channel filtering — only respond in channels whose names contain one of these substrings.
# Applies to both public and private channels. Sender authorization is a
# separate mandatory gate for every channel type.
ALLOWED_CHANNEL_SUBSTRINGS = tuple(
    p.strip().lower() for p in os.environ.get("ALLOWED_CHANNELS", "").split(",") if p.strip()
)

# Trust battery — optional. Set to a directory containing per-user JSON battery files.
TRUST_BATTERY_DIR = os.environ.get("TRUST_BATTERY_DIR", "")

MAX_SLACK_MSG_LEN = 3900
PORT = int(os.environ.get("PORT", "3000"))

# Per-channel/DM model + effort and per-model prompts live in model-config.json
# next to this script (start from model-config.json.example; the file explorer's
# /models page is a friendly editor). Read fresh on every claude spawn — no bot
# restart needed. No entry for a room → the config's own "default_model"; no
# default_model either → the CLI default from ~/.claude/settings.json.
MODEL_CONFIG_FILE = Path(__file__).resolve().parent / "model-config.json"
DEFAULT_EFFORT = "medium"
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _load_model_config() -> dict:
    try:
        return json.loads(MODEL_CONFIG_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:  # corrupt JSON must not take the bot down
        logger.warning(f"model-config.json unreadable ({e}); using CLI defaults")
        return {}


def _settings_default_model() -> str:
    """The model claude falls back to when --model is absent (~/.claude/settings.json)."""
    try:
        return json.loads((Path.home() / ".claude" / "settings.json").read_text()).get("model", "") or ""
    except Exception:
        return ""


def _resolve_entry(channel_id: str, user_id: str) -> dict:
    """The merged model-config entry for a room: channel entry, with a DM
    per-user entry layered on top (its non-empty keys win)."""
    cfg = _load_model_config()
    entry = dict(cfg.get("channels", {}).get(channel_id, {}))
    if channel_id.startswith("D"):
        entry.update({k: v for k, v in cfg.get("dm_users", {}).get(user_id, {}).items() if v})
    return entry


def _effective_model(cfg: dict, entry: dict) -> str:
    """The model a room actually gets: its own, else the config's default_model,
    else "" — meaning the CLI default from ~/.claude/settings.json."""
    return entry.get("model") or cfg.get("default_model") or ""


def resolve_backend(channel_id: str, user_id: str) -> str:
    """Return the explicitly configured, safety-allowlisted room backend."""
    return resolve_room_policy(_load_model_config(), channel_id, user_id).backend


def resolve_model_settings(channel_id: str, user_id: str) -> tuple[str, str, str]:
    """(model or "" for CLI default, effort, per-model prompt or "").

    DM per-user override beats the channel entry, which beats the config's own
    default_model; effort falls back to default_effort. The prompt is keyed by the
    model actually in play — including the settings.json default when nothing is
    set anywhere — so a per-model prompt follows the model everywhere.
    """
    cfg = _load_model_config()
    entry = _resolve_entry(channel_id, user_id)
    model = _effective_model(cfg, entry)
    effort = entry.get("effort") or cfg.get("default_effort") or DEFAULT_EFFORT
    if effort not in _EFFORTS:
        logger.warning(f"bad effort {effort!r} for {channel_id}; using {DEFAULT_EFFORT}")
        effort = DEFAULT_EFFORT
    prompt = (cfg.get("model_prompts", {}).get(model or _settings_default_model()) or "").strip()
    return model, effort, prompt


def resolve_prompt_cadence(channel_id: str, user_id: str) -> tuple[int, str]:
    """(every-N-messages, per-model prompt) for re-injecting the prompt mid-session.

    The prompt always goes in the system prompt at spawn; a cadence of N > 0 also
    appends it to every Nth message the human sends, so a standing instruction
    survives a long thread instead of decaying. 0 (the default) = spawn only.
    """
    cfg = _load_model_config()
    entry = _resolve_entry(channel_id, user_id)
    model = _effective_model(cfg, entry) or _settings_default_model()
    prompt = (cfg.get("model_prompts", {}).get(model) or "").strip()
    try:
        every = int(cfg.get("model_prompt_cadence", {}).get(model) or 0)
    except (TypeError, ValueError):
        every = 0
    return max(every, 0), prompt

VOTES_FILE = STATE_DIR / "votes.json"

# The Slack user ID of this bot — set via BOT_USER_ID env var.
# Used to identify the bot's own messages in thread history and to prevent
# duplicate handling of @mentions. Find it in your Slack app settings or
# by calling auth.test.
BOT_USER_ID = os.environ.get("BOT_USER_ID", "")

# Display name for the bot (used in thread context formatting)
BOT_DISPLAY_NAME = os.environ.get("BOT_DISPLAY_NAME", "Your AI Employee")

# ---------------------------------------------------------------------------
# Slack app (with signing secret for request verification)
# ---------------------------------------------------------------------------

app = App(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET,
)
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# Cache for Slack user display names (user_id → display name)
_user_name_cache: dict[str, str] = {}

# Cache for Slack channel names (channel_id → channel name)
_channel_name_cache: dict[str, str] = {}


def _get_channel_name(channel_id: str) -> str:
    """Look up a Slack channel's name, with caching."""
    if channel_id in _channel_name_cache:
        return _channel_name_cache[channel_id]
    try:
        info = slack_client.conversations_info(channel=channel_id)
        name = info["channel"].get("name", channel_id)
        _channel_name_cache[channel_id] = name
    except Exception:
        name = channel_id
        _channel_name_cache[channel_id] = name
    return name


_channel_private_cache: dict[str, bool] = {}


def _is_channel_private(channel_id: str) -> bool:
    """Check whether a Slack channel is private, with caching.

    Modern Slack reports `channel_type: "channel"` in events for both public
    and private channels — the only reliable signal is the `is_private` flag
    on the channel object.
    """
    if channel_id in _channel_private_cache:
        return _channel_private_cache[channel_id]
    try:
        info = slack_client.conversations_info(channel=channel_id)
        priv = bool(info["channel"].get("is_private", False))
    except Exception:
        priv = False
    _channel_private_cache[channel_id] = priv
    return priv


def _channel_allowed(channel_id: str, channel_type: str) -> bool:
    """True if Andy should respond in this channel.

    - DMs (im) and group DMs (mpim): always pass this channel-name filter;
      current-sender authorization is enforced separately.
    - Private channels: always pass — Slack only delivers events for channels
      Andy's a member of, so receiving an event implies she's been explicitly
      invited. Membership = consent to participate. Note: modern private
      channels report `channel_type: "channel"` (not "group"), so we check
      `is_private` via the channel object.
    - Public channels: must contain at least one of ALLOWED_CHANNEL_SUBSTRINGS
      in the name (case-insensitive). Empty list = all pass.
    """
    if channel_type in ("im", "mpim", "group"):
        return True
    if _is_channel_private(channel_id):
        return True
    if not ALLOWED_CHANNEL_SUBSTRINGS:
        return True
    name = _get_channel_name(channel_id).lower()
    return any(sub in name for sub in ALLOWED_CHANNEL_SUBSTRINGS)


def _get_user_name(user_id: str) -> str:
    """Look up a Slack user's display name, with caching."""
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    try:
        info = slack_client.users_info(user=user_id)
        profile = info["user"].get("profile", {})
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or info["user"].get("real_name")
            or user_id
        )
        _user_name_cache[user_id] = name
    except Exception:
        name = user_id
        _user_name_cache[user_id] = name
    return name



def _fetch_thread_context(channel: str, thread_ts: str, current_msg_ts: str) -> str | None:
    """Fetch all prior messages in a thread and format them as context for Claude.

    Returns a formatted string of the conversation history, or None if there's
    nothing useful (e.g., the thread has only the current message).
    Excludes the current message (it's already in the prompt) and bot messages
    that are Claude's own responses (to avoid echoing back our own output).
    """
    try:
        result = slack_client.conversations_replies(
            channel=channel, ts=thread_ts, limit=50,
        )
        messages = result.get("messages", [])
    except Exception as e:
        logger.warning(f"Failed to fetch thread history: {e}")
        return None

    if len(messages) <= 1:
        return None

    lines = []
    for msg in messages:
        msg_ts = msg.get("ts", "")
        # Skip the current inbound message — it's already the prompt
        if msg_ts == current_msg_ts:
            continue

        msg_user = msg.get("user", "")
        msg_text = msg.get("text", "").strip()
        if not msg_text:
            continue

        if msg_user == BOT_USER_ID:
            lines.append(f"[You ({BOT_DISPLAY_NAME})]:\n{msg_text}")
        else:
            name = _get_user_name(msg_user)
            lines.append(f"[{name}]({msg_user}):\n{msg_text}")

    if not lines:
        return None

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Session store: thread_ts → Claude session_id (file-backed)
# ---------------------------------------------------------------------------

SESSION_FILE = STATE_DIR / "sessions.json"
OPENAI_SESSION_FILE = STATE_DIR / "openai-sessions.json"
MAX_SESSIONS = 200
_session_file_lock = threading.Lock()


def _load_sessions() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_session(thread_ts: str, session_id: str) -> None:
    with _session_file_lock:
        sessions = _load_sessions()
        sessions[thread_ts] = session_id
        if len(sessions) > MAX_SESSIONS:
            for key in sorted(sessions.keys())[:-MAX_SESSIONS]:
                del sessions[key]
        write_private_json(SESSION_FILE, sessions)


def _get_session(thread_ts: str) -> str | None:
    return _load_sessions().get(thread_ts)


def _load_openai_sessions() -> dict:
    try:
        return json.loads(OPENAI_SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_openai_session(thread_ts: str, session_id: str, cwd: Path) -> None:
    with _session_file_lock:
        sessions = _load_openai_sessions()
        sessions[thread_ts] = {"session_id": session_id, "cwd": str(cwd.resolve())}
        if len(sessions) > MAX_SESSIONS:
            for key in sorted(sessions.keys())[:-MAX_SESSIONS]:
                del sessions[key]
        write_private_json(OPENAI_SESSION_FILE, sessions)


def _get_openai_session(thread_ts: str) -> str | None:
    value = _load_openai_sessions().get(thread_ts)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("session_id"), str):
        return value["session_id"]
    return None


def _get_openai_session_cwd(thread_ts: str) -> Path | None:
    value = _load_openai_sessions().get(thread_ts)
    if isinstance(value, dict) and isinstance(value.get("cwd"), str):
        return Path(value["cwd"]).resolve()
    return None


# ---------------------------------------------------------------------------
# Cross-thread forward map: when Claude DMs someone in a side thread to ask a
# question, register a forward so that any reply in that side thread routes
# back into the original conversation thread instead of starting a new one.
# Single-shot — the entry is removed once the first reply has been forwarded.
# ---------------------------------------------------------------------------

FORWARDS_FILE = STATE_DIR / "forwards.json"
FORWARDS_MAX_AGE_SECONDS = 14 * 24 * 3600
_forwards_lock = threading.Lock()


def _load_forwards() -> dict:
    try:
        return json.loads(FORWARDS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_forwards(fwds: dict) -> None:
    write_private_json(FORWARDS_FILE, fwds)


def _add_forward(
    from_thread: str,
    to_thread: str,
    to_channel: str,
    session_id: str | None,
    user_id: str,
) -> None:
    with _forwards_lock:
        fwds = _load_forwards()
        fwds[from_thread] = {
            "thread": to_thread,
            "channel": to_channel,
            "session_id": session_id,
            "user_id": user_id,
            "registered_at": time.time(),
        }
        _write_forwards(fwds)


def _get_forward(from_thread: str) -> dict | None:
    return _load_forwards().get(from_thread)


def _remove_forward(from_thread: str) -> None:
    with _forwards_lock:
        fwds = _load_forwards()
        if fwds.pop(from_thread, None) is not None:
            _write_forwards(fwds)


def _gc_forwards() -> None:
    cutoff = time.time() - FORWARDS_MAX_AGE_SECONDS
    with _forwards_lock:
        fwds = _load_forwards()
        stale = [k for k, v in fwds.items() if v.get("registered_at", 0) < cutoff]
        if not stale:
            return
        for k in stale:
            del fwds[k]
        _write_forwards(fwds)
        logger.info(f"Garbage-collected {len(stale)} stale forward entries")


# ---------------------------------------------------------------------------
# Live session management: long-lived Claude processes with stream-json I/O
#
# Instead of spawning a new `claude -p` subprocess for every message (which
# causes race conditions when multiple messages arrive for the same thread),
# we keep Claude processes alive and pipe messages to their stdin as JSON.
# The CLI queues them automatically, matching terminal behavior.
# ---------------------------------------------------------------------------

IDLE_TIMEOUT = 10800  # 3 hours — kill process if no messages
MAX_LIVE_SESSIONS = RUNTIME_POLICY.max_live_sessions


@dataclass
class LiveSession:
    """A long-lived Claude CLI process attached to a Slack thread."""
    proc: subprocess.Popen
    session_id: str | None = None
    stdin_lock: threading.Lock = field(default_factory=threading.Lock)
    last_activity: float = field(default_factory=time.time)
    channel: str = ""
    thread_ts: str = ""
    user_id: str = ""
    # Serializes the full send→wait cycle so only one message at a time
    # is being actively processed. Other messages queue in our Python code.
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    # Callback for posting text blocks to Slack
    _on_text: callable = field(default=None, repr=False)
    # Event that signals when a turn (result) is complete
    _turn_done: threading.Event = field(default_factory=threading.Event)
    # Eyes reactions from mid-turn steering messages, removed by the reader
    # loop when the absorbing turn's result arrives. CPython list append/pop
    # are atomic, so no extra lock for this traffic.
    pending_reactions: list = field(default_factory=list)
    # Highest 100k context threshold already announced in the thread
    ctx_notified_level: int = 0
    # Model last announced in the thread (from message.model on assistant
    # events — the served model, not the --model flag). "" = not yet announced.
    model_notified: str = ""
    # Messages sent into this process, for the per-model prompt cadence. Resets
    # when the thread's process is respawned after an idle-out.
    turns_sent: int = 0
    escalation_message_file: Path | None = None
    escalation_receipt_file: Path | None = None
    private_escalation_pending: bool = False
    first_tool_seen: bool = False
    pre_tool_text: list[str] = field(default_factory=list)
    workspace: ThreadWorkspace | None = None
    delegation_tracker: DelegationTracker = field(default_factory=DelegationTracker)


# thread_ts → LiveSession
_live_sessions: dict[str, LiveSession] = {}
_live_sessions_lock = threading.Lock()
_openai_turn_locks: dict[str, threading.Lock] = {}
_openai_processes: dict[str, subprocess.Popen] = {}
_openai_stopped: set[str] = set()
_openai_models_notified: dict[str, str] = {}


def _openai_turn_lock(thread_ts: str) -> threading.Lock:
    with _live_sessions_lock:
        return _openai_turn_locks.setdefault(thread_ts, threading.Lock())

# thread_ts → count of inbound messages seen in multi-person spaces (public
# channels + group DMs). Used to re-inject the relevance reminder every
# REMINDER_EVERY messages, since Claude drifts back to over-responding once a
# thread gets long. Guarded by _live_sessions_lock. Not GC'd — grows slowly.
_thread_msg_counts: dict[str, int] = {}
REMINDER_EVERY = 10

# Usage-limit pause: when the account is out of usage, the Claude CLI itself
# synthesizes an assistant message like "You've hit your limit · resets 4pm
# (UTC)" — the model never runs, so the SKIP relevance filter can't suppress
# it. We intercept that text before it reaches Slack and route new work through
# the explicit OpenAI fallback until the reset time. Limits are account-wide,
# so the pause is a single global, while successful fallback sessions remain
# pinned per thread.
LIMIT_RESET_RE = re.compile(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
LIMIT_PAUSE_FALLBACK = 1800  # seconds, if the reset time can't be parsed
_limit_pause_lock = threading.Lock()
_limit_pause = {"until": 0.0, "announced": False}


def _parse_limit_reset(text: str) -> float:
    """Parse 'resets 4pm (UTC)' into an epoch timestamp (next occurrence)."""
    m = LIMIT_RESET_RE.search(text)
    if not m:
        return time.time() + LIMIT_PAUSE_FALLBACK
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    minute = int(m.group(2) or 0)
    now = datetime.now(timezone.utc)
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    # Small buffer so we don't resume a minute before the limit actually lifts
    return reset.timestamp() + 120


def _limit_paused() -> bool:
    """True while the usage-limit pause is active. Self-resets on expiry."""
    with _limit_pause_lock:
        if _limit_pause["until"] and time.time() >= _limit_pause["until"]:
            _limit_pause["until"] = 0.0
            _limit_pause["announced"] = False
        return _limit_pause["until"] > 0.0


def _enter_limit_pause(text: str) -> float | None:
    """Record a usage-limit notice. Returns the pause-end epoch if this call
    is the one that should announce the outage in Slack, else None."""
    with _limit_pause_lock:
        _limit_pause["until"] = max(_limit_pause["until"], _parse_limit_reset(text))
        if _limit_pause["announced"]:
            return None
        _limit_pause["announced"] = True
        return _limit_pause["until"]


def _get_trust_battery_context() -> str:
    """Read trust battery JSON files and format a context summary for Claude.

    Returns an empty string if trust batteries are not configured or no files exist.
    """
    if not TRUST_BATTERY_DIR:
        return ""
    battery_dir = Path(TRUST_BATTERY_DIR)
    if not battery_dir.exists():
        return ""
    tiers = [
        (0, 25, "Propose and Wait"),
        (25, 50, "Routine Execution"),
        (50, 75, "Judgment Calls"),
        (75, 100, "Full Autonomy"),
    ]
    lines = ["## Trust Battery — Current State"]
    for fpath in sorted(battery_dir.glob("*.json")):
        try:
            data = json.loads(fpath.read_text())
            name = data.get("team_member", fpath.stem)
            charge = data.get("current_charge", 0)
            last_updated = data.get("last_updated", "unknown")
            last_delta = 0.0
            if data.get("history"):
                last_delta = data["history"][-1].get("delta", 0.0)
            tier = next((t for lo, hi, t in tiers if lo <= charge < hi), "Full Autonomy")
            sign = "+" if last_delta >= 0 else ""
            lines.append(f"- {name}: {charge:.1f}% ({tier}) | Last: {sign}{last_delta:.1f} on {last_updated}")
        except Exception:
            continue
    if len(lines) == 1:
        return ""
    lines.append("")
    lines.append("Your autonomy level is determined by the battery charge for the")
    lines.append("team member you're interacting with:")
    lines.append("  0-25%  = Propose and Wait")
    lines.append("  25-50% = Routine Execution")
    lines.append("  50-75% = Judgment Calls")
    lines.append("  75-100% = Full Autonomy")
    return "\n".join(lines)


def _spawn_claude_process(
    session_id: str | None = None,
    user_id: str = "",
    thread_ts: str = "",
    channel: str = "",
) -> tuple[subprocess.Popen, ThreadWorkspace]:
    """Spawn a long-lived Claude CLI process with stream-json I/O.

    Injects CLAUDE_THREAD_TS / CLAUDE_CHANNEL_ID / CLAUDE_SESSION_ID env vars
    so the spawned Claude can read its own routing context (mainly for the
    cross-thread DM pattern — passing `--forward-to $CLAUDE_THREAD_TS`).
    """
    battery_context = _get_trust_battery_context()
    _model, _effort, model_prompt = resolve_model_settings(channel, user_id)
    policy = resolve_room_policy(_load_model_config(), channel, user_id)
    for warning in preflight_room(policy):
        logger.warning(f"Cargo Chief preflight: {warning}")
    workspace = prepare_thread_workspace(
        policy.root, channel=channel, thread=thread_ts, session_id=session_id
    )
    escalation_message_file = workspace.escalation_message_file
    escalation_receipt_file = workspace.escalation_receipt_file
    for stale_path in (
        escalation_message_file, escalation_receipt_file,
        workspace.escalation_attempt_file, workspace.parking_claim_file,
        workspace.delegation_request_file, workspace.implementation_claim_file,
        workspace.delegate_pid_file,
    ):
        try:
            stale_path.unlink()
        except FileNotFoundError:
            pass
    cmd = build_claude_command(
        policy,
        initial_prompt=battery_context,
        transport_python=Path(sys.executable),
        transport_script=SOURCE_DIR / "bot.py",
        escalation_message_file=escalation_message_file,
        bundle_claim_file=workspace.bundle_claim_file,
        parking_claim_file=workspace.parking_claim_file,
        delegation_request_file=workspace.delegation_request_file,
        implementation_claim_file=workspace.implementation_claim_file,
        model_prompt=model_prompt,
        session_id=session_id,
    )

    stderr_tmp = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".stderr", delete=False, dir=TEMP_DIR
    )

    proc_env = {**os.environ}
    if thread_ts:
        proc_env["CLAUDE_THREAD_TS"] = thread_ts
    if channel:
        proc_env["CLAUDE_CHANNEL_ID"] = channel
    if session_id:
        proc_env["CLAUDE_SESSION_ID"] = session_id
    proc_env["CARGO_CHIEF_ESCALATION_CHANNEL"] = policy.escalation_channel
    proc_env["CARGO_CHIEF_ESCALATION_MESSAGE_FILE"] = str(escalation_message_file)
    proc_env["CARGO_CHIEF_ESCALATION_RECEIPT_FILE"] = str(escalation_receipt_file)
    proc_env["CARGO_CHIEF_ESCALATION_ATTEMPT_FILE"] = str(workspace.escalation_attempt_file)
    proc_env["CARGO_CHIEF_THREAD_WORK_DIR"] = str(workspace.path)
    proc_env["CARGO_CHIEF_BUNDLE_CLAIM_FILE"] = str(workspace.bundle_claim_file)
    proc_env["CARGO_CHIEF_PARKING_CLAIM_FILE"] = str(workspace.parking_claim_file)
    proc_env["CARGO_CHIEF_ROOT"] = str(policy.root)
    proc_env["CARGO_CHIEF_DELEGATION_REQUEST_FILE"] = str(workspace.delegation_request_file)
    proc_env["CARGO_CHIEF_IMPLEMENTATION_CLAIM_FILE"] = str(workspace.implementation_claim_file)
    proc_env["CARGO_CHIEF_DELEGATION_BUDGET_FILE"] = str(workspace.delegation_budget_file)
    proc_env["CARGO_CHIEF_DELEGATE_PID_FILE"] = str(workspace.delegate_pid_file)
    proc_env["CARGO_CHIEF_DELEGATE_VERIFICATION_FILE"] = str(workspace.delegate_verification_file)
    proc_env["CARGO_CHIEF_AUDIT_LOG"] = str(AUDIT_LOG)
    proc_env["CARGO_CHIEF_OWNER_PROVIDER"] = "claude"
    proc_env["CARGO_CHIEF_OWNER_MODEL"] = policy.model
    proc_env["CARGO_CHIEF_OWNER_EFFORT"] = policy.effort
    proc_env["CARGO_CHIEF_CURRENT_USER"] = user_id
    proc_env["CARGO_CHIEF_DELEGATE_TIMEOUT"] = str(CLAUDE_TIMEOUT)
    bundle = resolve_thread_bundle(workspace)
    if bundle:
        proc_env["CARGO_CHIEF_THREAD_BUNDLE_DIR"] = str(bundle)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_tmp,
        text=True,
        cwd=bundle or workspace.path,
        env=proc_env,
    )
    logger.info(
        f"Spawned Claude process pid={proc.pid} (resume={session_id or 'none'}, "
        f"user={user_id}, permissions={policy.permission_mode}, model={policy.model}, "
        f"effort={policy.effort}, root={policy.root}, thread_work={workspace.key}, "
        f"bundle={(bundle.name if bundle else 'none')})"
    )
    return proc, workspace


# Announce context utilization in the thread every N tokens (bot-side only —
# the notice never enters Claude's context)
CTX_NOTIFY_STEP = 100_000
CTX_WINDOW = 1_000_000


def _post_context_notice(session: LiveSession, ctx: int) -> None:
    """Post a small grey context-block notice about context utilization."""
    try:
        note = f"context window: ~{ctx / 1000:.0f}k of {CTX_WINDOW // 1000}k tokens ({ctx / CTX_WINDOW:.0%})"
        slack_client.chat_postMessage(
            channel=session.channel, thread_ts=session.thread_ts,
            text=note,
            blocks=[{"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":brain: _{note}_"}]}],
        )
    except Exception as e:
        logger.warning(f"Failed to post context notice: {e}")


def _post_skill_notice(session: LiveSession, skill: str, args_str: str) -> None:
    """Post a small grey context-block notice that a skill was invoked."""
    try:
        note = f"skill: {skill}" + (f" · {args_str}" if args_str else "")
        slack_client.chat_postMessage(
            channel=session.channel, thread_ts=session.thread_ts,
            text=note,
            blocks=[{"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":toolbox: _{note}_"}]}],
        )
    except Exception as e:
        logger.warning(f"Failed to post skill notice: {e}")


def _post_model_notice_to_thread(
    channel: str, thread_ts: str, model: str, prev: str
) -> None:
    """Post a small grey context-block notice naming the serving model.

    `model` comes from message.model on the assistant event — the API's report
    of which model actually produced the response, so it stays truthful under
    silent fallbacks (e.g. opus-5 → opus-4-8). Posted once per session and
    again only if the served model ever changes.
    """
    note = model_notice_text(model, prev)
    if note is None:
        return
    try:
        slack_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=note,
            blocks=[{"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":robot_face: _{note}_"}]}],
        )
    except Exception as e:
        logger.warning(f"Failed to post model notice: {e}")


def _post_model_notice(session: LiveSession, model: str, prev: str) -> None:
    _post_model_notice_to_thread(
        session.channel, session.thread_ts, model, prev
    )


def _track_model(session: LiveSession, data: dict) -> None:
    """Announce the served model on the first main-loop assistant event of a
    session, and re-announce if it changes mid-thread (fallback routing).
    Subagent events are skipped — they may run a different model than the
    thread itself."""
    if data.get("parent_tool_use_id"):
        return
    model = data.get("message", {}).get("model") or ""
    if model_notice_text(model, session.model_notified) is not None:
        _post_model_notice(session, model, session.model_notified)
        session.model_notified = model


def _track_openai_model(channel: str, thread_ts: str, model: str) -> None:
    """Announce the serving Codex model once per Slack thread."""
    with _live_sessions_lock:
        previous = _openai_models_notified.get(thread_ts, "")
        if model_notice_text(model, previous) is None:
            return
        _openai_models_notified[thread_ts] = model
    _post_model_notice_to_thread(channel, thread_ts, model, previous)


def _track_context(session: LiveSession, data: dict) -> None:
    """Watch usage on assistant events; announce each new 100k threshold.

    Context shrinks when the CLI compacts the conversation — re-arm the
    thresholds then, so utilization gets re-announced on the way back up.
    """
    # Subagent (Task) events stream through the same stdout with the
    # subagent's own, much smaller context — counting them re-arms the
    # threshold and re-fires the alert every turn. Main-loop events only.
    if data.get("parent_tool_use_id"):
        return
    usage = data.get("message", {}).get("usage") or {}
    ctx = (usage.get("input_tokens", 0)
           + usage.get("cache_read_input_tokens", 0)
           + usage.get("cache_creation_input_tokens", 0))
    if not ctx:
        return
    level = ctx // CTX_NOTIFY_STEP
    if level > session.ctx_notified_level:
        session.ctx_notified_level = level
        _post_context_notice(session, ctx)
    elif level < session.ctx_notified_level:
        session.ctx_notified_level = level


def _reader_loop(session: LiveSession) -> None:
    """Read stdout from a live Claude process and post responses to Slack.

    Runs in a dedicated thread for each live session.
    """
    try:
        for line in session.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Any output means the session is working — keep the idle reaper
            # away even when no new message has arrived for a long time
            # (e.g. a quiet multi-hour background workflow).
            session.last_activity = time.time()

            msg_type = data.get("type")
            session.delegation_tracker.observe(data)

            if msg_type == "system":
                sid = data.get("session_id")
                if sid:
                    session.session_id = sid

            elif msg_type == "assistant":
                content = data.get("message", {}).get("content", [])
                # Subagent (Agent/Task) events stream through the same stdout
                # with parent_tool_use_id set — their text is internal chatter,
                # not a reply to the human. Only main-loop text reaches Slack.
                if data.get("parent_tool_use_id"):
                    continue
                if (session.escalation_message_file
                        and contains_escalation_file_write(
                            content, session.escalation_message_file
                        )):
                    session.private_escalation_pending = True
                    session.pre_tool_text.clear()
                if session.private_escalation_pending:
                    continue

                if (session.workspace
                        and session.workspace.delegate_verification_file.is_file()):
                    # The delegate return is visible inside the owner context but may not reach
                    # Slack until the owner records an independent verification.
                    continue

                _track_model(session, data)
                _track_context(session, data)
                has_tool_use = any(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    for block in content
                )
                text_blocks = [
                    block.get("text", "").strip()
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                    and block.get("text", "").strip()
                ]
                if not session.first_tool_seen:
                    session.pre_tool_text.extend(text_blocks)
                    if not has_tool_use:
                        continue
                    session.first_tool_seen = True
                    text_blocks = list(session.pre_tool_text)
                    session.pre_tool_text.clear()
                for block in content:
                    # Skill visibility: announce main-loop skill invocations in a
                    # grey context block (bot-side only, never enters Claude's
                    # context).
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") == "Skill"):
                        inp = block.get("input") or {}
                        args_str = str(inp.get("args") or "")[:120]
                        _post_skill_notice(session, inp.get("skill", "?"), args_str)
                for text in text_blocks:
                    if session._on_text:
                        session._on_text(text)

            elif msg_type == "result":
                for record in session.delegation_tracker.finish_turn():
                    audit_logger.info(format_delegation_audit(
                        record,
                        user=session.user_id,
                        channel=session.channel,
                        thread=session.thread_ts,
                    ))
                if session.workspace and session.workspace.bundle_claim_file.exists():
                    try:
                        bundle = consume_bundle_claim(
                            session.workspace,
                            max_live_bundles=RUNTIME_POLICY.max_live_bundles,
                        )
                    except SafetyError as exc:
                        audit_logger.warning(
                            "BUNDLE_REFUSED | USER:%s | CHANNEL:%s | THREAD:%s | REASON:%s",
                            session.user_id, session.channel, session.thread_ts, str(exc),
                        )
                        if session._on_text:
                            session._on_text(f"Worktree bundle mapping refused: {exc}")
                    else:
                        if bundle:
                            audit_logger.info(
                                "BUNDLE_BOUND | USER:%s | CHANNEL:%s | THREAD:%s | BUNDLE:%s",
                                session.user_id, session.channel, session.thread_ts, bundle.name,
                            )
                parking = None
                parking_refused = None
                if session.workspace and session.workspace.parking_claim_file.exists():
                    try:
                        parking = consume_parking_claim(session.workspace)
                    except SafetyError as exc:
                        parking_refused = str(exc)
                        audit_logger.warning(
                            "PARKING_REFUSED | USER:%s | CHANNEL:%s | THREAD:%s | REASON:%s",
                            session.user_id, session.channel, session.thread_ts, parking_refused,
                        )
                    else:
                        if parking:
                            audit_logger.info(
                                "WORK_PARKED | USER:%s | CHANNEL:%s | THREAD:%s | KIND:%s | RECORD:%s",
                                session.user_id, session.channel, session.thread_ts,
                                parking.kind, parking.path.relative_to(session.workspace.root),
                            )
                verification_missing = bool(
                    session.workspace
                    and session.workspace.delegate_verification_file.is_file()
                )
                if verification_missing:
                    audit_logger.warning(
                        "DELEGATION_VERIFICATION | USER:%s | CHANNEL:%s | THREAD:%s "
                        "| OWNER_VERIFY_TOOLS:0 | STATUS:missing",
                        session.user_id, session.channel, session.thread_ts,
                    )
                sid = data.get("session_id")
                if sid:
                    session.session_id = sid
                    _save_session(session.thread_ts, sid)
                    if session.workspace:
                        prepare_thread_workspace(
                            session.workspace.root,
                            channel=session.channel,
                            thread=session.thread_ts,
                            session_id=sid,
                        )
                if session.private_escalation_pending:
                    delivered = bool(
                        session.escalation_receipt_file
                        and session.escalation_receipt_file.is_file()
                    )
                    if session.escalation_receipt_file:
                        try:
                            session.escalation_receipt_file.unlink()
                        except OSError:
                            pass
                    status = private_escalation_status(
                        delivered=delivered,
                        parking=parking,
                        parking_refused=bool(parking_refused),
                    )
                    if session._on_text:
                        session._on_text(status)
                elif verification_missing:
                    if session._on_text:
                        session._on_text(
                            "Delegate result withheld: independent owner verification is missing."
                        )
                else:
                    for text in session.pre_tool_text:
                        if session._on_text:
                            session._on_text(text)
                session.private_escalation_pending = False
                session.first_tool_seen = False
                session.pre_tool_text.clear()
                # Clear eyes reactions from steering messages this turn absorbed
                while session.pending_reactions:
                    ch, ts = session.pending_reactions.pop(0)
                    try:
                        slack_client.reactions_remove(channel=ch, name="eyes", timestamp=ts)
                    except Exception:
                        pass
                session._turn_done.set()

    except Exception as e:
        logger.error(f"Reader loop error for thread {session.thread_ts}: {e}")
    finally:
        # Process ended — unblock any thread waiting on a response
        session._turn_done.set()
        logger.info(f"Reader loop ended for thread {session.thread_ts} (pid={session.proc.pid})")
        with _live_sessions_lock:
            _live_sessions.pop(session.thread_ts, None)


def _get_or_create_live_session(thread_ts: str, channel: str, user_id: str = "") -> LiveSession:
    """Get an existing live session or create a new one for a thread."""
    with _live_sessions_lock:
        session = _live_sessions.get(thread_ts)
        if session and session.proc.poll() is None:
            session.last_activity = time.time()
            return session

        # Evict oldest idle session if at capacity
        if len(_live_sessions) >= MAX_LIVE_SESSIONS:
            oldest_ts = min(_live_sessions, key=lambda k: _live_sessions[k].last_activity)
            oldest = _live_sessions.pop(oldest_ts)
            logger.info(f"Evicting idle session for thread {oldest_ts} (pid={oldest.proc.pid})")
            try:
                oldest.proc.stdin.close()
                oldest.proc.wait(timeout=10)
            except Exception:
                oldest.proc.kill()

        saved_session_id = _get_session(thread_ts)
        proc, workspace = _spawn_claude_process(
            session_id=saved_session_id,
            user_id=user_id,
            thread_ts=thread_ts,
            channel=channel,
        )
        session = LiveSession(
            proc=proc,
            session_id=saved_session_id,
            channel=channel,
            thread_ts=thread_ts,
            user_id=user_id,
            escalation_message_file=workspace.escalation_message_file,
            escalation_receipt_file=workspace.escalation_receipt_file,
            workspace=workspace,
        )
        _live_sessions[thread_ts] = session

        threading.Thread(target=_reader_loop, args=(session,), daemon=True).start()
        return session


def _send_to_claude(session: LiveSession, text: str) -> None:
    """Send a user message to a live Claude process via stdin."""
    session.turns_sent += 1
    every, model_prompt = resolve_prompt_cadence(session.channel, session.user_id)
    if model_prompt and every and session.turns_sent % every == 0:
        text += f"\n\n[reminder]\n{model_prompt}"
        logger.info(f"Re-injected model prompt at message {session.turns_sent} "
                    f"in thread {session.thread_ts}")
    msg = json.dumps({
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    })
    with session.stdin_lock:
        session.proc.stdin.write(msg + "\n")
        session.proc.stdin.flush()
    session.last_activity = time.time()


def _cleanup_idle_sessions() -> None:
    """Periodically kill Claude processes that have been idle too long."""
    while True:
        time.sleep(300)
        now = time.time()
        to_remove = []
        with _live_sessions_lock:
            for ts, session in list(_live_sessions.items()):
                if now - session.last_activity > IDLE_TIMEOUT:
                    to_remove.append((ts, session))

        for ts, session in to_remove:
            logger.info(f"Cleaning up idle session for thread {ts} (pid={session.proc.pid})")
            try:
                session.proc.stdin.close()
                session.proc.wait(timeout=15)
            except Exception:
                session.proc.kill()
            if session.session_id:
                _save_session(ts, session.session_id)
            with _live_sessions_lock:
                _live_sessions.pop(ts, None)

        with _live_sessions_lock:
            active_keys = frozenset(
                session.workspace.key
                for session in _live_sessions.values()
                if session.workspace is not None
            )
        cleanup_thread_workspaces(WORKSPACE_ROOT, active_keys=active_keys)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def is_authorized(user_id: str) -> bool:
    try:
        return AuthorityPolicy.from_env().allows(user_id)
    except SafetyError:
        return False


def log_unauthorized(event: dict) -> None:
    user = event.get("user", "unknown")
    channel = event.get("channel", "unknown")
    audit_logger.warning(format_audit_metadata(
        "UNAUTHORIZED", user=user, channel=channel,
        thread=event.get("thread_ts") or event.get("ts") or "unknown",
        message_length=len(event.get("text", "")),
    ))


def audit_interaction(
    event: dict, response_text: str, duration: float, session_id: str | None
) -> None:
    user = event.get("user", "unknown")
    channel = event.get("channel", "unknown")
    audit_logger.info(format_audit_metadata(
        "INTERACTION", user=user, channel=channel,
        thread=event.get("thread_ts") or event.get("ts") or "unknown",
        message_length=len(event.get("text", "")), response_length=len(response_text),
        session_id=session_id or "new", duration=duration,
    ))


# ---------------------------------------------------------------------------
# Claude CLI (uses long-lived processes with stream-json I/O — see above)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Markdown → Slack mrkdwn
# ---------------------------------------------------------------------------


def md_to_slack(text: str) -> str:
    """Convert GitHub-flavored markdown to Slack mrkdwn."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    text = re.sub(r"```\w*\n", "```\n", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    return text


def chunk_message(text: str) -> list:
    """Split a message into Slack-safe chunks."""
    if len(text) <= MAX_SLACK_MSG_LEN:
        return [text]

    chunks = []
    while text:
        if len(text) <= MAX_SLACK_MSG_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, MAX_SLACK_MSG_LEN)
        if split_at == -1:
            split_at = text.rfind(" ", 0, MAX_SLACK_MSG_LEN)
        if split_at == -1:
            split_at = MAX_SLACK_MSG_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# A GFM table separator row, e.g. "|---|:---:|" — the signal that a chunk
# contains a markdown table and should go out as a native markdown block.
_MD_TABLE_SEP = re.compile(
    r"^ {0,3}\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$",
    re.MULTILINE,
)


def post_response(channel: str, message: str, thread_ts: str | None = None) -> str | None:
    """Post a markdown response to Slack, chunked.

    Chunks containing a markdown table are sent as a native `markdown` block
    (Slack renders GFM tables, task lists, headers natively); everything else
    goes as plain mrkdwn text via md_to_slack. Returns the effective thread_ts
    (the first message's ts when not already in a thread).
    """
    parent_ts = thread_ts
    for chunk in chunk_message(message):
        fallback = md_to_slack(chunk)
        result = None
        if _MD_TABLE_SEP.search(chunk):
            try:
                result = slack_client.chat_postMessage(
                    channel=channel, thread_ts=parent_ts, text=fallback,
                    blocks=[{"type": "markdown", "text": chunk}],
                )
            except Exception as e:
                logger.warning(f"markdown block post failed, using plain text: {e}")
        if result is None:
            result = slack_client.chat_postMessage(
                channel=channel, thread_ts=parent_ts, text=fallback,
            )
        if parent_ts is None:
            parent_ts = result["ts"]
    return parent_ts


# ---------------------------------------------------------------------------
# Voting (Block Kit interactive buttons)
# ---------------------------------------------------------------------------

_votes_lock = threading.Lock()


def _load_votes() -> dict:
    if VOTES_FILE.exists():
        try:
            return json.loads(VOTES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_votes(votes: dict) -> None:
    write_private_json(VOTES_FILE, votes, indent=2)


def _vote_key(channel: str, ts: str) -> str:
    return f"{channel}:{ts}"


def _build_vote_blocks(text: str, vote_key: str, votes: dict | None = None) -> list[dict]:
    """Build Block Kit blocks: message text + voting buttons with current tallies."""
    entry = (votes or {}).get(vote_key, {})
    strong_users = entry.get("strong", [])
    pass_users = entry.get("pass", [])

    strong_names = [_get_user_name(u) for u in strong_users]
    pass_names = [_get_user_name(u) for u in pass_users]

    strong_label = f":+1: strong ({len(strong_users)})" if strong_users else ":+1: strong"
    pass_label = f":-1: pass ({len(pass_users)})" if pass_users else ":-1: pass"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "block_id": f"votes_{vote_key}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": strong_label, "emoji": True},
                    "action_id": "vote_strong",
                    "value": vote_key,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": pass_label, "emoji": True},
                    "action_id": "vote_pass",
                    "value": vote_key,
                },
            ],
        },
    ]

    if strong_names or pass_names:
        parts = []
        if strong_names:
            parts.append(f":+1: {', '.join(strong_names)}")
        if pass_names:
            parts.append(f":-1: {', '.join(pass_names)}")
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  ·  ".join(parts)}],
        })

    return blocks


def _handle_vote(action_id: str, vote_key: str, user_id: str) -> None:
    """Record a vote and update the Slack message."""
    vote_type = "strong" if action_id == "vote_strong" else "pass"
    other_type = "pass" if vote_type == "strong" else "strong"

    with _votes_lock:
        votes = _load_votes()
        entry = votes.setdefault(vote_key, {"strong": [], "pass": [], "text": "", "channel": ""})

        if user_id in entry.get(vote_type, []):
            entry[vote_type].remove(user_id)
        else:
            if user_id in entry.get(other_type, []):
                entry[other_type].remove(user_id)
            entry.setdefault(vote_type, []).append(user_id)

        _save_votes(votes)

    channel, ts = vote_key.split(":", 1)
    text = entry.get("text", "")
    blocks = _build_vote_blocks(text, vote_key, votes)

    try:
        slack_client.chat_update(
            channel=channel,
            ts=ts,
            blocks=blocks,
            text=text,
        )
    except Exception as e:
        logger.error(f"Failed to update vote message: {e}")


def post_with_votes(channel: str, text: str, thread_ts: str | None = None) -> str | None:
    """Post a message with voting buttons. Returns the message ts."""
    slack_text = md_to_slack(text)
    placeholder_key = "pending"
    blocks = _build_vote_blocks(slack_text, placeholder_key)

    result = slack_client.chat_postMessage(
        channel=channel,
        blocks=blocks,
        text=slack_text,
        thread_ts=thread_ts,
    )
    ts = result["ts"]

    vote_key = _vote_key(channel, ts)
    blocks = _build_vote_blocks(slack_text, vote_key)
    slack_client.chat_update(channel=channel, ts=ts, blocks=blocks, text=slack_text)

    with _votes_lock:
        votes = _load_votes()
        votes[vote_key] = {"strong": [], "pass": [], "text": slack_text, "channel": channel}
        _save_votes(votes)

    return ts


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------


def download_slack_files(event: dict) -> list[Path]:
    """Download Slack file attachments to temp files for Claude to read."""
    files = event.get("files", [])
    if not files:
        return []
    if not RUNTIME_POLICY.file_transfer_enabled:
        audit_logger.warning(
            f"FILE_DOWNLOAD_REFUSED | USER:{event.get('user', 'unknown')} "
            f"| CHANNEL:{event.get('channel', 'unknown')} | FILE_COUNT:{len(files)}"
        )
        return []

    downloaded = []
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue

        name = f.get("name", "attachment")
        suffix = Path(name).suffix or ".bin"

        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
            )
            with urllib.request.urlopen(req) as resp:
                tmp = tempfile.NamedTemporaryFile(
                    suffix=suffix, prefix="slack-", delete=False, dir=TEMP_DIR
                )
                tmp.write(resp.read())
                tmp.close()
                downloaded.append(Path(tmp.name))
                logger.info(f"Downloaded Slack file: {name} -> {tmp.name}")
        except Exception as e:
            logger.error(f"Failed to download Slack file {name}: {e}")

    return downloaded


# File upload trigger: only paths prefixed with "attach:" are uploaded.
# Matches "attach:/path/to/file" or "attach:~/path/to/file" (with optional
# whitespace after the colon). This prevents accidental uploads when file
# paths are mentioned in normal conversation.
_ATTACH_PATTERN = re.compile(
    r'attach:\s*(~/[^\s`\'"<>|*?,]+\.\w+|/(?:Users|tmp|var|home)/[^\s`\'"<>|*?,]+\.\w+)',
    re.MULTILINE,
)


def _auto_upload_files(text: str, channel: str, thread_ts: str | None = None) -> None:
    """Scan text for attach:/path markers and upload matching files to Slack."""
    if not RUNTIME_POLICY.file_transfer_enabled:
        return
    seen: set[str] = set()
    for match in _ATTACH_PATTERN.findall(text):
        fp_str = match.rstrip('.,;:!?)]`"\'')
        # Expand tilde to home directory
        if fp_str.startswith('~'):
            fp_str = str(Path.home() / fp_str[2:])
        if fp_str in seen:
            continue
        seen.add(fp_str)
        fp = Path(fp_str)
        if fp.exists() and fp.is_file():
            upload_file_to_slack(str(fp), channel, thread_ts=thread_ts)
            logger.info(f"Auto-uploaded file from response: {fp}")


def upload_file_to_slack(
    file_path: str,
    channel: str,
    thread_ts: str | None = None,
    title: str | None = None,
    message: str | None = None,
) -> None:
    """
    Upload a file from the local machine to Slack.

    Uses Slack's v2 upload flow:
    1. Get a presigned upload URL
    2. POST the file to it
    3. Complete the upload (share to channel/thread)

    Claude can call this to share screenshots, CSVs, reports, etc.
    """
    if not RUNTIME_POLICY.file_transfer_enabled:
        raise SafetyError("file upload is disabled for Gate A")
    path = Path(file_path)
    if not path.exists():
        logger.error(f"File not found: {file_path}")
        return

    filename = title or path.name
    file_size = path.stat().st_size

    try:
        # Step 1: Get upload URL
        url_response = slack_client.files_getUploadURLExternal(
            filename=filename,
            length=file_size,
        )
        upload_url = url_response["upload_url"]
        file_id = url_response["file_id"]

        # Step 2: Upload the file
        with open(path, "rb") as f:
            import urllib.request as urlreq
            req = urlreq.Request(
                upload_url,
                data=f.read(),
                method="POST",
                headers={"Content-Type": "application/octet-stream"},
            )
            urlreq.urlopen(req)

        # Step 3: Complete the upload (share to channel)
        slack_client.files_completeUploadExternal(
            files=[{"id": file_id, "title": filename}],
            channel_id=channel,
            thread_ts=thread_ts,
            initial_comment=message or "",
        )

        logger.info(f"Uploaded file to Slack: {filename} ({file_size} bytes) -> {channel}")
    except Exception as e:
        logger.error(f"Failed to upload file {file_path}: {e}")
        # Fall back: post the file path so the user knows what happened
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Tried to upload `{filename}` but failed: {e}",
        )


# ---------------------------------------------------------------------------
# Proactive messaging (CLI mode)
# ---------------------------------------------------------------------------


def send_dm(
    user_id: str,
    message: str,
    session_id: str | None = None,
    thread_ts: str | None = None,
    forward_to: str | None = None,
) -> str | None:
    """Send a proactive DM. Returns thread_ts.

    If `forward_to` is set, registers a cross-thread forward: any reply in
    this new DM thread will be routed into the live session for `forward_to`
    instead of starting a new conversation here. Requires the running bot
    server to have a live session for `forward_to`.
    """
    response = slack_client.conversations_open(users=[user_id])
    channel_id = response["channel"]["id"]

    effective_thread_ts = post_response(channel_id, message, thread_ts=thread_ts)

    # Auto-upload any file paths mentioned in the message
    _auto_upload_files(message, channel_id, thread_ts=effective_thread_ts)

    if session_id and effective_thread_ts:
        _save_session(effective_thread_ts, session_id)

    if forward_to and effective_thread_ts:
        _register_forward_via_server(effective_thread_ts, forward_to)

    audit_logger.info(
        f"PROACTIVE_DM | USER:{user_id} | CHANNEL:{channel_id} "
        f"| THREAD:{effective_thread_ts} | SESSION:{session_id or 'none'} "
        f"| FORWARD_TO:{forward_to or 'none'} | MSG_LEN:{len(message)}"
    )
    return effective_thread_ts


def _register_forward_via_server(from_thread: str, to_thread: str) -> None:
    """Call the running bot server to register a forward.

    The server has the in-memory live-session map and can resolve channel,
    session_id, and user_id for the target thread. This CLI invocation can't.
    """
    url = f"http://127.0.0.1:{PORT}/internal/forward"
    payload = json.dumps({"from_thread": from_thread, "to_thread": to_thread}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                body = resp.read().decode(errors="replace")
                logger.error(f"Forward registration failed: {resp.status} {body}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        logger.error(f"Forward registration failed: {e.code} {body}")
        raise
    except Exception as e:
        logger.error(f"Forward registration error: {e}")
        raise


def send_to_channel(
    channel: str,
    message: str,
    session_id: str | None = None,
    thread_ts: str | None = None,
) -> str | None:
    """Post a message to a channel (optionally in a thread). Returns thread_ts."""
    effective_thread_ts = post_response(channel, message, thread_ts=thread_ts)

    # Auto-upload any file paths mentioned in the message
    _auto_upload_files(message, channel, thread_ts=effective_thread_ts)

    if session_id and effective_thread_ts:
        _save_session(effective_thread_ts, session_id)

    audit_logger.info(
        f"PROACTIVE_CHANNEL | CHANNEL:{channel} "
        f"| THREAD:{effective_thread_ts} | SESSION:{session_id or 'none'} "
        f"| MSG_LEN:{len(message)}"
    )
    return effective_thread_ts


# ---------------------------------------------------------------------------
# Read access (CLI mode) — read messages from any channel Andy has scope for
# ---------------------------------------------------------------------------


def fetch_channel_history(
    channel: str,
    limit: int = 50,
    thread_ts: str | None = None,
) -> str:
    """Return formatted message history for a channel, or a single thread.

    Works for any channel type Andy's token has scope for. Private channels
    require Andy to be a member. Public channels do not — `channels:history`
    scope is sufficient. If she gets `not_in_channel`, she can self-join via
    `slack_client.conversations_join(channel=...)` for public channels.
    """
    if thread_ts:
        result = slack_client.conversations_replies(
            channel=channel, ts=thread_ts, limit=limit,
        )
    else:
        result = slack_client.conversations_history(channel=channel, limit=limit)
    msgs = result.get("messages", [])
    if not msgs:
        return "(no messages)"
    if not thread_ts:
        msgs = list(reversed(msgs))  # history returns newest-first; flip to chronological
    lines = []
    for m in msgs:
        ts = m.get("ts", "")
        u = m.get("user", "") or m.get("bot_id", "")
        name = _get_user_name(m["user"]) if m.get("user") else (m.get("username") or m.get("bot_id") or "(system)")
        text = (m.get("text") or "").strip()
        lines.append(f"[{ts}] {name} ({u}): {text}")
    return "\n".join(lines)


def find_channel(query: str, limit: int = 25) -> str:
    """List public + private channels whose name contains `query` (case-insensitive)."""
    q = query.lower()
    out = []
    cursor = None
    while True:
        resp = slack_client.conversations_list(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
            exclude_archived=True,
        )
        for c in resp.get("channels", []):
            name = c.get("name", "")
            if q in name.lower():
                kind = "private" if c.get("is_private") else "public"
                member = " [member]" if c.get("is_member") else ""
                out.append(f"{c['id']}\t#{name}\t{kind}{member}")
                if len(out) >= limit:
                    return "\n".join(out)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return "\n".join(out) if out else f"(no channels matching '{query}')"


# ---------------------------------------------------------------------------
# Async message processing (handles Slack's 3-second deadline)
#
# Slack requires HTTP 200 within 3 seconds. Claude takes minutes.
# So we respond immediately and process in a background thread.
# ---------------------------------------------------------------------------


def _run_openai_fallback(
    *,
    policy,
    channel: str,
    thread_ts: str,
    user_id: str,
    prompt: str,
    on_text,
) -> str | None:
    """Run one profile-governed Codex turn and apply the shared harness claims."""
    validate_codex_runtime()
    for warning in preflight_room(policy):
        logger.warning(f"Cargo Chief fallback preflight: {warning}")
    workspace = prepare_thread_workspace(
        policy.root,
        channel=channel,
        thread=thread_ts,
        session_id=_get_openai_session(thread_ts),
    )
    for stale_path in (
        workspace.escalation_message_file,
        workspace.escalation_receipt_file,
        workspace.escalation_attempt_file,
        workspace.parking_claim_file,
        workspace.delegation_request_file,
        workspace.implementation_claim_file,
        workspace.delegate_pid_file,
    ):
        try:
            stale_path.unlink()
        except FileNotFoundError:
            pass
    bundle = resolve_thread_bundle(workspace)
    cwd = bundle or workspace.path
    saved_session_id = _get_openai_session(thread_ts)
    saved_cwd = _get_openai_session_cwd(thread_ts)
    session_id = saved_session_id if saved_cwd == cwd.resolve() else None
    command = build_codex_command(policy, cwd=cwd, session_id=session_id)
    governed_prompt = build_codex_prompt(
        policy,
        inbound_prompt=prompt,
        transport_python=Path(sys.executable),
        transport_script=SOURCE_DIR / "bot.py",
        escalation_message_file=workspace.escalation_message_file,
        bundle_claim_file=workspace.bundle_claim_file,
        parking_claim_file=workspace.parking_claim_file,
        delegation_request_file=workspace.delegation_request_file,
        implementation_claim_file=workspace.implementation_claim_file,
    )
    proc_env = {**os.environ}
    proc_env.update({
        "CLAUDE_THREAD_TS": thread_ts,
        "CLAUDE_CHANNEL_ID": channel,
        "CARGO_CHIEF_ESCALATION_CHANNEL": policy.escalation_channel,
        "CARGO_CHIEF_ESCALATION_MESSAGE_FILE": str(workspace.escalation_message_file),
        "CARGO_CHIEF_ESCALATION_RECEIPT_FILE": str(workspace.escalation_receipt_file),
        "CARGO_CHIEF_ESCALATION_ATTEMPT_FILE": str(workspace.escalation_attempt_file),
        "CARGO_CHIEF_THREAD_WORK_DIR": str(workspace.path),
        "CARGO_CHIEF_BUNDLE_CLAIM_FILE": str(workspace.bundle_claim_file),
        "CARGO_CHIEF_PARKING_CLAIM_FILE": str(workspace.parking_claim_file),
        "CARGO_CHIEF_ROOT": str(policy.root),
        "CARGO_CHIEF_DELEGATION_REQUEST_FILE": str(workspace.delegation_request_file),
        "CARGO_CHIEF_IMPLEMENTATION_CLAIM_FILE": str(workspace.implementation_claim_file),
        "CARGO_CHIEF_DELEGATION_BUDGET_FILE": str(workspace.delegation_budget_file),
        "CARGO_CHIEF_DELEGATE_PID_FILE": str(workspace.delegate_pid_file),
        "CARGO_CHIEF_DELEGATE_VERIFICATION_FILE": str(workspace.delegate_verification_file),
        "CARGO_CHIEF_AUDIT_LOG": str(AUDIT_LOG),
        "CARGO_CHIEF_OWNER_PROVIDER": "openai",
        "CARGO_CHIEF_OWNER_MODEL": policy.fallback_model,
        "CARGO_CHIEF_OWNER_EFFORT": policy.fallback_effort,
        "CARGO_CHIEF_CURRENT_USER": user_id,
        "CARGO_CHIEF_DELEGATE_TIMEOUT": str(CLAUDE_TIMEOUT),
    })
    if bundle:
        proc_env["CARGO_CHIEF_THREAD_BUNDLE_DIR"] = str(bundle)
    started = time.time()

    def track_process(process):
        with _live_sessions_lock:
            if process is None:
                _openai_processes.pop(thread_ts, None)
            else:
                _openai_processes[thread_ts] = process

    result = run_codex_turn(
        command,
        governed_prompt,
        cwd=str(cwd),
        env=proc_env,
        timeout=CLAUDE_TIMEOUT,
        on_process=track_process,
    )
    with _live_sessions_lock:
        stopped = thread_ts in _openai_stopped
        _openai_stopped.discard(thread_ts)
    if result.session_id:
        session_id = result.session_id
        _save_openai_session(thread_ts, session_id, cwd)

    if workspace.bundle_claim_file.exists():
        try:
            claimed = consume_bundle_claim(
                workspace, max_live_bundles=RUNTIME_POLICY.max_live_bundles
            )
        except SafetyError as exc:
            audit_logger.warning(
                "BUNDLE_REFUSED | PROVIDER:openai | USER:%s | CHANNEL:%s | THREAD:%s | REASON:%s",
                user_id, channel, thread_ts, str(exc),
            )
        else:
            if claimed:
                audit_logger.info(
                    "BUNDLE_BOUND | PROVIDER:openai | USER:%s | CHANNEL:%s | THREAD:%s | BUNDLE:%s",
                    user_id, channel, thread_ts, claimed.name,
                )

    parking = None
    parking_refused = None
    if workspace.parking_claim_file.exists():
        try:
            parking = consume_parking_claim(workspace)
        except SafetyError as exc:
            parking_refused = str(exc)
            audit_logger.warning(
                "PARKING_REFUSED | PROVIDER:openai | USER:%s | CHANNEL:%s | THREAD:%s | REASON:%s",
                user_id, channel, thread_ts, parking_refused,
            )
        else:
            if parking:
                audit_logger.info(
                    "WORK_PARKED | PROVIDER:openai | USER:%s | CHANNEL:%s | THREAD:%s | KIND:%s | RECORD:%s",
                    user_id, channel, thread_ts, parking.kind,
                    parking.path.relative_to(workspace.root),
                )

    delivered = workspace.escalation_receipt_file.is_file()
    attempted = workspace.escalation_attempt_file.is_file()
    verification_missing = workspace.delegate_verification_file.is_file()
    if attempted:
        try:
            workspace.escalation_attempt_file.unlink()
        except OSError:
            pass
    if not stopped and not result.error:
        _track_openai_model(channel, thread_ts, policy.fallback_model)
    if stopped:
        pass
    elif delivered:
        try:
            workspace.escalation_receipt_file.unlink()
        except OSError:
            pass
        on_text(private_escalation_status(
            delivered=True,
            parking=parking,
            parking_refused=bool(parking_refused),
        ))
    elif attempted:
        on_text(private_escalation_status(
            delivered=False, parking=parking, parking_refused=bool(parking_refused)
        ))
    elif result.error:
        logger.warning("OpenAI fallback turn failed in thread %s", thread_ts)
        on_text("OpenAI fallback could not complete this turn.")
    elif verification_missing:
        audit_logger.warning(
            "DELEGATION_VERIFICATION | PROVIDER:openai | USER:%s | CHANNEL:%s | "
            "THREAD:%s | OWNER_VERIFY_TOOLS:0 | STATUS:missing",
            user_id, channel, thread_ts,
        )
        on_text("Delegate result withheld: independent owner verification is missing.")
    else:
        for text_block in result.texts:
            on_text(text_block)

    audit_logger.info(
        "FALLBACK_INTERACTION | PROVIDER:openai | MODEL:%s | EFFORT:%s | USER:%s | CHANNEL:%s "
        "| THREAD:%s | SESSION:%s | RESP_BLOCKS:%d | DURATION:%.1fs",
        policy.fallback_model, policy.fallback_effort, user_id, channel, thread_ts,
        session_id or "none", len(result.texts), time.time() - started,
    )
    return session_id


def process_message_async(event: dict) -> None:
    """Process a message in a background thread.

    Uses long-lived Claude processes with stream-json I/O and a profile-governed
    Codex CLI turn after a recognized Claude credit limit. If a Claude process is
    already running for this thread, the message is piped to its stdin and
    queued automatically by the CLI. Otherwise a new process is spawned
    (resuming any prior session for the thread).
    """
    user_id = event.get("user", "")
    text = event.get("text", "").strip()
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    msg_ts = event.get("ts")
    reaction_channel = channel
    reaction_msg_ts = msg_ts

    # Defense in depth: every ingress path (including forwarded replies and
    # mid-turn steering) must pass the current Slack sender, before downloads
    # or any other filesystem-derived work occurs.
    try:
        authority = AuthorityPolicy.from_env()
    except SafetyError:
        log_unauthorized(event)
        return
    if not authority.allows(user_id):
        log_unauthorized(event)
        return

    # Resolve forwarding before room policy. Authorization belongs to the
    # current sender; execution policy belongs to the destination room.
    forward = _get_forward(thread_ts)
    original_thread = None
    if forward:
        original_thread = thread_ts
        thread_ts = forward["thread"]
        channel = forward["channel"]

    try:
        room_policy = resolve_room_policy(_load_model_config(), channel, user_id)
    except SafetyError as exc:
        logger.error(f"Refusing unsafe room {channel}: {exc}")
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"I could not start this Cargo Chief room safely: {exc}",
        )
        return

    use_openai = _limit_paused() or _get_openai_session(thread_ts) is not None

    # Replace user mentions with readable names
    text = re.sub(
        r"<@([A-Z0-9]+)>",
        lambda m: f"@{_get_user_name(m.group(1))}",
        text,
    ).strip()

    # Download attachments
    attached_files = download_slack_files(event)

    if not text and not attached_files:
        return

    if attached_files:
        paths = ", ".join(str(fp) for fp in attached_files)
        label = "Files attached" if len(attached_files) > 1 else "File attached"
        text = f"{label}: {paths}" + (f"\n\n{text}" if text else "")

    # Prepend sender attribution so Claude knows who sent this message
    sender_name = _get_user_name(user_id)

    # Cross-thread forwarding: if a reply landed in a thread that's been
    # registered as a forward (e.g., a DM Claude opened mid-task to ask
    # someone a question), rewrite the routing so the reply is delivered
    # into the original conversation with attribution. The eyes reaction
    # below still goes on the original message in its original channel.
    if forward:
        _remove_forward(original_thread)
        logger.info(
            f"Forwarded reply from {sender_name} in {original_thread} -> {thread_ts}"
        )

    # For channel messages (not DMs), let Claude decide if it should respond
    is_channel = event.get("channel_type") not in ("im", "mpim")
    has_existing_session = (
        _get_session(thread_ts) is not None or _get_openai_session(thread_ts) is not None
    )

    # Check if there's already a live process for this thread
    has_live_process = (
        thread_ts in _live_sessions and _live_sessions[thread_ts].proc.poll() is None
    )

    # If this is a thread reply and we have no saved session AND no live process,
    # fetch the full thread history so Claude has context on what was said before.
    is_thread_reply = thread_ts != msg_ts
    thread_context = None
    if not has_existing_session and not has_live_process and is_thread_reply:
        thread_context = _fetch_thread_context(channel, thread_ts, msg_ts)

    channel_type = event.get("channel_type", "")
    if not _channel_allowed(channel, channel_type):
        logger.info(f"Ignoring message in non-allowed channel {channel} ({_get_channel_name(channel)})")
        return

    # Inject the SKIP relevance filter for every message except 1:1 DMs.
    # 1:1 DM = always respond; any multi-person space (group DM, private or
    # public channel) = only respond when directly addressed.
    is_dm = channel_type == "im"

    # In public channels, private channels, and group DMs, count inbound messages
    # per thread so we can re-inject a shorter relevance reminder every
    # REMINDER_EVERY messages — Claude forgets the "only respond when addressed"
    # rule once a thread is long.
    is_reminder_space = channel_type in ("channel", "group", "mpim")
    show_reminder = False
    if is_reminder_space:
        with _live_sessions_lock:
            _thread_msg_counts[thread_ts] = _thread_msg_counts.get(thread_ts, 0) + 1
            count = _thread_msg_counts[thread_ts]
        # Only inside an ongoing thread — cold contact already gets the full filter.
        if (has_existing_session or has_live_process) and count % REMINDER_EVERY == 0:
            show_reminder = True

    prefix = ""
    if needs_relevance_prefix(
        event_type=event.get("type", ""),
        is_dm=is_dm,
        has_existing_session=has_existing_session,
        has_live_process=has_live_process,
        show_reminder=show_reminder,
    ):
        channel_name = _get_channel_name(channel)
        prefix = relevance_prefix(
            channel_name,
            BOT_DISPLAY_NAME,
            reminder=show_reminder,
        )

    text = prefix + build_authority_envelope(
        authority,
        sender_id=user_id,
        sender_name=sender_name,
        channel_id=channel,
        permission_mode=room_policy.permission_mode,
        message=text,
        thread_context=thread_context,
        forwarded_from=original_thread,
    )

    # Add eyes reaction as thinking indicator
    try:
        slack_client.reactions_add(channel=reaction_channel, name="eyes", timestamp=reaction_msg_ts)
    except Exception:
        pass

    # Get or create a live Claude process for this thread
    all_texts = []
    first_text_sent = False
    skip_detected = False
    fallback_requested = False

    def on_text(text_block: str):
        """Called for each text block Claude produces — post it to Slack immediately."""
        nonlocal first_text_sent, skip_detected, fallback_requested

        # Usage-limit notices are synthesized by the CLI, not the model.
        # Suppress them and replay the same authenticated turn through the
        # configured OpenAI fallback after the Claude result closes.
        if is_claude_limit_notice(text_block):
            _enter_limit_pause(text_block)
            fallback_requested = True
            logger.warning(f"Usage limit hit in thread {thread_ts}")
            return

        # Check for SKIP on the very first text block (channel relevance filter)
        if not first_text_sent and text_block.strip() == "SKIP":
            skip_detected = True
            return

        all_texts.append(text_block)

        # Auto-upload any file paths mentioned
        _auto_upload_files(text_block, channel, thread_ts=thread_ts)

        # Post to Slack
        post_response(channel, text_block, thread_ts=thread_ts)
        first_text_sent = True

    start = time.time()
    audit_session_id = _get_openai_session(thread_ts) if use_openai else None

    try:
        if use_openai:
            with _openai_turn_lock(thread_ts):
                audit_session_id = _run_openai_fallback(
                    policy=room_policy,
                    channel=channel,
                    thread_ts=thread_ts,
                    user_id=user_id,
                    prompt=text,
                    on_text=on_text,
                )
            session = None
        else:
            session = _get_or_create_live_session(thread_ts, channel, user_id=user_id)

        # Real-time steering: a Claude turn is already running in this thread — don't
        # hold the message until it finishes. Write it to stdin now; the CLI
        # delivers it at the next tool-call boundary inside the running turn,
        # exactly like typing without Esc in interactive Claude Code. The
        # running turn's on_text posts to this same thread, so replies route
        # correctly. If the turn ends in the race window the message simply
        # starts the next turn, and its eyes reaction is still drained by the
        # reader on that turn's result. `stop`/`esc` remains the hard
        # interrupt for aborting a slow tool call outright.
        if session and session.turn_lock.locked():
            _send_to_claude(session, text)
            session.pending_reactions.append((reaction_channel, reaction_msg_ts))
            audit_interaction(event, "(steered into running turn)", 0.0, session.session_id)
            logger.info(f"Steering message injected mid-turn in thread {thread_ts}")
            return

        # Acquire turn_lock — this serializes the send→wait cycle.
        # If another message is already being processed, we block here.
        if session:
            with session.turn_lock:
                session._on_text = on_text
                session._turn_done.clear()

                _send_to_claude(session, text)

                if not session._turn_done.wait(timeout=CLAUDE_TIMEOUT):
                    try: slack_client.reactions_remove(channel=reaction_channel, name="eyes", timestamp=reaction_msg_ts)
                    except Exception: pass
                    minutes = CLAUDE_TIMEOUT // 60
                    slack_client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text=f"Sorry, that timed out after {minutes} minutes. Try a simpler question?",
                    )
                    return

                audit_session_id = session.session_id
                if fallback_requested:
                    fallback_context = _fetch_thread_context(channel, thread_ts, msg_ts)
                    fallback_prompt = text
                    if fallback_context:
                        fallback_prompt += (
                            "\n\n[UNTRUSTED_FAILOVER_THREAD_CONTEXT]\n"
                            f"{fallback_context}\n[/UNTRUSTED_FAILOVER_THREAD_CONTEXT]"
                        )
                    with _openai_turn_lock(thread_ts):
                        audit_session_id = _run_openai_fallback(
                            policy=room_policy,
                            channel=channel,
                            thread_ts=thread_ts,
                            user_id=user_id,
                            prompt=fallback_prompt,
                            on_text=on_text,
                        )

                # Check if the process died without producing a response
                if not all_texts and not skip_detected and session.proc.poll() is not None:
                    try: slack_client.reactions_remove(channel=reaction_channel, name="eyes", timestamp=reaction_msg_ts)
                    except Exception: pass
                    logger.error(f"Claude process died without responding in thread {thread_ts}")
                    slack_client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text="Sorry, I lost my train of thought. Could you try sending that again?",
                    )
                    return

    except Exception as e:
        try: slack_client.reactions_remove(channel=reaction_channel, name="eyes", timestamp=reaction_msg_ts)
        except Exception: pass
        logger.error(f"Error processing message in thread {thread_ts}: {e}")
        slack_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"Something went wrong: {e}",
        )
        return

    duration = time.time() - start

    # If Claude decided not to respond (channel messages only), stay silent
    if skip_detected:
        try: slack_client.reactions_remove(channel=reaction_channel, name="eyes", timestamp=reaction_msg_ts)
        except Exception: pass
        logger.info(f"Skipped message from {user_id} in {channel} (not relevant)")
        return

    # Remove eyes reaction
    try:
        slack_client.reactions_remove(channel=reaction_channel, name="eyes", timestamp=reaction_msg_ts)
    except Exception:
        pass

    full_response = "\n\n".join(all_texts)
    audit_interaction(event, full_response, duration, audit_session_id)


# ---------------------------------------------------------------------------
# In-thread stop (the Esc key for Slack)
# ---------------------------------------------------------------------------


def _interrupt_session(session: LiveSession) -> bool:
    """Send the CLI an interrupt (the programmatic Esc). True if the turn ended cleanly."""
    try:
        payload = json.dumps({"type": "control_request",
                              "request_id": f"interrupt-{int(time.time() * 1000)}",
                              "request": {"subtype": "interrupt"}})
        with session.stdin_lock:
            session.proc.stdin.write(payload + "\n")
            session.proc.stdin.flush()
    except Exception as e:
        logger.warning(f"Interrupt write failed for {session.thread_ts}: {e}")
    if session._turn_done.wait(timeout=5):
        return True
    # Interrupt didn't land — hard-kill; the thread resumes via --resume next message
    try:
        session.proc.terminate()
    except Exception:
        pass
    session._turn_done.set()
    return False


def _maybe_stop_from_message(event: dict) -> bool:
    """In-thread Esc: Slack blocks slash commands in thread reply boxes, so a
    bare 'stop' (or 'esc') in a thread with a running turn interrupts it
    instead of queueing as a normal message. Returns True if handled.

    Exact-match only — sentences containing 'stop' pass through untouched,
    and with no running turn the word falls through as a normal message.
    Authorized users only: interrupting a run is a control action, even in
    channels where unauthorized users may otherwise talk to the bot.
    """
    if not is_authorized(event.get("user", "")):
        return False
    text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip().lower()
    if text not in ("stop", "esc"):
        return False
    thread_ts = event.get("thread_ts") or event.get("ts")
    delegate_stopped = False
    try:
        policy = resolve_room_policy(
            _load_model_config(), event.get("channel", ""), event.get("user", "")
        )
        workspace = prepare_thread_workspace(
            policy.root, channel=event.get("channel", ""), thread=thread_ts
        )
        if workspace.delegate_pid_file.is_file() and not workspace.delegate_pid_file.is_symlink():
            pid_text = workspace.delegate_pid_file.read_text(encoding="utf-8").strip()
            if pid_text.isdigit() and int(pid_text) > 1:
                os.kill(int(pid_text), signal.SIGTERM)
                delegate_stopped = True
            workspace.delegate_pid_file.unlink(missing_ok=True)
    except (OSError, SafetyError):
        pass
    with _live_sessions_lock:
        session = _live_sessions.get(thread_ts)
        openai_process = _openai_processes.get(thread_ts)
    if openai_process and openai_process.poll() is None:
        with _live_sessions_lock:
            _openai_stopped.add(thread_ts)
        try:
            openai_process.terminate()
        except Exception:
            return False
        note = "stopped the OpenAI fallback mid-run — tell me where to go instead"
        slack_client.chat_postMessage(
            channel=event.get("channel"), thread_ts=thread_ts,
            text=f"Stopped: {note}",
            blocks=[{"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":octagonal_sign: _{note}_"}]}],
        )
        return True
    if not session or session.proc.poll() is not None or not session.turn_lock.locked():
        if delegate_stopped:
            slack_client.chat_postMessage(
                channel=event.get("channel"), thread_ts=thread_ts,
                text="Stopped: stopped the active governed delegate",
            )
            return True
        return False  # nothing running here — treat as a normal message

    def _do_stop():
        clean = _interrupt_session(session)
        note = ("stopped mid-run — tell me where to go instead" if clean
                else "had to hard-kill the process; the thread resumes with full context on your next message")
        try:
            slack_client.chat_postMessage(
                channel=session.channel, thread_ts=session.thread_ts,
                text=f"Stopped: {note}",
                blocks=[{"type": "context", "elements": [
                    {"type": "mrkdwn", "text": f":octagonal_sign: _{note}_"}]}],
            )
        except Exception:
            pass

    threading.Thread(target=_do_stop, daemon=True).start()
    return True


def _maybe_delegation_budget_command(event: dict) -> bool:
    """Handle exact per-thread budget controls from a named approver."""
    text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip().lower()
    match = re.fullmatch(r"delegation budget (status|reset|set\s+([1-9][0-9]*))", text)
    if not match:
        return False
    user_id = event.get("user", "")
    try:
        authority = AuthorityPolicy.from_env()
        if not authority.can_approve(user_id):
            raise SafetyError("named approver required")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        policy = resolve_room_policy(_load_model_config(), channel, user_id)
        workspace = prepare_thread_workspace(
            policy.root, channel=channel, thread=thread_ts
        )
        action = match.group(1)
        if action == "status":
            state = budget_status(workspace.delegation_budget_file)
        elif action == "reset":
            state = update_budget(workspace.delegation_budget_file, reset=True)
        else:
            state = update_budget(
                workspace.delegation_budget_file, limit=int(match.group(2))
            )
        if state["used"] < state["limit"]:
            marker = workspace.delegate_verification_file
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                if metadata.get("status") == "budget_exhausted":
                    marker.unlink()
            except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
                pass
    except (SafetyError, ValueError, OSError) as exc:
        slack_client.chat_postMessage(
            channel=event.get("channel"),
            thread_ts=event.get("thread_ts") or event.get("ts"),
            text=f"Delegation budget refused: {exc}",
        )
        return True
    slack_client.chat_postMessage(
        channel=event.get("channel"),
        thread_ts=event.get("thread_ts") or event.get("ts"),
        text=f"Delegation budget: {state['used']}/{state['limit']} tokens used",
    )
    audit_logger.info(format_audit_metadata(
        "DELEGATION_BUDGET", user=user_id, channel=event.get("channel", ""),
        thread=event.get("thread_ts") or event.get("ts") or "unknown",
        action=match.group(1).split()[0], used=state["used"], limit=state["limit"],
    ))
    return True


# ---------------------------------------------------------------------------
# Slack event handlers
# ---------------------------------------------------------------------------


@app.event("message")
def handle_message(event, say):
    """Handle DMs and channel messages."""
    subtype = event.get("subtype")
    if subtype and subtype != "file_share":
        return

    # Skip @mentions in channels — those are handled by handle_mention() via
    # the app_mention event.  Without this guard, Slack fires BOTH a "message"
    # event and an "app_mention" event for the same message, causing duplicate
    # responses.
    if BOT_USER_ID:
        text = event.get("text", "")
        if event.get("channel_type") != "im" and f"<@{BOT_USER_ID}>" in text:
            return

    user_id = event.get("user", "")
    if not is_authorized(user_id):
        log_unauthorized(event)
        say(text="I only respond to authorized users.", thread_ts=event.get("ts"))
        return

    # In-thread Esc: bare "stop" while a turn is running interrupts it
    if _maybe_stop_from_message(event):
        return
    if _maybe_delegation_budget_command(event):
        return

    # Process async — return immediately so Slack gets its 200
    threading.Thread(target=process_message_async, args=(event,), daemon=True).start()


@app.event("app_mention")
def handle_mention(event, say):
    """Handle @bot mentions in channels."""
    user_id = event.get("user", "")
    channel = event.get("channel", "")

    if not _channel_allowed(channel, event.get("channel_type", "")):
        logger.info(f"Ignoring mention in non-allowed channel {channel} ({_get_channel_name(channel)})")
        return

    if not is_authorized(user_id):
        log_unauthorized(event)
        say(text="I only respond to authorized users.", thread_ts=event.get("ts"))
        return

    # "@bot stop" in a thread = in-thread Esc
    if _maybe_stop_from_message(event):
        return
    if _maybe_delegation_budget_command(event):
        return

    threading.Thread(target=process_message_async, args=(event,), daemon=True).start()


# Catch-all for events we subscribe to but don't handle
@app.event("member_joined_channel")
def handle_member_joined(event):
    pass


@app.event("reaction_added")
def handle_reaction(event):
    pass


@app.event("file_shared")
def handle_file_shared(event):
    pass


# ---------------------------------------------------------------------------
# Interactive actions (Block Kit buttons)
# ---------------------------------------------------------------------------


@app.action("vote_strong")
def handle_vote_strong(ack, body):
    ack()
    user_id = body["user"]["id"]
    if not is_authorized(user_id):
        log_unauthorized(body)
        return
    vote_key = body["actions"][0]["value"]
    threading.Thread(target=_handle_vote, args=("vote_strong", vote_key, user_id), daemon=True).start()


@app.action("vote_pass")
def handle_vote_pass(ack, body):
    ack()
    user_id = body["user"]["id"]
    if not is_authorized(user_id):
        log_unauthorized(body)
        return
    vote_key = body["actions"][0]["value"]
    threading.Thread(target=_handle_vote, args=("vote_pass", vote_key, user_id), daemon=True).start()


# Registered after the vote handlers — Bolt dispatches to the first matching
# listener, so vote_* clicks never reach this catch-all.
@app.action(re.compile(r"^(?!vote_).*"))
def handle_block_action(ack, body):
    """Route button clicks / menu selections into the thread's Claude session.

    Any interactive element the bot (or Claude via the SDK) posts lands here.
    The click becomes a structured message in the same thread, so Claude sees
    '[Name clicked "Send it"]' and responds there. URL buttons are
    navigational — ack only.
    """
    ack()
    try:
        action = body["actions"][0]
        if action.get("url"):
            return

        user_id = body["user"]["id"]
        if not is_authorized(user_id):
            log_unauthorized(body)
            return

        atype = action.get("type", "")
        label = action.get("text", {}).get("text", "")
        value = action.get("value", "")
        if atype in ("static_select", "radio_buttons"):
            opt = action.get("selected_option") or {}
            label = opt.get("text", {}).get("text", label)
            value = opt.get("value", value)
        elif atype in ("checkboxes", "multi_static_select"):
            opts = action.get("selected_options") or []
            value = ", ".join(o.get("text", {}).get("text") or o.get("value", "") for o in opts)
            label = label or "selection"
        elif atype == "datepicker":
            value = action.get("selected_date") or ""
            label = label or "date"
        elif atype == "timepicker":
            value = action.get("selected_time") or ""
            label = label or "time"
        elif atype == "plain_text_input":
            label = label or "text input"
        desc = f'"{label}"' if label else f"action {action.get('action_id', '?')}"
        if value and value != label:
            desc += f" (value: {value})"

        message = body["message"]
        event = {
            "user": user_id,
            "channel": body["channel"]["id"],
            "ts": message["ts"],  # :eyes: lands on the clicked message
            "thread_ts": message.get("thread_ts") or message["ts"],
            # The configured bot name keeps this from being SKIPped by the
            # channel-relevance filter when the click starts a fresh session
            "text": f"[Button click for {BOT_DISPLAY_NAME}: {_get_user_name(user_id)} clicked {desc} "
                    f"(action_id: {action.get('action_id', '')})]",
            # unique per click so repeat clicks on one message aren't deduped
            "client_msg_id": f"{action.get('action_id', '')}:{action.get('action_ts', '')}",
            "channel_type": body["channel"].get("name") == "directmessage" and "im" or "channel",
        }
        threading.Thread(target=process_message_async, args=(event,), daemon=True).start()
    except Exception as e:
        logger.error(f"Failed to route block action: {e}")


# ---------------------------------------------------------------------------
# Flask app (HTTP Events API)
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)
handler = SlackRequestHandler(app)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/slack/actions", methods=["POST"])
def slack_actions():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot": "ai-employee"})


@flask_app.route("/internal/forward", methods=["POST"])
def register_forward_endpoint():
    """Register a cross-thread forward. Called by `bot.py --send --forward-to`.

    Localhost-only — same-machine CLI talking to the running server. Looks up
    the target thread's live session for channel + session_id + user_id and
    persists the forward to .forwards.json.
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    from_thread = data.get("from_thread")
    to_thread = data.get("to_thread")
    if not from_thread or not to_thread:
        return jsonify({"error": "from_thread and to_thread required"}), 400
    with _live_sessions_lock:
        target = _live_sessions.get(to_thread)
        target_channel = target.channel if target else ""
        target_session_id = target.session_id if target else _get_session(to_thread)
        target_user_id = target.user_id if target else ""
    if not target_channel:
        return jsonify({
            "error": "no_live_session",
            "detail": f"no live session for thread {to_thread} — forward requires the target to be alive at registration",
        }), 404
    _add_forward(
        from_thread=from_thread,
        to_thread=to_thread,
        to_channel=target_channel,
        session_id=target_session_id,
        user_id=target_user_id,
    )
    logger.info(f"Registered forward: {from_thread} -> {to_thread}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="AI Employee — Slack Bot powered by Claude Code")
    parser.add_argument(
        "--send", nargs=2, metavar=("USER_ID", "MESSAGE"),
        help="Send a proactive DM and exit",
    )
    parser.add_argument(
        "--send-result", metavar="USER_ID",
        help="Read Claude JSON from stdin, send as DM with session linking",
    )
    parser.add_argument(
        "--thread", metavar="THREAD_TS",
        help="Reply in an existing thread (use with --send or --send-result)",
    )
    parser.add_argument(
        "--channel", nargs=2, metavar=("CHANNEL", "MESSAGE"),
        help="Post a message to a channel and exit",
    )
    parser.add_argument(
        "--escalate", action="store_true",
        help="Post to the harness-configured private escalation route and exit",
    )
    parser.add_argument(
        "--history", metavar="CHANNEL_ID",
        help="Print recent messages from a channel (or a thread if --thread is set)",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max messages to return for --history (default 50)",
    )
    parser.add_argument(
        "--find-channel", metavar="QUERY",
        help="List channels whose name contains QUERY (case-insensitive)",
    )
    parser.add_argument(
        "--with-votes", action="store_true",
        help="Post the message with Block Kit voting buttons (use with --channel or --send)",
    )
    parser.add_argument(
        "--forward-to", metavar="THREAD_TS", dest="forward_to",
        help="When a reply lands in this new DM thread, route it into the live session for THREAD_TS instead of starting a new conversation. Requires the running bot server to have a live session for THREAD_TS.",
    )
    parser.add_argument(
        "--session-id", metavar="SESSION_ID", dest="session_id",
        help="Register this Claude session_id as the resume target for replies in this DM thread. Use for cron jobs that DM someone, exit, and want to continue where they left off when the person replies.",
    )
    args = parser.parse_args()

    # CLI modes — send and exit
    if args.send:
        if args.with_votes:
            response = slack_client.conversations_open(users=[args.send[0]])
            channel_id = response["channel"]["id"]
            ts = post_with_votes(channel_id, args.send[1], thread_ts=args.thread)
            if ts:
                print(ts)
        else:
            thread_ts = send_dm(
                args.send[0], args.send[1],
                session_id=args.session_id,
                thread_ts=args.thread,
                forward_to=args.forward_to,
            )
            if thread_ts:
                print(thread_ts)
        return

    if args.send_result:
        raw = sys.stdin.read().strip()
        try:
            data = json.loads(raw)
            message = data.get("result", "")
            session_id = data.get("session_id")
        except json.JSONDecodeError:
            message = raw
            session_id = None
        if not message:
            message = "Job completed but produced no output."
        send_dm(args.send_result, message, session_id=session_id, thread_ts=args.thread)
        return

    if args.channel:
        if args.with_votes:
            ts = post_with_votes(args.channel[0], args.channel[1], thread_ts=args.thread)
            if ts:
                print(ts)
        else:
            send_to_channel(args.channel[0], args.channel[1], thread_ts=args.thread)
        return

    if args.escalate:
        escalation_channel = os.environ.get("CARGO_CHIEF_ESCALATION_CHANNEL", "")
        if not re.fullmatch(r"[CDG][A-Z0-9]+", escalation_channel):
            raise SystemExit("private escalation route is unavailable")
        message_path_value = os.environ.get("CARGO_CHIEF_ESCALATION_MESSAGE_FILE", "")
        if not message_path_value:
            raise SystemExit("private escalation message path is unavailable")
        message_path = Path(message_path_value)
        attempt_path_value = os.environ.get("CARGO_CHIEF_ESCALATION_ATTEMPT_FILE", "")
        if attempt_path_value:
            attempt_path = Path(attempt_path_value)
            attempt_path.write_text("attempted\n", encoding="utf-8")
            attempt_path.chmod(0o600)
        try:
            message = message_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit("private escalation message is unavailable") from exc
        finally:
            try:
                message_path.unlink()
            except OSError:
                pass
        if not message:
            raise SystemExit("private escalation message is empty")
        receipt_path_value = os.environ.get("CARGO_CHIEF_ESCALATION_RECEIPT_FILE", "")
        if not receipt_path_value:
            raise SystemExit("private escalation receipt path is unavailable")
        receipt_path = Path(receipt_path_value)
        send_to_channel(escalation_channel, message)
        receipt_path.write_text("delivered\n", encoding="utf-8")
        receipt_path.chmod(0o600)
        return

    if args.history:
        print(fetch_channel_history(args.history, limit=args.limit, thread_ts=args.thread))
        return

    if args.find_channel:
        print(find_channel(args.find_channel))
        return

    # Server mode.  An empty allowlist is a startup error, never "allow all".
    try:
        authority = AuthorityPolicy.from_env()
    except SafetyError as exc:
        logger.error(f"Cargo Chief authorization configuration refused startup: {exc}")
        raise SystemExit(1)

    if not SLACK_BOT_TOKEN or not SLACK_SIGNING_SECRET:
        logger.error("Missing SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET in .env")
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))

    logger.info(f"{BOT_DISPLAY_NAME} starting on port {PORT}")
    logger.info(f"Authorized users: {sorted(authority.authorized_users)}")
    logger.info(f"Named approvers: {sorted(authority.approvers)}")
    logger.info(f"Allowed channel substrings: {ALLOWED_CHANNEL_SUBSTRINGS or '(all channels)'}")
    logger.info("Project roots are resolved per room from model-config.json")
    logger.info(
        f"Runtime: {RUNTIME_POLICY.root} (sessions={MAX_LIVE_SESSIONS}, "
        f"bundles={RUNTIME_POLICY.max_live_bundles}, timeout={CLAUDE_TIMEOUT}s, "
        "file_transfer=off, transcript_search=off)"
    )

    removed_temp = RUNTIME_POLICY.cleanup_temp()
    if removed_temp:
        logger.info(f"Removed {removed_temp} stale runtime temp file(s)")

    removed_workspaces = cleanup_thread_workspaces(WORKSPACE_ROOT)
    if removed_workspaces:
        logger.info(f"Removed {removed_workspaces} stale thread workspace(s)")

    # Garbage-collect stale forward entries (>14 days old) from prior runs
    _gc_forwards()

    # Start idle session cleanup thread
    threading.Thread(target=_cleanup_idle_sessions, daemon=True).start()

    serve_http(flask_app, PORT)


if __name__ == "__main__":
    main()
