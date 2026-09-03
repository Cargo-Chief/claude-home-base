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
4. **Refreshes its private index** — only founding principles, the four agent-owned identity files,
   and sanitized diary enter this principal's identity database. Raw conversation logs never do.

## Files

| File | Purpose |
|------|---------|
| `daily-diary.sh` | Wrapper script: validation, non-mutating dry run, idempotency, timeout, truthful exit handling, logging, and `claude -p` |
| `diary-prompt.md` | The diary-writing instructions (the wrapper substitutes the date and passes this to Claude) |
| `com.claude.daily-diary.plist` | launchd schedule (3:30 AM local) |

## Setup

```bash
python3 agent_identity.py init --principles-file /path/to/that-agent-principles.md
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
`DATE_PLACEHOLDER` is substituted automatically by the wrapper — leave it as-is.

The job deliberately starts Claude from `CARGO_CHIEF_ROOT`, not from the home directory, so the
same generated `AGENTS.md`/`CLAUDE.md` and permission rails govern reflection. A missing root stops
the job; it never falls back to an ungoverned working directory.

Run the non-mutating preflight before loading launchd. It validates paths, the identity store,
required commands, and the target date without invoking a model or writing a diary entry:

```bash
~/scripts/daily-diary.sh --dry-run
# DIARY_DRY_RUN_OK date=YYYY-MM-DD target=/Users/<principal>/.../diary/YYYY-MM-DD.md
```

Load and verify:

```bash
launchctl load ~/Library/LaunchAgents/com.claude.daily-diary.plist
launchctl list | grep com.claude.daily-diary
```

Run one explicit live acceptance (this writes today's entry, or skips if one already exists):

```bash
launchctl start com.claude.daily-diary
tail -f ~/scripts/diary-cron.log
```

The live acceptance passes only when the entry exists, the complete identity store validates, and
the private index refresh succeeds. A model, validation, or indexing failure exits nonzero and logs
`ERROR`; it must never be reported as a completed diary run.

Model prose goes only into the private identity files, not scheduler stdout. The operational log is
mode 0600, contains process and error status rather than the model's reflective summary, and rotates
at 1 MiB with one retained predecessor. The launchd plist also sets umask 077 for its own stdout and
stderr files.

## Notes

- Use `search/agent_identity_search.sh`; do not add the diary to the shared Cargo Chief docs index.
- Want a weekly synthesis on top? Add a second job that reads the last 7 entries and writes a `~/diary/weekly-YYYY-WW.md` — same pattern, `Weekday` set in the plist.
- `principles.md` is operator-seeded, verbatim, and read-only. The four authored identity files and
  diary are agent-writable local state, not repository files. None can grant authority or override
  the kit, and one principal must never index another principal's directory.
