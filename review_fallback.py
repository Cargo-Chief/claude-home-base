"""Running an admitted peer review when Claude cannot: on the Codex fallback, or later.

A review on the Codex fallback runs in a review-only sandbox with a read-only GitHub token. It
writes its review and a post request into its own workspace, and the harness posts them through
review_post.py after the turn, so the model never holds a token that can write to GitHub. A review
that neither provider can run is deferred: recorded here and replayed through the full admission
guards once Claude's usage-limit pause ends.

No Slack or bot imports, so the sandbox, the post request and the deferral record are testable.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Callable, Iterable, Mapping

from cargo_chief_safety import (
    RoomPolicy,
    TOKEN_PERMISSIONS_ENV,
    review_turn_env,
)


# The Codex permissions profile a review turn runs under; defined entirely by the command's -c flags.
CODEX_REVIEW_PERMISSIONS = "cc_review"
# The token the review model's process carries: enough to read the PR, its code and its threads.
REVIEW_READ_TOKEN_PERMISSIONS = {"contents": "read", "pull_requests": "read"}
# Paths under $HOME the review sandbox may not read: credentials and the App key that mints tokens.
REVIEW_DENIED_HOME_PATHS = (
    ".config/cargo-chief", ".config/gh", ".config/git", ".aws", ".ssh", ".git-credentials", ".netrc",
    ".claude/.credentials.json", ".claude.json",
)
# What `git fetch` writes in a clone's git dir; hooks/ and config stay read-only.
GIT_FETCH_PATHS = ("objects", "refs", "logs", "packed-refs", "FETCH_HEAD")

REVIEW_FILE_NAME = "review.json"
POST_REQUEST_FILE_NAME = "review-post.json"
POST_REQUEST_MAX_BYTES = 16 * 1024
REVIEW_FILE_MAX_BYTES = 1024 * 1024
MAX_RESOLVE_IDS = 100
POST_TIMEOUT_SECONDS = 300
VERDICTS = ("comment", "request_changes", "approve")
# A GraphQL node id; it never starts with "-", so it cannot read as an option to review_post.py.
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_=-]{0,199}$")
_PR_REPO_RE = re.compile(r"^https://github\.com/Cargo-Chief/([A-Za-z0-9._-]+)/pull/\d+$")

DEFER_WINDOW_SECONDS = 24 * 3600
DEFERRED_FIRST_AT_KEY = "cc_deferred_first_at"
# Event fields a replay needs; everything else Slack sent is dropped from the record.
_EVENT_FIELDS = ("type", "user", "bot_id", "subtype", "channel", "channel_type", "ts", "thread_ts", "text")


class PostRequestError(RuntimeError):
    """The review's post request or review file is unusable, so nothing is posted."""


class DeferStoreError(RuntimeError):
    """The deferred-review record cannot be read, so nothing is deferred or replayed."""


def pr_repo(pr_url: str) -> str | None:
    match = _PR_REPO_RE.match(pr_url or "")
    return match.group(1).lower() if match else None


def _origin_url(checkout: Path) -> str | None:
    """The origin URL from a clone's own .git/config; None for a worktree, a missing or odd clone."""
    git_dir = checkout / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return None
    try:
        text = (git_dir / "config").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    in_origin = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped.replace(" ", "") in ('[remote"origin"]',)
            continue
        if in_origin:
            key, _, value = stripped.partition("=")
            if key.strip() == "url":
                return value.strip()
    return None


def _names_repo(url: str, repo: str) -> bool:
    match = re.search(r"[:/]Cargo-Chief/([A-Za-z0-9._-]+?)(?:\.git)?/?$", url, re.IGNORECASE)
    return bool(match) and match.group(1).lower() == repo.lower()


def review_checkout(root: Path, pr_url: str) -> Path | None:
    """The workspace's shared clone of the PR's repo, found by its origin URL, or None.

    The review sandbox may write only that clone's .git, which fetching the PR's refs needs.
    """
    repo = pr_repo(pr_url)
    if not repo:
        return None
    root = Path(root)
    repos = root / "repos"
    candidates = [repos / repo, root / repo]
    try:
        candidates.extend(sorted(path for path in repos.iterdir() if path.is_dir()))
    except OSError:
        pass
    candidates.extend(root / name for name in ("docs", "agent-kit", "claude-home-base"))
    for checkout in dict.fromkeys(candidates):
        url = _origin_url(checkout)
        if url and _names_repo(url, repo):
            return checkout.resolve()
    return None


def _toml_string(value: str) -> str:
    return json.dumps(value)


def app_key_path(app_config: Path) -> Path | None:
    """The App private key path named in the wrapper's config, or None; the key itself is never read."""
    try:
        text = Path(app_config).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == "GITHUB_APP_PRIVATE_KEY":
            path = Path(value.strip().strip("'\"")).expanduser()
            return path if path.is_absolute() else None
    return None


