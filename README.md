# Claude Home Base

> This branch is Cargo Chief's hardened fork. It is not ready for Slack traffic yet.
> The first safety gate permits only explicitly configured Claude rooms using
> `--permission-mode auto` and refuses startup when user/approver allowlists are empty.

## Cargo Chief safety gate

Before every Claude spawn, the harness verifies that the configured workspace has clean
`agent-kit` and `docs` shared clones on `master`, a tracked non-empty autonomous overlay, a current
generated `AGENTS.md`, and a `CLAUDE.md` symlink to it. Room entries must explicitly provide the
root, permission mode, overlay, private escalation channel, backend, model, effort, and role.

Every Slack ingress path checks the current sender. Unauthorized DMs, channel messages, mentions,
button actions, forwarded replies, stops, and mid-turn steering are refused before reaching a live
agent process. The initial gate allows only the Claude backend; Codex remains disabled until it has
equivalent governance.

Secrets load from `~/.config/cargo-chief/home-base.env` by default, never from the checkout.
Logs, session maps, forwards, votes, stderr, and temporary artifacts live under the external
`CARGO_CHIEF_RUNTIME_DIR`. Gate A permits one live session, caps turns at 15 minutes, writes
metadata-only audit records, removes stale temporary files at startup, and refuses file transfer
or transcript search.

Each Slack thread runs from a stable private scratch directory under
`$CARGO_CHIEF_ROOT/work/home-base/<routing-hash>/`. The hash includes both the channel and thread,
so separate threads never share a current working directory. A private `state.json` preserves the
route, Claude session ID, and activity timestamps across daemon restarts. Stale workspaces are
removed after 14 days; live, recent, unrecognized, malformed, and symlinked entries are preserved
fail-closed.

Run the policy tests with:

```bash
python3 -m unittest discover -s tests -v
```

An always-on AI cofounder running on your Mac. DM it in Slack, it responds with full access to your codebase, tools, and context. $200/month flat. You own the whole stack.

## What this is

A complete setup for turning a spare Mac (Mini, MacBook Air, whatever) into a dedicated AI server:

- **Slack bot** that wraps Claude Code's CLI — DM it or @mention it in channels
- **Cloudflare Tunnel** for production-grade Slack integration (HTTP Events API, not Socket Mode)
- **Plugin marketplace** with skills for creative direction, coding, image generation, brainstorming, and more
- **Identity system** — your AI writes its own personality, keeps a diary, compounds over time
- **Setup guide** — step-by-step, with dark mode, interactive checklists, and concept explainers

## What you need

