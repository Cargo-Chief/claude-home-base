# Daily Diary

A nightly job where one service principal reviews its day and maintains its private local identity.
Each agent has a different Unix account, identity directory, diary, and search database.

## What it does

Each night (default 3:30 AM), a headless Claude session:

1. **Researches the day** — today's conversations and recent private diary entries are transient
   inputs for reflection, not another corpus to retain.
2. **Authors in quarantine** beneath `$CARGO_CHIEF_IDENTITY_DIR/.diary-staging/`. Detailed conversation summaries
   are allowed after transformation, but PII, customer-specific facts, secrets, raw quotations,
   transcripts, task state, and authoritative company/product/platform claims are prohibited.
3. **Reviews independently** — a fresh model turn sanitizes all five candidate files and emits a
   strict, machine-validated all-clear receipt. A missing, malformed, incomplete, or failing receipt
   leaves the live profile untouched and deletes the rejected candidate set so prohibited material
   does not become a second private archive.
4. **Promotes atomically** — only reviewed candidates replace the diary/core files. A failed
   promotion restores the prior core profile.
5. **Refreshes its private index** — only founding principles, the four agent-owned identity files,
   and sanitized diary enter this principal's identity database. Raw conversation logs never do.

## Files

| File | Purpose |
|------|---------|
| `daily-diary.sh` | Wrapper script: validation, non-mutating dry run, idempotency, timeout, truthful exit handling, logging, and `claude -p` |
| `diary-prompt.md` | The diary-writing instructions (the wrapper substitutes the date and passes this to Claude) |
| `diary-review-prompt.md` | A separate strict review/sanitization turn over quarantined candidates |
| `com.claude.daily-diary.plist` | LaunchAgent schedule for a principal with its own macOS login session |
| `com.cargo-chief.daily-diary.daemon.plist` | LaunchDaemon schedule for a headless service principal |

## Setup

```bash
python3 agent_identity.py init --principles-file /path/to/that-agent-principles.md
mkdir -p ~/scripts
cp jobs/daily-diary/daily-diary.sh   ~/scripts/
cp jobs/daily-diary/diary-prompt.md  ~/scripts/
cp jobs/daily-diary/diary-review-prompt.md ~/scripts/
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

### Scheduling a logged-in desktop principal

Only a principal with its own macOS login session has a `gui/<uid>` launchd domain. For that case,
copy and render `com.claude.daily-diary.plist` beneath `~/Library/LaunchAgents`, then load and verify:

```bash
launchctl load ~/Library/LaunchAgents/com.claude.daily-diary.plist
launchctl list | grep com.claude.daily-diary
```

### Scheduling a headless service principal

An account reached through `sudo -iu`, SSH, or another user's terminal has no GUI launchd domain.
Do not bootstrap its LaunchAgent into another user's domain, and never load that LaunchAgent as
root: without an explicit `UserName`, the diary would run as root.

Instead, render `com.cargo-chief.daily-diary.daemon.plist`, replacing `YOUR_USERNAME`, `YOUR_GROUP`,
and `CARGO_CHIEF_ROOT_PLACEHOLDER`. Validate the rendered copy, then install it as a root-owned
LaunchDaemon and bootstrap the system domain:

```bash
plutil -lint /path/to/rendered-diary-daemon.plist
sudo install -o root -g wheel -m 644 /path/to/rendered-diary-daemon.plist \
  /Library/LaunchDaemons/com.cargo-chief.daily-diary.plist
sudo launchctl bootstrap system \
  /Library/LaunchDaemons/com.cargo-chief.daily-diary.plist
sudo launchctl print system/com.cargo-chief.daily-diary
```

The daemon explicitly drops privileges to the configured service user and group before invoking
the diary script. The schedule is still 3:30 AM local and runs without an interactive login.

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