def review_denied_paths(home: Path, *, codex_home: Path, extra: Iterable[Path | None] = ()) -> list[Path]:
    """Everything the review sandbox may not read: credential homes, the App key wherever it lives,
    the harness's env file and runtime directory, and Codex's own login."""
    paths = [home / path for path in REVIEW_DENIED_HOME_PATHS]
    paths.append(codex_home / "auth.json")
    paths.extend(Path(path) for path in extra if path)
    return list(dict.fromkeys(paths))


def git_fetch_paths(git_dir: Path) -> list[Path]:
    return [git_dir / name for name in GIT_FETCH_PATHS]


def build_codex_review_command(
    policy: RoomPolicy, *, cwd: Path, writable: Iterable[Path], denied: Iterable[Path], codex_home: Path,
) -> list[str]:
    """A review-only Codex command: no approvals, apps, MCP servers, user config or learned rules.

    The sandbox extends the working directory with ``writable``, keeps CODEX_HOME read-only so the
    turn cannot steer later Codex turns, and makes ``denied`` unreadable so it cannot mint a GitHub
    token of its own. Network stays on for gh and git reads under the read-only token the harness
    wrapper passes in. Nothing in the turn starts another Codex, so CODEX_HOME need not be writable.
    """
    filesystem = {str(codex_home): "read"}
    for path in writable:
        filesystem[str(path)] = "write"
    for path in denied:
        filesystem[str(path)] = "none"
    table = "{" + ", ".join(f"{_toml_string(k)} = {_toml_string(v)}" for k, v in filesystem.items()) + "}"
    profile = f"permissions.{CODEX_REVIEW_PERMISSIONS}"
    return [
        "codex", "--profile", "cargo-chief",
        "-c", f'model_reasoning_effort="{policy.fallback_effort}"',
        "-c", "features.multi_agent=false",
        # The ChatGPT account's connected apps (GitHub, Slack, mail...) and any MCP server would act
        # outside the sandbox and the read-only token, as --strict-mcp-config prevents on Claude.
        "-c", "features.apps=false",
        "-c", "mcp_servers={}",
        "-c", 'approval_policy="never"',
        "-c", f'default_permissions="{CODEX_REVIEW_PERMISSIONS}"',
        "-c", f'{profile}.extends=":workspace"',
        "-c", f"{profile}.filesystem={table}",
        "-c", f"{profile}.network.enabled=true",
        "-c", f'{profile}.network.mode="full"',
        # The env is stripped by codex_review_env, so the token the wrapper adds is what reaches gh.
        "-c", "shell_environment_policy.ignore_default_excludes=true",
        "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--json",
        "--model", policy.fallback_model, "--cd", str(cwd), "--skip-git-repo-check", "-",
    ]


def codex_review_env(base: Mapping[str, str], **review: object) -> dict[str, str]:
    """review_turn_env without Claude's credentials, asking the wrapper for a read-only token."""
    env = {
        key: value for key, value in review_turn_env(base, **review).items()
        if key not in ("CLAUDE_CODE_OAUTH_TOKEN", "CARGO_CHIEF_ENV_FILE") and not key.startswith("ANTHROPIC_")
    }
    env[TOKEN_PERMISSIONS_ENV] = json.dumps(REVIEW_READ_TOKEN_PERMISSIONS, separators=(",", ":"))
    return env


def _read_regular_file(path: Path, limit: int) -> bytes:
    """A regular file's bytes, never through a symlink, at most ``limit`` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PostRequestError(f"{path.name} is not a plain file") from exc
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise PostRequestError(f"{path.name} is not a plain file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise PostRequestError(f"{path.name} is larger than {limit} bytes")
    return data


@dataclass(frozen=True)
class PostRequest:
    verdict: str
    resolve: tuple[str, ...]
    review: bytes


def read_post_request(workspace: Path) -> PostRequest | None:
    """The turn's post request and review, validated; None when the turn wrote no post request."""
    try:
        raw = _read_regular_file(workspace / POST_REQUEST_FILE_NAME, POST_REQUEST_MAX_BYTES)
    except FileNotFoundError:
        return None
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PostRequestError(f"{POST_REQUEST_FILE_NAME} is not JSON") from exc
    if not isinstance(request, dict) or set(request) - {"verdict", "resolve"}:
        raise PostRequestError(f"{POST_REQUEST_FILE_NAME} must hold only verdict and resolve")
    verdict = request.get("verdict")
    if verdict not in VERDICTS:
        raise PostRequestError(f"verdict must be one of {', '.join(VERDICTS)}")
    resolve = request.get("resolve", [])
    if (not isinstance(resolve, list) or len(resolve) > MAX_RESOLVE_IDS
            or not all(isinstance(item, str) and _THREAD_ID_RE.match(item) for item in resolve)):
        raise PostRequestError("resolve must be a list of review thread IDs")
    try:
        review = _read_regular_file(workspace / REVIEW_FILE_NAME, REVIEW_FILE_MAX_BYTES)
    except FileNotFoundError as exc:
        raise PostRequestError(f"{REVIEW_FILE_NAME} is missing") from exc
    return PostRequest(verdict=verdict, resolve=tuple(dict.fromkeys(resolve)), review=review)


