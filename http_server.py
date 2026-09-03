"""Production HTTP serving for the Slack Events API."""

from __future__ import annotations

from typing import Callable


def serve_http(
    app,
    port: int,
    *,
    create_server: Callable | None = None,
    on_ready: Callable[[], None] | None = None,
) -> None:
    """Bind the Slack receiver on loopback, announce readiness, then serve."""
    if create_server is None:
        from waitress.server import create_server as waitress_create_server

        create_server = waitress_create_server

    server = create_server(app, host="127.0.0.1", port=port, threads=4)
    if on_ready is not None:
        on_ready()
    server.run()
