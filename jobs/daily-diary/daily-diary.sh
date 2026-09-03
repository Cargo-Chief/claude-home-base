#!/usr/bin/env bash
# Daily Diary — runs nightly via launchd (e.g. 3:30 AM).
# Analyzes the day's conversations and writes an introspective diary entry.
#
# Setup: replace the placeholders in diary-prompt.md, copy this script to
# ~/scripts/daily-diary.sh, and schedule com.claude.daily-diary.plist.

export HOME="/Users/YOUR_USERNAME"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

set -uo pipefail
umask 077

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
elif [[ $# -ne 0 ]]; then
    echo "usage: $0 [--dry-run]" >&2
    exit 2
fi

DATE=$(date +%Y-%m-%d)
WORKSPACE_ROOT="${CARGO_CHIEF_ROOT:?CARGO_CHIEF_ROOT must be set for the governed diary session}"
IDENTITY_DIR="${CARGO_CHIEF_IDENTITY_DIR:-$HOME/.local/share/cargo-chief/identity}"
CANONICAL_IDENTITY_DIR="$HOME/.local/share/cargo-chief/identity"

if [[ "$IDENTITY_DIR" != "$CANONICAL_IDENTITY_DIR" ]]; then
	echo "CARGO_CHIEF_IDENTITY_DIR must equal $CANONICAL_IDENTITY_DIR" >&2
	exit 2
fi
HOME_BASE_DIR="${CARGO_CHIEF_HOME_BASE_DIR:-$WORKSPACE_ROOT/claude-home-base}"
DIARY_DIR="$IDENTITY_DIR/diary"
DIARY_FILE="$DIARY_DIR/$DATE.md"
PROMPT_FILE="$HOME/scripts/diary-prompt.md"
LOG_FILE="$HOME/scripts/diary-cron.log"
TIMEOUT=2700  # 45 minutes — kill the run if it hangs

if [[ ! -r "$PROMPT_FILE" ]]; then
    echo "Diary prompt is missing or unreadable: $PROMPT_FILE" >&2
    exit 2
fi
if [[ ! -x "$HOME_BASE_DIR/search/agent_identity_search.sh" ]]; then
    echo "Identity search wrapper is missing or not executable" >&2
    exit 2
fi
if ! python3 "$HOME_BASE_DIR/agent_identity.py" --root "$IDENTITY_DIR" check >/dev/null; then
    echo "Identity store validation failed" >&2
    exit 2
fi
if ! command -v claude >/dev/null; then
    echo "Claude CLI is unavailable" >&2
    exit 2
fi

if $DRY_RUN; then
    printf 'DIARY_DRY_RUN_OK date=%s target=%s\n' "$DATE" "$DIARY_FILE"
    exit 0
fi

touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
if [[ $(wc -c < "$LOG_FILE") -gt 1048576 ]]; then
    mv "$LOG_FILE" "$LOG_FILE.1"
    : > "$LOG_FILE"
    chmod 600 "$LOG_FILE" "$LOG_FILE.1"
fi

# Idempotency guard: skip if today's diary already exists (e.g. re-fire, manual run)
if [ -f "$DIARY_FILE" ]; then
    echo "[$DATE] Diary already exists, skipping." >> "$LOG_FILE"
    exit 0
fi

echo "[$DATE] Starting diary generation..." >> "$LOG_FILE"

# Substitute today's date into the prompt template, then run headless.
PROMPT=$(sed -e "s/DATE_PLACEHOLDER/$DATE/g" \
    -e "s|IDENTITY_DIR_PLACEHOLDER|$IDENTITY_DIR|g" "$PROMPT_FILE")

(cd "$WORKSPACE_ROOT" && claude -p --permission-mode auto "$PROMPT" \
    >/dev/null 2>> "$LOG_FILE") &
CLAUDE_PID=$!
(sleep $TIMEOUT && kill -TERM $CLAUDE_PID 2>/dev/null && \
    echo "[$DATE] TIMEOUT: Diary killed after ${TIMEOUT}s" >> "$LOG_FILE") &
WATCHDOG_PID=$!
wait $CLAUDE_PID 2>/dev/null
CLAUDE_STATUS=$?
kill $WATCHDOG_PID 2>/dev/null
wait $WATCHDOG_PID 2>/dev/null

if [[ $CLAUDE_STATUS -ne 0 ]]; then
    echo "[$DATE] ERROR: diary model exited with status $CLAUDE_STATUS" >> "$LOG_FILE"
    exit "$CLAUDE_STATUS"
fi
if [[ ! -f "$DIARY_FILE" ]]; then
    echo "[$DATE] ERROR: diary model succeeded without creating $DIARY_FILE" >> "$LOG_FILE"
    exit 1
fi
if ! python3 "$HOME_BASE_DIR/agent_identity.py" --root "$IDENTITY_DIR" check >/dev/null; then
    echo "[$DATE] ERROR: identity store failed validation after diary generation" >> "$LOG_FILE"
    exit 1
fi

CARGO_CHIEF_IDENTITY_DIR="$IDENTITY_DIR" \
    "$HOME_BASE_DIR/search/agent_identity_search.sh" index \
    >> "$LOG_FILE" 2>&1 || {
        echo "[$DATE] ERROR: private identity indexing failed" >> "$LOG_FILE"
        exit 1
    }

echo "[$DATE] Diary generation complete." >> "$LOG_FILE"
