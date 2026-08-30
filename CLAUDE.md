# Claude Home Base — Cargo Chief fork

An open-source project that lets anyone turn a spare Mac into an always-on AI cofounder. DM it in Slack, it responds with full access to your codebase, tools, and context. $200/month flat.

## What this is

The generalized, open-source version of CC Home Base (Luo Ji's installation). This is what Nityesh's friends and the public use. It includes:
- `bot.py` — generic Slack bot (Flask + HTTP Events API)
- `bot_codex.py` — optional alternate backend: drives OpenAI's `codex app-server` for rooms with `"backend": "codex"` in model-config.json (imported lazily by bot.py)
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

Keep this CLAUDE.md up-to-date with the latest changes / decisions made in the project.