- A Mac you can leave running (Mac Mini, old MacBook Air, etc.)
- [Claude Code Max subscription](https://claude.ai) ($200/month)
- A Slack workspace
- A domain for Cloudflare Tunnel (any domain works)

## Quick start

1. **Follow the setup guide** at **[nityeshaga.github.io/claude-home-base](https://nityeshaga.github.io/claude-home-base/)** — it walks you through everything step by step
2. **Set up hardware** — plug in your Mac, configure it for always-on use
3. **Deploy the Slack bot** — Cloudflare Tunnel + Flask, production-standard
4. **Install the starter kit** — the final step in the guide has you paste one prompt into Claude Code. It clones this repo, installs plugins, asks you a few questions, and writes its own identity. You watch it come alive.

## What's in the box

```
bot.py                  # Slack bot (Flask + HTTP Events API)
bot_codex.py            # Optional Codex backend (drives `codex app-server` instead of Claude)
index.html              # Setup guide (GitHub Pages)
CLAUDE.md.example       # Template for your AI's operations manual
identity.md             # Your AI's soul (principles + self-authored identity)
about-you-and-how-you-came-to-life.md  # Origin story template
.env.example            # Configuration template
model-config.json.example  # Per-channel/DM model + effort config template
requirements.txt        # Python dependencies

plugins/
├── coding/             # Precision coding tools
│   └── skills/
│       └── make-precise-ui/  # Pixel-perfect UI from Figma designs
├── creative/           # Creative direction, writing, brainstorming
│   └── skills/
│       ├── creative-lead/    # Creative direction for any project
│       ├── explorable-explanation-creator/  # Topic → tree of no-scroll interactive HTML pages
│       ├── lets-brainstorm/  # Timed coaching sessions
│       ├── help-me-write/    # Collaborative writing (keeps your voice)
│       └── interview-me/     # Timed discovery interviews
├── more-ai/            # Gemini and OpenAI image generation, thinking
│   └── skills/
│       ├── gemini-imagegen/
│       ├── openai-imagegen/
│       └── gemini-thinking/
└── experimental/       # Operational workflows, debiasing, prompt engineering
    └── skills/
        ├── briefing/         # Email, briefs, action tracking
        ├── are-you-sure/     # Blind debiasing for claims and opinions
        ├── prompt-engineer/  # AI prompt writing and review
        └── investigate-yourself/  # Forensic self-diagnosis
```

## Architecture

```
You (anywhere) → Slack → Cloudflare Tunnel → Your Mac → Claude Code CLI
                                                          ↓
                                              CLAUDE.md + identity.md
                                              + plugins + skills
                                              + full filesystem access
```

## Codex backend (upstream capability; disabled for Cargo Chief Gate A)

Upstream's default brain is Claude Code. Upstream can also point individual rooms at
OpenAI's Codex — same Slack UX, same full-access posture, a different engine
answering. It's per-room, so a Claude channel and a Codex channel can coexist
in one workspace (handy for side-by-side comparison).

**How it works:** `bot_codex.py` drives `codex app-server` over JSON-RPC on
stdio, mirroring the one-process-per-thread model of the Claude path. Threads
resume across messages, output streams to Slack, and mid-turn follow-ups steer
the running turn — exactly like the Claude backend. The Cargo Chief Gate A bot
does not import this module or create any Codex runtime state.

The Cargo Chief safety policy currently rejects `backend: "codex"` before process spawn. The
instructions below describe upstream behavior for reference; do not enable it in a Cargo Chief room
until equivalent governance and sandboxing are implemented and tested.

**Upstream setup:**

1. Install the [`codex` CLI](https://github.com/openai/codex) and sign in.
2. In `model-config.json`, add `"backend": "codex"` to any channel or DM entry,
   with a Codex `"model"`:
   ```json
   "channels": {
     "C0YOURCODEXROOM": { "name": "#codex", "backend": "codex", "model": "gpt-5.6-sol" }
   }
   ```
3. (Optional) Set `CODEX_HOME` in `.env` to isolate the bot's Codex threads/auth
   from your own interactive `codex`.

Picking a `gpt-*` / `o*` model for a room on the `/models` page is enough on its
own — the backend is inferred from the model, so there is no second control to
keep in sync. An explicit `"backend"` key still wins when you need it.

**Notes:** Codex runs with approvals off and no sandbox (`danger-full-access`) —
the equivalent of Claude's `--dangerously-skip-permissions` — because a
Slack-driven turn has no human to answer an approval prompt. Reasoning effort
comes from Codex's own `config.toml` (`model_reasoning_effort`), not
model-config's `effort`.

## Bot features

- **HTTP Events API** via Flask — production-standard, stateless
- **Async processing** — responds to Slack within 3 seconds, runs Claude in background
- **Agentic channel behavior** — decides when to respond, stays silent when not relevant (SKIP)
- **Thread continuity** — session IDs persist per thread
- **File handling** — upstream capability; disabled by the Cargo Chief Gate A runtime policy
- **Proactive messaging** — send DMs, post to channels, reply in threads via CLI
- **Streaming output** — real-time responses as Claude generates
- **Native tables** — markdown tables in responses render as real Slack tables (Block Kit `markdown` block)
- **Per-room models** — `model-config.json` picks which model and reasoning effort answers in each channel or DM, plus an optional per-model system prompt; read fresh on every spawn (no restart), editable from the file explorer's `/models` page. Name a `default_model` there and the page's default row becomes a dropdown too, so you can move every unconfigured room to a different model in one pick
- **Prompt cadence** — a per-model prompt is in the system prompt at spawn; give it a cadence and it is also re-sent with every Nth message, so a standing instruction ("keep Slack replies short") doesn't decay over a long thread (Claude backend; Codex rooms take their instructions from Codex's own config)
- **Pluggable backend** — point any room at OpenAI's Codex instead of Claude by picking a `gpt-*` model for it on the `/models` page; same Slack UX, different engine (see [Codex backend](#codex-backend-optional))
- **Interactive buttons** — button clicks and menu picks route back into the thread's Claude session as messages, so your AI can offer approve/hold/snooze choices and act on the answer (requires Interactivity enabled in your Slack app config; Request URL = the same `/slack/events` endpoint)
- **In-thread stop** — type a bare `stop` in a thread where the bot is mid-run to interrupt it (like Esc in the terminal); the session survives with full context, so your next message steers it in the new direction
- **Mid-turn steering** — message a thread while the bot is mid-run and it sees your message at the next tool-call boundary, inside the same turn (like typing without Esc in the terminal); no more waiting for the whole task to finish before you can course-correct

## License

MIT
