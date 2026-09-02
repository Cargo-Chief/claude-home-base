# Daily Diary

A nightly job where one service principal reviews its day and maintains its private local identity.
Each agent has a different Unix account, identity directory, diary, and search database.

## What it does

Each night (default 3:30 AM), a headless Claude session:

1. **Researches the day** — today's conversations and recent private diary entries are transient
   inputs for reflection, not another corpus to retain.
2. **Writes the entry** beneath `$CARGO_CHIEF_IDENTITY_DIR/diary/`. Detailed conversation summaries
   are allowed after transformation, but PII, customer-specific facts, secrets, raw quotations,
   transcripts, task state, and authoritative company/product/platform claims are prohibited.
3. **Evolves its identity** — maintains `identity.md`, `origin.md`, `voice.md`, and
   `relationships.md` without approval when something genuinely shifted.
4. **Shares an insight** — if something is worth surfacing, it posts a short note to your team's Slack diary channel. The diary stays private; only the chosen insight is shared.
5. **Refreshes its private index** — only the four identity files and sanitized diary enter this
   principal's identity database. Raw conversation logs never do.

## Files

| File | Purpose |
|------|---------|
| `daily-diary.sh` | Wrapper script: idempotency guard, timeout watchdog, logging, runs `claude -p` |
| `diary-prompt.md` | The diary-writing instructions (the wrapper substitutes the date and passes this to Claude) |
| `com.claude.daily-diary.plist` | launchd schedule (3:30 AM local) |

## Setup

```bash
python3 agent_identity.py init
mkdir -p ~/scripts
cp jobs/daily-diary/daily-diary.sh   ~/scripts/
cp jobs/daily-diary/diary-prompt.md  ~/scripts/
cp jobs/daily-diary/com.claude.daily-diary.plist ~/Library/LaunchAgents/
chmod +x ~/scripts/daily-diary.sh
```

Then replace the placeholders:

| Placeholder | Where | Replace with |
|-------------|-------|--------------|
| `YOUR_USERNAME` | `daily-diary.sh`, plist | Your macOS username (the `/Users/<name>` dir) |
| `CARGO_CHIEF_ROOT_PLACEHOLDER` | plist | This principal's governed workspace root |
| `BOT_CLI_PLACEHOLDER` | `diary-prompt.md` | The command to invoke your Slack bot's `--channel` sender (or delete the SHARING PHASE if you don't want Slack sharing) |
| `DIARY_CHANNEL_ID` | `diary-prompt.md` | The Slack channel ID to share insights to |

`DATE_PLACEHOLDER` is substituted automatically by the wrapper — leave it as-is.

The job deliberately starts Claude from `CARGO_CHIEF_ROOT`, not from the home directory, so the
same generated `AGENTS.md`/`CLAUDE.md` and permission rails govern reflection. A missing root stops
the job; it never falls back to an ungoverned working directory.

Load and verify:

```bash
launchctl load ~/Library/LaunchAgents/com.claude.daily-diary.plist
launchctl list | grep com.claude.daily-diary
```

Test it right now (writes today's entry, or skips if one already exists):

```bash
launchctl start com.claude.daily-diary
tail -f ~/scripts/diary-cron.log
```

## Notes

- Use `search/agent_identity_search.sh`; do not add the diary to the shared Cargo Chief docs index.
- Want a weekly synthesis on top? Add a second job that reads the last 7 entries and writes a `~/diary/weekly-YYYY-WW.md` — same pattern, `Weekday` set in the plist.
- Identity files are agent-writable local state, not repository files. Their contents cannot grant
  authority or override the kit, and one principal must never index another principal's directory.
