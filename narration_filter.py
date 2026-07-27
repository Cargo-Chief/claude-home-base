"""Procedural-preamble filter for on_text streaming.

Root cause of the recurring "Produce output first" battery ding (flagged
2026-04-25, 05-12, 05-13, 07-19, 07-22, 07-26): bot.py streams *every* text
block to Slack the instant Claude produces it. During a multi-step build each
interstitial "Let me decode the board…", "Let me set up the build script",
"Let me push it to Google Slides" becomes its own Slack message, so a fast,
correct deliverable still lands as a 4-message progress log followed by the
goods. Adding CLAUDE.md text has not fixed it across 6 flags — the reflex fires
mid-turn, when rules loaded at 100% can't gate a habit. This is the structural
gate (per identity.md: "a principle written there is prose, not a gate").

Wiring (do this once the homebase/Codex port in bot.py is committed):
    from narration_filter import is_procedural_preamble
    # inside on_text(), after the SKIP / soft-skip checks and BEFORE post_response:
    if first_text_sent and is_procedural_preamble(text_block):
        logger.info(f"Preamble suppressed (not streamed): {text_block.strip()!r}")
        return
Note the `first_text_sent` guard: only interstitial preambles are suppressed,
never a first/sole block, so a terse standalone reply is never swallowed.

DESIGN NOTES
- Mirrors the existing SOFT_SKIP_PHRASES filter's shape (short + normalized).
- Length cap (<=120 chars) avoids swallowing a real block that happens to open
  with "Let me". A genuine deliverable is essentially never <=120 chars AND
  pure "Let me <verb>" preamble.
- Must NOT swallow the sanctioned status-visibility pattern (up-front
  duration-setting on long opaque runs, per feedback_status_visibility): those
  read "This will take a while…", not "Let me…", so the tight regex threads it.
- Must NOT swallow common sign-offs that start "Let me know …" — explicitly
  excluded and covered by tests.
"""

import re

# Interstitial procedural preamble: present/future statement about what the
# model is *about to do*, not a deliverable. Anchored at start, case-insensitive.
_PREAMBLE_RE = re.compile(
    r"^\s*(?:"
    r"let me(?!\s+know)"                      # "Let me decode…" but NOT "Let me know…"
    r"|now let me"
    r"|alright,?\s+let me"
    r"|ok(?:ay)?,?\s+let me"
    r"|next,?\s+(?:let me|i'?ll)"
    r"|first,?\s+i'?ll"
    r"|i'?ll\s+(?:now|start|go|then|first|next)\b"
    r"|i'?m\s+going\s+to\s+(?:now\s+)?(?:set up|start|build|write|push|run|create|check|pull|grab|decode)"
    r"|let'?s\s+(?:set up|start|build|write|push|run|create|check|pull|grab|decode|do that|get)"
    r")\b",
    re.IGNORECASE,
)

_MAX_PREAMBLE_LEN = 120


def is_procedural_preamble(text_block: str) -> bool:
    """True if `text_block` is a short interstitial 'about-to-do-X' line that
    should be suppressed from Slack rather than streamed as its own message.

    Only the regex + length gate live here; the caller is responsible for the
    position gate (suppress only when it is NOT the first/sole block).
    """
    stripped = text_block.strip()
    if not stripped or len(stripped) > _MAX_PREAMBLE_LEN:
        return False
    # A block with multiple sentences that then delivers content is not pure
    # preamble — only suppress single-clause "Let me X" lines.
    if stripped.count("\n") > 0:
        return False
    return bool(_PREAMBLE_RE.match(stripped))


# --- self-test: run `python3 narration_filter.py` -------------------------
if __name__ == "__main__":
    SUPPRESS = [  # real interstitial narration pulled from battery verdicts
        "Let me decode the board...",
        "Let me set up and write the build script",
        "Let me push it to Google Slides",
        "Now let me render the slide to check it.",
        "Alright, let me pull the transcript first.",
        "Okay, let me set up the deck.",
        "First, I'll grab the source data.",
        "Next, I'll build the shapes.",
        "I'll now start the research job.",
        "I'll go build that as one editable slide.",
        "I'm going to set up the build script.",
        "Let's push it to Google Slides.",
    ]
    KEEP = [  # deliverables / status / sign-offs that must NOT be swallowed
        "Let me know if you want changes.",          # sign-off, not preamble
        "Let me know which option you prefer.",
        "Done — deck's live: https://docs.google.com/x",
        "This will take a while — expect the two links, not a progress log.",
        "Here's the summary you asked for.",
        "Let's go with the second option — it's cleaner and cheaper.",  # a decision, >120? no—must keep via len? it's <120
        "The doubled logo is my pipeline, not a Slides bug.",
        "Yes — but one correction: it was only Tribe AI, no Casper analysis exists.",
        "SKIP",
        "",
    ]
    ok = True
    for t in SUPPRESS:
        if not is_procedural_preamble(t):
            print(f"FAIL (should suppress): {t!r}")
            ok = False
    for t in KEEP:
        if is_procedural_preamble(t):
            print(f"FAIL (should keep):     {t!r}")
            ok = False
    print("ALL PASS" if ok else "TESTS FAILED")
    raise SystemExit(0 if ok else 1)
