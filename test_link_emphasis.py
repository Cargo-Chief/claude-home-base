#!/usr/bin/env python3
"""Regression tests for strip_link_emphasis (bot.py).

Emphasis markers touching a URL break Slack's auto-linkification, so a shared
link arrives non-clickable. Natalia hit this twice; the second time the share
link WAS the deliverable ("stop delivering links with * * at the start and end,
that breaks the link" — 2026-08-11).

Both directions matter. A gate that only ever passes is indistinguishable from a
gate that cannot fail, so the MUST_NOT_CHANGE half is as load-bearing as the
MUST_STRIP half: over-stripping would silently kill ordinary bold text.

Run: python3 test_link_emphasis.py
"""
import re
import sys
from pathlib import Path

# Import the two functions without booting the Slack app (bot.py connects at import).
_SRC = (Path(__file__).parent / "bot.py").read_text()
_NS = {"re": re}
exec(_SRC[_SRC.index("# Emphasis markers touching a URL"):_SRC.index("def chunk_message")], _NS)
strip_link_emphasis = _NS["strip_link_emphasis"]
md_to_slack = _NS["md_to_slack"]

MUST_STRIP = [
    ("**https://form.typeform.com/to/abc123**", "https://form.typeform.com/to/abc123"),
    ("Here: *https://every-docs.pages.dev*", "Here: https://every-docs.pages.dev"),
    ("__https://x.com/claudie_every__", "https://x.com/claudie_every"),
    ("_http://100.123.10.100:8889/browse/a_", "http://100.123.10.100:8889/browse/a"),
    ("~~https://dead.link~~", "https://dead.link"),
    ("**<https://a.co|Dashboard>**", "<https://a.co|Dashboard>"),
    ("**[Dashboard](https://a.co/x)**", "[Dashboard](https://a.co/x)"),
    ("**_https://nested.example.com_**", "https://nested.example.com"),
    ("Link: **https://a.co** and **https://b.co**", "Link: https://a.co and https://b.co"),
    ("- **https://a.co/1**\n- **https://b.co/2**", "- https://a.co/1\n- https://b.co/2"),
    ("_https://a.co/x_y_", "https://a.co/x_y"),
    (
        "**https://docs.google.com/spreadsheets/d/1AbC_dE-f/edit#gid=0**",
        "https://docs.google.com/spreadsheets/d/1AbC_dE-f/edit#gid=0",
    ),
    ("Form: **https://form.typeform.com/to/wEyDuCuP** — send it",
     "Form: https://form.typeform.com/to/wEyDuCuP — send it"),
]

MUST_NOT_CHANGE = [
    "**Deliverable ready** — see the sheet",
    "The **link** is below:\nhttps://a.co",
    "**Bold with https://a.co inside a sentence**",
    "*Completed 3 of 4 tasks* :white_check_mark:",
    "**Natalia** owns this",
    "a_url_like_this_is_not_a_link",
    "https://a.co/path_with_underscores_ok",
    "**status: 200**",
    "*Done, off your plate:*",
    "See https://a.co and https://b.co plainly",
    "**Two links: https://a.co https://b.co**",
]


def main() -> int:
    failures = 0
    for src, want in MUST_STRIP:
        got = strip_link_emphasis(src)
        if got != want:
            failures += 1
            print(f"FAIL strip {src!r}\n  want {want!r}\n  got  {got!r}")
    for src in MUST_NOT_CHANGE:
        got = strip_link_emphasis(src)
        if got != src:
            failures += 1
            print(f"FAIL over-strip {src!r}\n  got {got!r}")

    # End to end: the link survives clickable, ordinary bold still converts.
    e2e = md_to_slack(
        "Form is live: **https://form.typeform.com/to/wEyDuCuP**\n**Next step:** Natalia sends it."
    )
    if "*https" in e2e or "*Next step:*" not in e2e:
        failures += 1
        print(f"FAIL end-to-end md_to_slack: {e2e!r}")

    total = len(MUST_STRIP) + len(MUST_NOT_CHANGE) + 1
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
