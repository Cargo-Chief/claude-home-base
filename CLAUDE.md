# Claude Home Base — Cargo Chief fork

An open-source project that lets anyone turn a spare Mac into an always-on AI cofounder. DM it in Slack, it responds with full access to your codebase, tools, and context. $200/month flat.

## What this is

The generalized, open-source version of CC Home Base (Luo Ji's installation). This is what Nityesh's friends and the public use. It includes:
- `bot.py` — generic Slack bot (Flask + HTTP Events API)
- `openai_fallback.py` — governed one-turn Codex runner used only after a recognized Claude credit-limit event
- `CLAUDE.md.example` — template operations manual
- `identity.md` / `about-you-and-how-you-came-to-life.md` — identity templates
- `plugins/` — marketplace of skills (coding, creative, more-ai, chief-of-staff)
- `search/` — hybrid keyword + vector search over local files and conversation history
- `index.html` — setup guide hosted at nityeshaga.github.io/claude-home-base

## Who cares about it

- **Nityesh** — maintainer, open-sourced this project
- **External users** — Nityesh's friends and anyone who finds the repo

## Relationship to upstream

This is Cargo Chief's fork of `nityeshaga/claude-home-base`. Upstream remains the transport
template; this fork adds Cargo Chief's fail-closed authorization, room policy, and governance
preflight. Keep broadly useful transport improvements separable so they can be proposed upstream.

## How to work on it

- Repo is on GitHub: `Cargo-Chief/claude-home-base`
- Shared clone stays clean on `main`; make changes in a worktree under
  `$CARGO_CHIEF_ROOT/worktrees/<slug>/claude-home-base`
- Setup guide: `index.html` (GitHub Pages)
- Plugin versioning: bump version in both `plugin.json` and `marketplace.json`

## Gotchas

- This repo uses `bot.py`, not `luoji_bot.py` (that's the cc-home-base variant)
- The `CLAUDE.md.example` here is a template with placeholders — don't fill them in, they're for end users
- Plugin changes need version bumps or the auto-updater won't pick them up
- A `channels` entry with `"review_only": true` is mention-only and runs review turns for any member
  or listed peer bot (`review_requests.py`); it needs a top-level `peer_review` block with a valid
  `self_github_login`. `peer_bot_user_ids` (default empty) and `max_reviews_per_pr` (default 3,
  range 1–10) are optional. See README's safety gate
- A review turn's process is closed when the turn ends, so the reader ends a review only at a
  `result` after its `REVIEW_DONE_MARKER` line with no background subagent running
  (`session_lifecycle.BackgroundAgents`); every other `result` is a pause. See README
- A review turn writes to GitHub only through `review_post.py` (verdict, inline comments, resolving
  its own threads); approvals need the requester in `peer_review.github_logins`. The review token
  (`REVIEW_TOKEN_PERMISSIONS`) may be refused a resolve, so an approval can post over a thread the
  agent answered and could not resolve, asking the author to resolve it. See README
- Review passes are counted from the agent's own marked reviews on the PR (`plan_review_pass`), not
  from local state; the 24-hour cap store is only a rate limit. After the last pass a request runs a
  close-out, which can still approve. See README
- A peer's review ending wakes the thread that asked (`ReviewNoticeStore`, `_start_review_notice`),
  but only a live session in that channel: a notice never spawns a process or runs on the Codex
  fallback, because either would run as the peer bot. The turn has `review_notice` authority, is
  kept out of `turn_senders`, and reuses `from_handoff` to refuse a handoff, so `handoff_refusal`
  edits reach it too. Its bypasses in `process_message_async`, `is_review_ended`, and the prompt
  line naming `review_notice_pr` change together. See README
- A turn's handoff claim (`thread_handoff.py`) is taken by `_take_handoff` inside the turn lock
  and acted on by `_start_handoff` after the turn completes. The handoff turn is never an approver
  and cannot hand off again. The coupled pieces are:
  - the refusal rules (`handoff_refusal`);
  - the `_handoff_stops` mark set by `stop`;
  - `turn_senders`;
  - the Codex runner's discard on a stopped, failed or withheld turn;
  - the `handoff_from` bypasses in `process_message_async`;
  - the prompt text in `cargo_chief_safety.handoff_prompt`, which names the `handoff_from_thread`
    envelope field.

  Change them together.
