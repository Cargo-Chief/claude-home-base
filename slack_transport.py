"""Slack transport defaults: no link previews on posts, and each event handled once."""

from __future__ import annotations

import threading
from collections import OrderedDict


class NoUnfurlMixin:
    """Mix in before WebClient: posts never expand link or media previews unless asked to."""

    def chat_postMessage(self, **kwargs):
        kwargs.setdefault("unfurl_links", False)
        kwargs.setdefault("unfurl_media", False)
        return super().chat_postMessage(**kwargs)


class RecentEvents:
    """Event ids already accepted; Slack redelivers an event when its first ack was slow."""

    def __init__(self, capacity: int = 2000) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def first_delivery(self, event_id: str) -> bool:
        """True the first time an id is offered. An event without an id is never a repeat."""
        if not event_id:
            return True
        with self._lock:
            if event_id in self._seen:
                self._seen.move_to_end(event_id)
                return False
            self._seen[event_id] = None
            if len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return True
