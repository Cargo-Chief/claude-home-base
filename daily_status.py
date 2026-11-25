"""The daily team-status post: draft validation, review binding and rendering.

The model writes a structured draft and a separate review pass writes a verdict bound to that
draft's hash. Only the harness renders and posts, so mentions and links are never model text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from thread_handoff import slack_escape


DRAFT_SCHEMA = "daily-status/v1"
REVIEW_SCHEMA = "daily-status-review/v1"
SECTIONS = (
    ("yesterday", "What I did"),
    ("finished", "Finished"),
    ("working_on", "Working on now"),
    ("blocked", "Blocked"),
    ("waiting_on_operator", "Waiting on"),
)
MAX_ITEMS = 10
MAX_TEXT = 240
MAX_LINKS = 3
MAX_FILE_BYTES = 64 * 1024
CHANNEL_ID_RE = re.compile(r"[CG][A-Z0-9]+")
USER_ID_RE = re.compile(r"[UW][A-Z0-9]+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
LINK_RULES = (
    (re.compile(r"https://github\.com/Cargo-Chief/([A-Za-z0-9._-]+)/pull/(\d+)"), lambda m: f"{m[1]}#{m[2]}"),
    (re.compile(r"https://cargochief\.atlassian\.net/browse/(CN-\d+)"), lambda m: m[1]),
    (re.compile(r"https://cargochief\.slack\.com/archives/[CGD][A-Z0-9]+/p\d{16}"), lambda m: "thread"),
)
UNAVAILABLE_TEXT = "Daily status unavailable today: the status job did not produce a draft."
WITHHELD_TEXT = "Daily status withheld today: its privacy check did not clear the draft."


class StatusError(ValueError):
    """A draft, review or configuration that must not be posted."""


@dataclass(frozen=True)
class StatusConfig:
    channel: str
    mention_user: str


def configured_team_status(env: Mapping[str, str] | None = None) -> StatusConfig:
    values = os.environ if env is None else env
    channel = values.get("TEAM_STATUS_CHANNEL_ID", "").strip()
    mention = values.get("TEAM_STATUS_MENTION_USER_ID", "").strip()
    if not CHANNEL_ID_RE.fullmatch(channel):
        raise StatusError("TEAM_STATUS_CHANNEL_ID must be a Slack channel ID beginning with C or G")
    if mention and not USER_ID_RE.fullmatch(mention):
        raise StatusError("TEAM_STATUS_MENTION_USER_ID must be a Slack user ID beginning with U or W")
    return StatusConfig(channel, mention)


def post_request_is_exact(argv: list[str]) -> bool:
    """The post capability is exactly one flag and one run directory."""
    return len(argv) == 2 and argv[0] == "--post-daily-status" and not argv[1].startswith("-")


def validate_run_dir(raw: str, workspace_root: Path) -> Path:
    """A run directory is work/daily-status/<date> under the workspace root, never a symlink."""
    base = (workspace_root / "work" / "daily-status").resolve()
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink():
        raise StatusError("run directory must be an absolute, non-symlink path")
    resolved = path.resolve()
    if resolved.parent != base or not DATE_RE.fullmatch(resolved.name) or not resolved.is_dir():
        raise StatusError("run directory must be work/daily-status/<YYYY-MM-DD>")
    return resolved


def _read_json(path: Path) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.is_file():
        raise StatusError(f"{path.name} is missing or not a regular file")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise StatusError(f"{path.name} is too large")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StatusError(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise StatusError(f"{path.name} must be a JSON object")
    return data, raw


def _link_label(url: str) -> str:
    for pattern, label in LINK_RULES:
        match = pattern.fullmatch(url)
        if match:
            return label(match)
    raise StatusError("a link is not a Cargo-Chief PR, CN ticket or Slack thread URL")


def validate_draft(data: dict, *, date: str) -> dict[str, list[dict]]:
    if data.get("schema") != DRAFT_SCHEMA:
        raise StatusError("draft schema is not daily-status/v1")
    if data.get("date") != date:
        raise StatusError("draft date does not match the run")
    covers = data.get("covers")
    if not isinstance(covers, str) or not covers.strip() or len(covers) > 60 or "\n" in covers:
        raise StatusError("draft covers must be a short single line")
    sections = data.get("sections")
    if not isinstance(sections, dict) or set(sections) != {key for key, _ in SECTIONS}:
        raise StatusError("draft sections must be exactly the five status sections")
    clean: dict[str, list[dict]] = {}
    for key, _ in SECTIONS:
        items = sections[key]
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise StatusError(f"section {key} must be a list of at most {MAX_ITEMS} items")
        clean[key] = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {"text", "links"}:
                raise StatusError(f"section {key} has an item that is not {{text, links}}")
            text = item.get("text")
            links = item.get("links", [])
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\n" in text:
                raise StatusError(f"section {key} has an empty, multi-line or overlong item")
            if not isinstance(links, list) or len(links) > MAX_LINKS or not all(isinstance(u, str) for u in links):
                raise StatusError(f"section {key} has more than {MAX_LINKS} links or a non-string link")
            for url in links:
                _link_label(url)
            clean[key].append({"text": text.strip(), "links": links})
    return clean


def validate_review(data: dict, draft_raw: bytes) -> bool:
    """True when the review clears this exact draft; False when it withholds it."""
    if data.get("schema") != REVIEW_SCHEMA:
        raise StatusError("review schema is not daily-status-review/v1")
    if data.get("draft_sha256") != hashlib.sha256(draft_raw).hexdigest():
        raise StatusError("review is not bound to this draft")
    verdict = data.get("verdict")
    if verdict not in ("clear", "withhold"):
        raise StatusError("review verdict must be clear or withhold")
    return verdict == "clear"


def render(covers: str, sections: dict[str, list[dict]], mention_user: str) -> str:
    lines = [f"*Daily status* (covers {slack_escape(covers.strip())})"]
    for key, title in SECTIONS:
        if key == "waiting_on_operator":
            title = f"Waiting on <@{mention_user}>" if mention_user and sections[key] else "Waiting on you"
        lines.append("")
        lines.append(f"*{title}*")
        if not sections[key]:
            lines.append("• nothing")
        for item in sections[key]:
            links = " ".join(f"<{url}|{_link_label(url)}>" for url in item["links"])
            lines.append(f"• {slack_escape(item['text'])}" + (f" ({links})" if links else ""))
    return "\n".join(lines)


def build_post(run_dir: Path, mention_user: str) -> tuple[str, str]:
    """Return (outcome, text) for a run directory: posted, withheld or unavailable."""
    draft_path = run_dir / "draft.json"
    if not draft_path.exists():
        return "unavailable", UNAVAILABLE_TEXT
    try:
        draft, draft_raw = _read_json(draft_path)
        sections = validate_draft(draft, date=run_dir.name)
        review, _ = _read_json(run_dir / "review.json")
        cleared = validate_review(review, draft_raw)
    except StatusError:
        return "withheld", WITHHELD_TEXT
    if not cleared:
        return "withheld", WITHHELD_TEXT
    return "posted", render(draft["covers"], sections, mention_user)
