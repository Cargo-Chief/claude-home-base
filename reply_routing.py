"""Durable, one-shot routing for replies to proactive Slack threads."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time


@dataclass(frozen=True)
class ReplyRouteResult:
    status: str
    route: dict | None = None


class ReplyRouteStore:
    """Private JSON store with atomic, terminal reply claims."""

    def __init__(self, path: Path, *, max_age_seconds: int):
        self.path = path
        self.max_age_seconds = max_age_seconds
        self._lock = threading.Lock()

    @staticmethod
    def _key(channel: str, thread: str) -> str:
        return f"{channel}:{thread}"

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError("reply route store is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("reply route store is invalid")
        return value

    def _write(self, routes: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.write_text(json.dumps(routes), encoding="utf-8")
        self.path.chmod(0o600)

    def register(
        self,
        *,
        from_channel: str,
        from_thread: str,
        to_channel: str,
        to_thread: str,
        user_id: str,
        session_id: str | None,
        approver_only: bool = False,
        now: float | None = None,
    ) -> None:
        if not all((from_channel, from_thread, to_channel, to_thread, user_id)):
            raise ValueError("reply route fields are required")
        timestamp = time.time() if now is None else now
        with self._lock:
            routes = self._load()
            routes[self._key(from_channel, from_thread)] = {
                "from_channel": from_channel,
                "from_thread": from_thread,
                "thread": to_thread,
                "channel": to_channel,
                "session_id": session_id,
                "user_id": user_id,
                "approver_only": bool(approver_only),
                "state": "active",
                "registered_at": timestamp,
            }
            self._write(routes)

    def claim(
        self,
        *,
        from_channel: str,
        from_thread: str,
        user_id: str,
        is_approver: bool,
        now: float | None = None,
    ) -> ReplyRouteResult:
        """Atomically claim a route, retaining terminal state for duplicate suppression."""
        timestamp = time.time() if now is None else now
        with self._lock:
            try:
                routes = self._load()
            except ValueError:
                return ReplyRouteResult("invalid")
            key = self._key(from_channel, from_thread)
            route = routes.get(key)
            legacy_key = None
            if not isinstance(route, dict):
                legacy = routes.get(from_thread)
                if isinstance(legacy, dict) and "from_channel" not in legacy:
                    # Pre-correlation forwards were keyed by thread timestamp only.
                    # Adopt the inbound channel on first use so pending ordinary DM
                    # forwards survive deployment of the hardened schema.
                    legacy_key = from_thread
                    route = legacy
                    route["from_channel"] = from_channel
                    route["from_thread"] = from_thread
                    route.setdefault("approver_only", False)
                    route.setdefault("state", "active")
                    routes[key] = route
                    routes.pop(legacy_key, None)
                    self._write(routes)
            if not isinstance(route, dict):
                return ReplyRouteResult("missing")
            if route.get("from_channel") != from_channel or route.get("from_thread") != from_thread:
                return ReplyRouteResult("invalid")

            state = route.get("state", "active")
            if state == "consumed":
                return ReplyRouteResult("consumed")
            if state == "expired":
                return ReplyRouteResult("expired")
            if state != "active":
                return ReplyRouteResult("invalid")
            registered_at = route.get("registered_at")
            if not isinstance(registered_at, (int, float)):
                return ReplyRouteResult("invalid")
            if timestamp - registered_at >= self.max_age_seconds:
                route["state"] = "expired"
                route["expired_at"] = timestamp
                self._write(routes)
                return ReplyRouteResult("expired")
            if route.get("approver_only") and not is_approver:
                return ReplyRouteResult("approver_required")

            route["state"] = "consumed"
            route["consumed_at"] = timestamp
            route["consumed_by"] = user_id
            self._write(routes)
            return ReplyRouteResult("routed", dict(route))

    def gc(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else now
        cutoff = timestamp - self.max_age_seconds
        with self._lock:
            routes = self._load()
            stale = []
            changed = False
            for key, route in routes.items():
                if not isinstance(route, dict):
                    stale.append(key)
                    continue
                if route.get("state", "active") == "active":
                    registered_at = route.get("registered_at")
                    if not isinstance(registered_at, (int, float)):
                        stale.append(key)
                    elif registered_at < cutoff:
                        route["state"] = "expired"
                        route["expired_at"] = timestamp
                        changed = True
                    continue
                last = route.get("consumed_at", route.get("expired_at", route.get("registered_at", 0)))
                if not isinstance(last, (int, float)) or last < cutoff:
                    stale.append(key)
            for key in stale:
                routes.pop(key, None)
            if stale or changed:
                self._write(routes)
            return len(stale)
