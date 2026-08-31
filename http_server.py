"""Production HTTP serving for the Slack Events API."""

from __future__ import annotations

from typing import Callable


def serve_http(app, port: int, *, serve: Callable | None = None) -> None:
    """Serve the Slack receiver on loopback for the local Cloudflare tunnel."""
    if serve is None:
        from waitress import serve as waitress_serve

        serve = waitress_serve

    serve(app, host="127.0.0.1", port=port, threads=4)
