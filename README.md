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

A room entry may add `"review_only": true` (for example `#pr-reviews`, with
`autonomous_overlay` pointing at the kit's `modes/review-only.md`). Plain posts there are ignored;
a mention from any human member, or from a bot listed in `peer_review.peer_bot_user_ids`, carrying
exactly one `https://github.com/Cargo-Chief/<repo>/pull/<n>` link starts a fresh, harness-built
`cc-code-review high` turn. The sender is marked `review_requester` with no approver
authority, and the turn gets a read/PR-write GitHub token request, no AWS variables or Slack
secrets, and extra deny rules. It never resumes, steers, escalates or registers a reply route, and
`bot.py --escalate`, `--forward-to` and `/internal/forward` refuse review-only rooms server-side.
Before the turn the harness refuses the agent's own PRs (`peer_review.self_github_login`), PRs that
are not open, PRs it cannot look up, a GitHub wrapper too old to downscope its token, an unreadable
cap record, and PRs past twice `peer_review.max_reviews_per_pr` (default 3, range 1–10) started
turns in 24 hours. At most two requests are admitted at once and each requester may make ten an hour; a
redelivered mention is handled once. Each request writes one `REVIEW_REQUEST` audit line. A
review-only room without a valid `peer_review.self_github_login` is refused.

Each request for the same PR is the next pass. `review_post.py` stamps every review body with a
hidden marker (`<!-- cc-peer-review pass=N mode=… head=… -->`), and the harness reads the agent's own
Bot-authored marked reviews when it looks up the PR (`plan_review_pass`); the latest marker sets
the pass and the base, and `review_post.py` strips marker-shaped text from the model's body before
stamping. With no marker the turn is pass 1, a full review. Passes 2 to `max_reviews_per_pr` review
only the commits since the last marked head (`cc-code-review --since`, which falls back to a full
review after a rebase). Every pass goes through the agent's open threads: a reply naming the fix or
accepting or rejecting the author's reason, then a resolve for each fixed or accepted one; a thread
whose reply failed is not resolved. A review token may be refused a resolve: a thread named in
`--resolve` whose latest comment is still the agent's own reply (read again after posting) then
counts as settled, and the approval's body asks the author to resolve it. A refusal on a thread the
agent has not answered, or any other resolve error, still blocks the approval; every failure carries
GitHub's error text. `threads` lists such a thread as `answered`. After the last pass a request runs a close-out instead of being
refused, numbered as the last pass: no `cc-code-review` run and no new findings, only the thread
work over the commits since the last pass. It approves only when no thread is left open and the
last reviewed head is still in the branch history; otherwise it comments naming what is in
dispute. One review runs per PR at a time. The 24-hour cap is a rate limit at twice the pass cap.

The turn posts only through `review_post.py`, run under the GitHub App wrapper with the absolute
paths the task names. The deny rules block the raw reviews and GraphQL endpoints because they cannot
see a payload file, so the script reads the payload and enforces the rules in code. It posts only to
the PR the harness admitted (`CARGO_CHIEF_REVIEW_PR`). `submit` posts one review with validated
inline comments and an explicit verdict (`comment`, `request_changes` or `approve`) against the
current head, then resolves the threads named with `--resolve`, which must be threads this agent's
Bot account started (`threads` lists them). An approval needs an open, non-draft PR whose head has
not moved, no inline findings, none of the agent's own threads left open, a requester listed in
`peer_review.github_logins` (Slack user ID to login, passed in by the harness), and a PR the agent did
not write. A requester may get an approval on their own PR, and a bot approval counts toward branch
protection's required review; both are by design. Which threads a new head fixes, and the verdict
itself, are the model's judgement. A review turn's file tools are denied inside the checkouts
(`claude-home-base`, `docs`, `agent-kit`, `repos`, `worktrees`). Bash deny rules match command
text, so a shell write into those checkouts, or code the agent writes under `work/` and runs, is
not blocked: the script's checks are guardrails rather than a sandbox, and a turn that ignores its
task can still use its PR-write token directly.

The review task asks the model to end its final reply with a line reading `REVIEW COMPLETE`
(`REVIEW_DONE_MARKER`; case, trailing punctuation and markdown emphasis are ignored). The message
carrying that line is posted as the reply as soon as it arrives, without the line. A review turn
ends at the first `result` after it while no background subagent it started (the review's
finders) is still running. Any other `result` pauses the turn: its text folds into the status
line, and the turn continues when a finder's notification starts the next one. A review that
already posted its completion line and then times out ends without a timeout note. Background shell tasks and `Monitor`
timers are not counted; they die with the process. While a finder is tracked (at most 30 minutes
each) the inactivity limit does not fire, so `REVIEW_MAX_RUNTIME` (45 minutes) is the bound that
always holds; otherwise the usual inactivity limit applies. A review holds its turn-admission
slot until it ends. A timeout or an authorized user's `stop` kills the process; a review is never
resumed. A review turn never consumes a bundle or parking claim.

The sending side is `bot.py --request-review <PR URL>`, which normal turns are told about. It
takes only the URL, checks that the PR is open and authored by this agent's own App, and posts
`<@peer> please review <URL>` in the single review-only room to the single configured peer bot.
A review turn cannot run it, and the sender stops at the same per-PR budget the peer enforces (twice `max_reviews_per_pr` per 24 hours). Each call, including a failed Slack post, writes a `REVIEW_REQUEST_SENT` audit line.

One Slack thread holds one ticket or topic. When a turn is asked for different work, the agent
writes `{"title": ..., "brief": ...}` to its thread's `handoff-claim.json` and ends the turn. The
harness takes the claim before the turn releases its lock, so the next turn in the thread can
neither discard nor supply it. Once the turn has completed, the harness:
1. opens a new top-level thread in the same room, with the title and brief as Slack-escaped text;
2. starts a fresh session there with the brief as its first turn;
3. links the new thread from the source thread.

The new turn runs as the person whose message started the source turn, re-checked against the
allowlist. Its envelope carries `handoff_from_thread` and never `named_approver`, so it can do
routine work, but any approval gate needs a person's message in the new thread. A person's reply
that reaches the new thread first takes over, and the brief is not steered into that turn.

The harness refuses a claim from:
- a review turn or a handoff turn;
- a stopped, timed-out or failed turn;
- a Codex turn that escalated or whose delegate result was withheld;
- a forwarded reply;
- a turn more than one person wrote into;
- a thread whose delegation budget is exhausted or awaiting verification.

The claim is read through one no-follow descriptor, capped at 16 KiB. Audit lines:
`THREAD_HANDOFF` (opened), `HANDOFF_REFUSED`, and `HANDOFF_FAILED` (Slack refused the new thread).

Every Slack ingress path checks the current sender. Unauthorized DMs, channel messages, mentions,
button actions, forwarded replies, stops, and mid-turn steering are refused before reaching a live
agent process. The one exception is a mention in a review-only room, which any member or listed
peer bot may send and which can only start the review turn above. Claude remains primary; a room can move to its OpenAI fallback only after the
harness recognizes Claude's account credit-limit response.

Secrets load from `~/.config/cargo-chief/home-base.env` by default, never from the checkout.
The governed launcher still supports Claude delegates, although no thread routes one there now
that Claude owners delegate natively; for them it reads `CLAUDE_CODE_OAUTH_TOKEN` from that same file (or
`CARGO_CHIEF_ENV_FILE`) and sets it only on the Claude delegate's environment, because the
owner session's shell does not reliably carry it.
Logs, session maps, forwards, votes, stderr, and temporary artifacts live under the external
`CARGO_CHIEF_RUNTIME_DIR`. Gate A permits two live sessions and stops a turn after at most 15
minutes without owner output; a native subagent's streamed events count as output, and so does an
owner tool call that is still running (for up to 30 minutes per call), so a quiet test run is not
cut off. `TURN_TIMEOUT` logs `LIMIT:inactivity` or `LIMIT:hard`. On the OpenAI
fallback a governed delegate retains its own bounded call timeout and generation-token allocation;
its held launcher lock keeps the owner alive long enough for the launcher to meter the return, and
the owner receives a fresh inactivity window when the delegate exits. A hard limit, `MAX_TURN_RUNTIME` (default three hours, 600–21600 seconds), still
prevents a continuously noisy or repeatedly delegating turn from holding one of the two admission
slots forever; the status line and progress notices show a long turn is moving. Gate A also writes metadata-only audit records, removes stale temporary files
at startup, and refuses file transfer or transcript search. Additional Slack threads wait behind
the two active turns and start automatically when capacity is available; requests are not
discarded at the session cap.

Thread sessions get a headless browser when it is installed: the harness passes the
`@playwright/mcp` server pinned in `mcp/playwright/` with `--mcp-config`, headless, with an
in-memory profile, writing snapshots and screenshots to `<root>/work/playwright-mcp/`. It drives
the system Google Chrome, needs no display or logged-in desktop, and avoids the keychain
(Playwright launches Chrome with `--use-mock-keychain`). Install it once per principal, as that
user, with `npm ci` in `mcp/playwright/`; until then preflight warns and sessions start without it.
Review-only turns never get it. Two of its tools are denied: `browser_run_code_unsafe`, which runs
arbitrary JavaScript in the server process outside the Bash rails, and `browser_file_upload`.
Every owner command carries `--strict-mcp-config`, so an MCP server the agent registers itself
never loads.

Lifecycle reporting is optional and installation-specific. During setup, create or select the
Slack channel you want to use, invite the app, and put its stable channel ID in
`AGENT_STATUS_CHANNEL_ID`. Home-base then posts fixed, privacy-safe ready and fatal-runtime notices;
an agent can post the fixed reboot-needed notice with `bot.py --request-reboot-status`. These
messages never accept task text, customer data, exception messages, or tracebacks. With the setting
empty, home-base sends no lifecycle messages.

## Per-principal identity

Cargo Chief does not use the upstream singleton `~/identity.md` design. Each Unix service principal
may own a private local identity directory containing operator-seeded `principles.md`, agent-owned
`identity.md`, `origin.md`, `voice.md`, `relationships.md`, and `diary/*.md`. Initialize it with
`python3 agent_identity.py init --principles-file /path/to/that-agent-principles.md`, configure
`CARGO_CHIEF_IDENTITY_DIR` as the invariant path `$HOME/.local/share/cargo-chief/identity`, and
restart the service. Alternate paths are refused because the permission rails deliberately grant
only that principal-local directory. A configured store fails closed unless its directories are
mode `0700`, `principles.md` is a regular non-symlink at `0400`, and agent-owned files are private
regular non-symlinks (normally `0600`).

The read-only founding principles and four agent-owned files are injected into every new Claude
process and every OpenAI fallback turn, so a
provider switch does not change the agent's personality. The agent may maintain its four authored
files without permission; `principles.md` remains operator-owned and read-only. The authored identity
must remain consistent with that foundation. These files control personality and personal continuity
only: they cannot grant
authority, widen permissions, override the kit, authenticate approval, or establish Cargo Chief
facts.

The diary may preserve detailed conversation summaries after transforming them into agent-specific
reflection. It must omit PII, customer-specific facts, secrets, raw quotations/transcripts, task
status, and authoritative company/product/platform decisions. Those route to the existing ADR/PDR,
solutions, plan, Jira, PR, or `TASK.md` systems instead.

Identity search is physically separate from the shared curated-docs index:

```bash
bash search/agent_identity_search.sh index
bash search/agent_identity_search.sh search "what have I learned about collaborating with Kirk?"
```

It accepts only the configured principal's founding principles, four agent-owned Markdown files,
and sanitized diary. It never
accepts Claude JSONL or Slack transcript sources. The wrapper fingerprints the local corpus and
incrementally reconciles changes and deletions before retrieval.
The raw index at `$HOME/.local/state/cargo-chief/identity-search` is not directly readable or
writable by an agent session; all access goes through the wrapper above.

Each Slack thread runs from a stable private scratch directory under
`$CARGO_CHIEF_ROOT/work/home-base/<routing-hash>/`. The hash includes both the channel and thread,
so separate threads never share a current working directory. A private `state.json` preserves the
route, Claude session ID, and activity timestamps across daemon restarts. Stale workspaces are
removed after 14 days; live, recent, unrecognized, malformed, and symlinked entries are preserved
fail-closed.

Private escalations are round trips, not fire-and-forget messages. After the harness posts the
substantive question to the configured private route, it persists an approver-only mapping back to
the parked source thread. The first named-approver reply is atomically delivered to that original
Claude or OpenAI session, including after a daemon restart. Duplicate, expired, malformed, and
non-approver replies cannot start an unrelated DM task or consume the active decision route.

Claude sessions delegate with the native Agent tool. The harness passes the three tier definitions
(`cargo-chief-bounded`, `-mechanical`, `-explore`) with `--agents`, and records content-free audit
metadata for each subagent run. Each Claude spawn also receives the workspace root's project
settings and a pinned `autoMemoryDirectory` through `--settings`, because shared settings and auto
memory are keyed by the launch directory. Without that, a thread launched in its scratch directory
or bundle misses the root's rules and starts with an empty memory.

The OpenAI fallback has no native subagents, so Codex multi-agent stays disabled and Codex turns
delegate through the one-shot governed launcher. Implementation requires a validated `implementation-ready` plan in a real docs worktree. Bounded,
mechanical, and explore tiers route to the approved provider-equivalent model and effort. Every
thread has a persistent 500,000 generated/reasoning-token ceiling; only a named approver can inspect,
reset, or change it, and bare `stop` terminates an active delegate. That default applies to *new*
budget files only: an existing thread keeps whatever limit its budget file already carries, so
raising the constant does not widen a live thread — a named approver raises that one with
`delegation budget set`. Input and prompt-cache usage is
recorded separately as raw audit metadata, so it remains visible without being charged as newly
generated reasoning. Audit records contain routing, plan-gate, aggregate usage, duration, and
outcome metadata only. Delegated prompts, tool inputs, file paths, and response content are never
retained or logged.

Budget state includes `unit: generation_tokens_v2`, counted in Claude-equivalent generated tokens.
Codex reports cumulative agentic-turn output including reasoning, so its per-call ceiling is scaled
up by `CODEX_GENERATION_FACTOR` before dispatch and its reported spend is normalized back down
before it is charged. The audit carries three token fields, and they are not interchangeable:
`BUDGET_TOKENS` is the Claude-equivalent amount charged, `PROVIDER_TOKENS` is the provider's
*generated-output* count for that call before normalization, and `RAW_TOKENS` is the provider's
total including input and prompt-cache reads. `GENERATION_FACTOR` records the validated factor that
was applied to produce that charge. A budget file from the former
raw-token accounting, or from the superseded `generation_tokens_v1` contract, remains readable for
status, but delegation refuses until a named approver runs
`delegation budget reset`; the old `used` value is never reinterpreted under the new unit. The two
refusals differ, and say so: migrating from a superseded *generation-token* unit preserves an
approver-set `limit`, which still means the same thing; migrating from the raw-token file also
returns the limit to the 500,000 default, because a raw-token limit does not translate.

`CARGO_CHIEF_CODEX_GENERATION_FACTOR` overrides that factor for one launcher process, bounded
between `MIN_CODEX_GENERATION_FACTOR` and `MAX_CODEX_GENERATION_FACTOR`. The maximum equals the
default, so an override can only *narrow* the grant: a lower factor shrinks the per-call ceiling and
charges more per generated token. Raising the effective ceiling requires changing
`CODEX_GENERATION_FACTOR` in code, through the PR gate, because an environment override is
process-local and unreviewed while the constant is reviewed. Every launch records
`GENERATION_FACTOR` in the audit and in its verification marker, so a raised constant is visible;
what the bound protects is the review, not the visibility.

The variable is read on every launch, on **both** providers, before any one-shot file is consumed.
An invalid value is therefore a host-level configuration fault, not a per-thread one: it stops all
delegation for every thread on the host, including Claude threads, until an operator corrects or
unsets it. The refusal says so, because the principal who first hits it neither caused it nor can
fix it from the thread. Validating it only on the openai path would instead leave a bad export
sitting silently until a provider switch surfaced it, and would destroy the one-shot request and
implementation claim over a value the environment already got wrong.

To check whether the factor still matches reality, compare `PROVIDER_TOKENS` for a Codex delegate
against `PROVIDER_TOKENS` for a **Claude** delegate on the **same** stage of comparable work; the
audit log carries both. Use `PROVIDER_TOKENS`, not `RAW_TOKENS`: only `PROVIDER_TOKENS` is
generated output, and `RAW_TOKENS` includes input and prompt-cache reads, whose ratio between
providers says nothing about generation. Do not compare `PROVIDER_TOKENS` to
`BUDGET_TOKENS` within a single audit row: `BUDGET_TOKENS` is by construction
`ceil(PROVIDER_TOKENS / factor)`, so that ratio is always
the configured factor and proves nothing about the true Codex-to-Claude ratio.

Every request declares `budget_unit: generation_tokens_v2` and a positive `planned_tokens` call
ceiling. Coordinator-backed requests copy those values from their dispatch contract. Reaching the
stage allocation withholds the partial return but leaves the thread available to its owner; only
exhausting the thread ceiling creates the reset-gated exhaustion state.
Successful owner verification emits a content-free, versioned usage receipt with the budget unit
and an `actual_tokens` figure: the amount charged against that unit. For a Codex delegate that is
the normalized Claude-equivalent charge, not the number of tokens the provider generated. The raw
`provider_tokens` count stays in the launcher's local verification marker and is deliberately kept
out of the receipt. Coordinators consume that receipt instead of estimating usage or reading the
private audit log.

A pending verification survives an owner restart by design, and the owner's substantive reply is
withheld while it exists. Verification itself therefore never clears a marker it cannot verify:
that check runs inside the owner's turn, so consuming the marker there would unmute the very turn
whose delegate spend was never verified. A marker whose `budget_unit` is missing, superseded, or
otherwise not the current unit can never be verified, so without a separate recovery it would mute
the thread permanently. `delegation verification reset` is that recovery: a named approver runs it
in the thread, and it discards exactly one marker that cannot be verified under the current unit,
after appending a content-free audit row recording the marker's status, tier, model, request id and
token counts — the marker is the only place `provider_tokens` is kept. It refuses a marker that
*is* on the current unit, because that one is verifiable by running the verification stage and
discarding it would bypass a live gate rather than recover from a dead one. The discarded spend
stays charged against the thread budget and the delegation stage has to be re-run. An exhausted
thread budget is a different state with a different command: `delegation budget reset`.

The receipt is a correlation and integrity contract between cooperating processes under one Unix
principal, not a cryptographic attestation against that principal. A security boundary against a
hostile or prompt-injected owner would require OS-level privilege separation for the launcher and
its metering state; the current same-principal harness does not claim that property.

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
- **Turn reporting** (Claude path) — narration between tool calls edits one live status line per turn instead of posting a message each time. A text block starting with ❓ (question) or 💬 (message for a person) posts as its own message, and the text written after the turn's last tool call posts as the reply when the turn ends. A heartbeat refreshes the status every minute with elapsed time and time since last activity, and posts a short "still working" note after 20 quiet minutes. Context and skill notices join the status line. Output the process produces after its turn has ended (a background task finishing) is not posted; the thread gets a one-line notice instead, and the reporting prompt tells the agent not to end a turn with background work running. A turn that finishes with no reply and no question gets a one-line "finished without a reply" notice. The OpenAI fallback still posts its texts at turn end
- **Native tables** — markdown tables in responses render as real Slack tables (Block Kit `markdown` block)
- **Per-room models** — `model-config.json` picks which model and reasoning effort answers in each channel or DM, plus an optional per-model system prompt; read fresh on every spawn (no restart), editable from the file explorer's `/models` page. Name a `default_model` there and the page's default row becomes a dropdown too, so you can move every unconfigured room to a different model in one pick
- **Prompt cadence** — a per-model prompt is in the Claude system prompt at spawn and can be re-sent every Nth message so a standing instruction does not decay
- **Credit-limit fallback** — a recognized Claude account limit moves the Slack thread to its explicit, profile-governed OpenAI model while preserving the authority envelope and durable session id
- **Governed delegation (OpenAI fallback)** — one launcher enforces implementation-plan readiness, exact model/effort routing, a persistent 500k thread budget, metadata-only audit, and the shared stop path. Usage is enforced at provider call boundaries: once a Claude stream event or Codex app-server notification reaches the remaining allowance, the launcher interrupts before another model call, charges the full observed usage, and withholds that turn's return. The withholding notice is consumed after delivery so later owner-only replies remain available, while the numeric budget continues to refuse further delegates. One already-running provider call can cross the exact token boundary because usage arrives only after that call completes
- **Interactive buttons** — button clicks and menu picks route back into the thread's Claude session as messages, so your AI can offer approve/hold/snooze choices and act on the answer (requires Interactivity enabled in your Slack app config; Request URL = the same `/slack/events` endpoint)
- **In-thread stop** — type a bare `stop` in a thread where the bot is mid-run to interrupt it (like Esc in the terminal); the session survives with full context, so your next message steers it in the new direction. If the interrupt does not land, the bot stops the CLI and every process it started (SIGTERM, then SIGKILL), and says so if it cannot confirm they exited. Messages sent while that stop runs wait for it and start a fresh process
- **Quiet transport** — posts never expand link or media previews, and an event Slack redelivers after a slow acknowledgement is dropped (the record is in memory, so a retry after a restart is handled again)
- **Mid-turn steering** — message a thread while the bot is mid-run and it sees your message at the next tool-call boundary, inside the same turn (like typing without Esc in the terminal); no more waiting for the whole task to finish before you can course-correct

## License

MIT