def post_review(
    request: PostRequest,
    *,
    pr: str,
    python: str,
    wrapper: Path,
    script: Path,
    env: Mapping[str, str],
    scratch_dir: Path,
    runner: Callable = subprocess.run,
) -> tuple[bool, str]:
    """Post the review through review_post.py under a write token; (posted, what to tell the thread).

    The review bytes go to a harness-owned copy, so a review file the turn swapped afterwards is not
    what gets posted.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    fd, copy = tempfile.mkstemp(dir=scratch_dir, prefix=".review-post.", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(request.review)
        # The = form binds each value to its option, so no value can be read as another option.
        command = [python, str(wrapper), "--", python, str(script), "submit", pr,
                   f"--review={copy}", f"--verdict={request.verdict}",
                   *(f"--resolve={thread}" for thread in request.resolve)]
        try:
            completed = runner(command, env=dict(env), capture_output=True, text=True,
                               timeout=POST_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return False, "posting the review timed out"
        except OSError as exc:
            return False, f"posting the review could not start ({type(exc).__name__})"
    finally:
        try:
            os.unlink(copy)
        except OSError:
            pass
    output = (completed.stdout if completed.returncode == 0 else completed.stderr or completed.stdout) or ""
    detail = " ".join(output.split())[-600:]
    return completed.returncode == 0, detail


def _write_json_atomically(path: Path, data: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".deferred-reviews.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _first_at(entry: object) -> float:
    try:
        return float(entry["first_at"])
    except (TypeError, KeyError, ValueError):
        return 0.0


class DeferredReviewStore:
    """Review requests neither provider could run, one per PR and thread, replayed once they are due.

    An entry keeps the time of the first deferral, so a request that keeps failing is dropped after
    ``window`` rather than retried forever. An unreadable record raises rather than being reset.
    """

    def __init__(self, path: Path, *, window: int = DEFER_WINDOW_SECONDS, clock: Callable = time.time):
        self.path = path
        self.window = window
        self.clock = clock

    def _locked(self, update: Callable[[dict], tuple[object, bool]]) -> object:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._load()
            result, changed = update(data)
            if changed:
                _write_json_atomically(self.path, data)
            return result

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise DeferStoreError(f"deferred review record is unreadable: {type(exc).__name__}") from exc
        if not isinstance(value, dict):
            raise DeferStoreError("deferred review record is corrupt")
        return value

    def add(self, event: Mapping, *, pr: str, not_before: float) -> float:
        """Record the request, replacing an older one for that PR in that thread; returns the first-deferral time."""
        now = self.clock()
        first = event.get(DEFERRED_FIRST_AT_KEY)
        first = float(first) if isinstance(first, (int, float)) else now
        kept = {key: event[key] for key in _EVENT_FIELDS if isinstance(event.get(key), str)}
        kept[DEFERRED_FIRST_AT_KEY] = first
        entry = {"event": kept, "pr": pr, "first_at": first, "not_before": float(not_before)}
        key = f"{pr} {kept.get('thread_ts') or kept.get('ts', '')}"
        self._locked(lambda data: (data.__setitem__(key, entry), True))
        return first

    def take_due(self, limit: int = 1) -> tuple[list[dict], list[dict]]:
        """(due, expired): at most ``limit`` entries that may run now, oldest first, and every entry
        that waited past the window, each removed. One at a time by default, because a requester holds
        only one admission slot and a replay refused for that would be lost."""
        now = self.clock()

        def split(data: dict) -> tuple[tuple[list[dict], list[dict]], bool]:
            due, expired, changed = [], [], False
            for key in sorted(data, key=lambda k: _first_at(data[k])):
                entry = data[key]
                try:
                    first_at, not_before = float(entry["first_at"]), float(entry["not_before"])
                    valid = isinstance(entry.get("event"), dict)
                except (TypeError, KeyError, ValueError):
                    valid = False
                if not valid:
                    data.pop(key)
                    changed = True
                elif now - first_at > self.window:
                    expired.append(data.pop(key))
                    changed = True
                elif not_before <= now and len(due) < limit:
                    due.append(data.pop(key))
                    changed = True
            return (due, expired), changed

        return self._locked(split)
