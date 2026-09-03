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
AUTHOR_PROMPT_FILE="$HOME/scripts/diary-prompt.md"
REVIEW_PROMPT_FILE="$HOME/scripts/diary-review-prompt.md"
LOG_FILE="$HOME/scripts/diary-cron.log"
TIMEOUT=2700  # 45 minutes — kill the run if it hangs
PIPELINE="$HOME_BASE_DIR/diary_pipeline.py"

for required in "$AUTHOR_PROMPT_FILE" "$REVIEW_PROMPT_FILE" "$PIPELINE"; do
    if [[ ! -r "$required" ]]; then
        echo "Diary pipeline file is missing or unreadable: $required" >&2
        exit 2
    fi
done
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

if ! STAGE_DIR=$(python3 "$PIPELINE" prepare --root "$IDENTITY_DIR" --date "$DATE"); then
    echo "[$DATE] ERROR: could not prepare quarantined diary candidates" >> "$LOG_FILE"
    exit 1
fi

run_model() {
    local phase="$1"
    local prompt="$2"
    (cd "$WORKSPACE_ROOT" && claude -p --permission-mode auto "$prompt" \
        >/dev/null 2>> "$LOG_FILE") &
    local model_pid=$!
    (sleep "$TIMEOUT" && kill -TERM "$model_pid" 2>/dev/null && \
        echo "[$DATE] TIMEOUT: $phase killed after ${TIMEOUT}s" >> "$LOG_FILE") &
    local watchdog_pid=$!
    wait "$model_pid" 2>/dev/null
    local model_status=$?
    kill "$watchdog_pid" 2>/dev/null
    wait "$watchdog_pid" 2>/dev/null
    if [[ $model_status -ne 0 ]]; then
        echo "[$DATE] ERROR: $phase exited with status $model_status" >> "$LOG_FILE"
        return "$model_status"
    fi
}

discard_candidates() {
    python3 "$PIPELINE" discard --root "$IDENTITY_DIR" --date "$DATE" >/dev/null 2>&1 || \
        echo "[$DATE] ERROR: failed to discard rejected candidates at $STAGE_DIR" >> "$LOG_FILE"
}

AUTHOR_PROMPT=$(sed -e "s/DATE_PLACEHOLDER/$DATE/g" \
    -e "s|IDENTITY_DIR_PLACEHOLDER|$IDENTITY_DIR|g" \
    -e "s|STAGE_DIR_PLACEHOLDER|$STAGE_DIR|g" "$AUTHOR_PROMPT_FILE")
if ! run_model "diary author" "$AUTHOR_PROMPT"; then
    discard_candidates
    exit 1
fi

REVIEW_PROMPT=$(sed -e "s/DATE_PLACEHOLDER/$DATE/g" \
    -e "s|IDENTITY_DIR_PLACEHOLDER|$IDENTITY_DIR|g" \
    -e "s|STAGE_DIR_PLACEHOLDER|$STAGE_DIR|g" "$REVIEW_PROMPT_FILE")
if ! run_model "diary reviewer" "$REVIEW_PROMPT"; then
    discard_candidates
    exit 1
fi

if ! python3 "$PIPELINE" promote --root "$IDENTITY_DIR" --date "$DATE" >/dev/null; then
    echo "[$DATE] ERROR: independent diary review did not produce a valid all-clear receipt" >> "$LOG_FILE"
    discard_candidates
    exit 1
fi

CARGO_CHIEF_IDENTITY_DIR="$IDENTITY_DIR" \
    "$HOME_BASE_DIR/search/agent_identity_search.sh" index \
    >> "$LOG_FILE" 2>&1 || {
        echo "[$DATE] ERROR: private identity indexing failed" >> "$LOG_FILE"
        exit 1
    }

echo "[$DATE] Diary generation complete." >> "$LOG_FILE"
