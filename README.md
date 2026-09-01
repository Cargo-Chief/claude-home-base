# Claude Home Base

> This branch is Cargo Chief's hardened fork. It is not ready for Slack traffic yet.
> The first safety gate permits only explicitly configured Claude rooms using
> `--permission-mode auto` and refuses startup when user/approver allowlists are empty.

## Cargo Chief safety gate

Before every Claude spawn, the harness verifies that the configured workspace has clean
`agent-kit` and `docs` shared clones on `master`, a tracked non-empty autonomous overlay, a current
generated `AGENTS.md`, and a `CLAUDE.md` symlink to it. Room entries must explicitly provide the
root, permission mode, overlay, private escalation channel, primary model/effort, explicit OpenAI
fallback, and role.

Every Slack ingress path checks the current sender. Unauthorized DMs, channel messages, mentions,
button actions, forwarded replies, stops, and mid-turn steering are refused before reaching a live
agent process. Claude remains primary; a room can move to its OpenAI fallback only after the
harness recognizes Claude's account credit-limit response.

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

Delegation is pinned by the harness rather than inherited from a machine default. Raw Claude Agent
calls and Codex multi-agent are disabled; both providers use the same one-shot governed launcher.
Implementation requires a validated `implementation-ready` plan in a real docs worktree. Bounded,
mechanical, and explore tiers route to the approved provider-equivalent model and effort. Every
thread has a persistent 250,000 delegated-token ceiling; only a named approver can inspect, reset,
or change it, and bare `stop` terminates an active delegate. Audit records contain routing,
plan-gate, aggregate usage, duration, and outcome metadata only. Delegated prompts, tool inputs,
file paths, and response content are never retained or logged.

Every request declares `mutation` explicitly. A mutating request at any capable tier requires the
implementation plan claim. A non-mutating request runs with read-only provider tools; Explore can
never be marked mutating.

When a thread creates a standard `worktrees/CN-####-slug/` bundle, it can claim that bundle through
its private one-shot claim file. Home-base accepts only a direct, non-symlinked child of
`$CARGO_CHIEF_ROOT/worktrees/` with a regular `TASK.md`, refuses a bundle already owned by another
thread, and caps live mappings at `MAX_LIVE_BUNDLES` (default 3, maximum 5). The mapping lives in
the thread's private state and survives daemon/session restart; a resumed process starts in the
validated bundle. A removed or invalid bundle safely falls back to the thread scratch directory.

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
openai_fallback.py      # Governed Codex CLI fallback for Claude credit exhaustion
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

## OpenAI fallback

Every Cargo Chief room keeps Claude as its primary owning process and explicitly names a Codex
fallback model and effort. The harness switches only when the Claude CLI emits its recognized
account credit-limit response. Arbitrary model errors, tool failures and timeouts do not trigger a
provider change. A named approver can also pin one Slack thread explicitly with `provider openai`
or `provider claude`; `provider status` reports the pin and `provider auto` removes it. The command
affects subsequent turns and survives daemon restarts. `provider openai new` keeps the OpenAI pin
but discards that thread's saved Codex resume id, for explicit recovery from a rejected session.

Fallback turns run as `codex --profile cargo-chief exec --json`: the generated Cargo Chief profile
keeps the workspace sandbox, network policy and automatic approval reviewer active. The same
authenticated Slack authority envelope, autonomous overlay, working directory or bound bundle,
private escalation route, parking claim and audit metadata are carried into the fallback turn.
The OpenAI session id is persisted per Slack thread, so a thread that crosses providers remains on
OpenAI rather than oscillating when Claude credits return.

The approved routing equivalents are Sol/high for the owning thread, Sol/medium for bounded
engineering, Terra/high for mechanical or high-volume work, and Luna/medium for read-only search.
Fallback turns are serialized per Slack thread; the Claude path retains its live mid-turn steering.
Governed delegation metadata is written to
`$CARGO_CHIEF_ROOT/work/home-base/delegation-audit.log`. Keeping this audit inside the workspace
lets both Claude and OpenAI owning sessions append it without a filesystem-permission retry; the
file is mode `0600` and never contains prompts or delegate returns.

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
- **Prompt cadence** — a per-model prompt is in the Claude system prompt at spawn and can be re-sent every Nth message so a standing instruction does not decay
- **Credit-limit fallback** — a recognized Claude account limit moves the Slack thread to its explicit, profile-governed OpenAI model while preserving the authority envelope and durable session id
- **Governed delegation** — one provider-neutral launcher enforces implementation-plan readiness, exact model/effort routing, a persistent 250k thread budget, metadata-only audit, and the shared stop path. Usage is enforced at provider call boundaries: once a Claude stream event or Codex app-server notification reaches the remaining allowance, the launcher interrupts before another model call, charges the full observed usage, and withholds the return. One already-running provider call can cross the exact token boundary because usage arrives only after that call completes
- **Interactive buttons** — button clicks and menu picks route back into the thread's Claude session as messages, so your AI can offer approve/hold/snooze choices and act on the answer (requires Interactivity enabled in your Slack app config; Request URL = the same `/slack/events` endpoint)
- **In-thread stop** — type a bare `stop` in a thread where the bot is mid-run to interrupt it (like Esc in the terminal); the session survives with full context, so your next message steers it in the new direction
- **Mid-turn steering** — message a thread while the bot is mid-run and it sees your message at the next tool-call boundary, inside the same turn (like typing without Esc in the terminal); no more waiting for the whole task to finish before you can course-correct

## License

MIT
