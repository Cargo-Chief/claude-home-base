#!/usr/bin/env bash
# Daily team status: runs weekday mornings via launchd as the agent's own principal.
# One Claude pass drafts the status from records, a second pass checks it for the shared channel,
# and bot.py --post-daily-status renders and posts it. Claude never posts to Slack itself.
# Runs from this principal's home-base clone, so updates arrive with update_agent.sh.

set -uo pipefail
umask 077
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
	DRY_RUN=true
elif [[ $# -ne 0 ]]; then
	echo "usage: $0 [--dry-run]" >&2
	exit 2
fi

WORKSPACE_ROOT="${CARGO_CHIEF_ROOT:?CARGO_CHIEF_ROOT must be set for the governed status session}"
HOME_BASE_DIR="${CARGO_CHIEF_HOME_BASE_DIR:-$WORKSPACE_ROOT/claude-home-base}"
JOB_DIR="$HOME_BASE_DIR/jobs/daily-status"
HOME_BASE_PYTHON="$HOME/.local/share/cargo-chief/home-base-venv/bin/python"
CREDENTIAL_WRAPPER="$HOME_BASE_DIR/with_harness_credential.py"
# The home-base logs folder, so the operator account that reads the service logs reads this too.
LOG_DIR="$HOME/.local/state/cargo-chief/home-base/logs"
LOG_FILE="$LOG_DIR/daily-status.log"
TIMEOUT=1500  # 25 minutes per model pass

DATE=$(date +%Y-%m-%d)
# Monday's post covers Friday through Sunday; every other day covers the day before.
if [[ $(date +%u) -eq 1 ]]; then
	SINCE=$(date -v-3d +%Y-%m-%d)
	COVERS="$(date -v-3d '+%a %b %-d') to $(date -v-1d '+%a %b %-d')"
else
	SINCE=$(date -v-1d +%Y-%m-%d)
	COVERS="$(date -v-1d '+%a %b %-d')"
fi
RUN_DIR="$WORKSPACE_ROOT/work/daily-status/$DATE"

for required in "$JOB_DIR/status-prompt.md" "$JOB_DIR/status-review-prompt.md" \
	"$JOB_DIR/status-revise-prompt.md" \
	"$HOME_BASE_DIR/bot.py" "$CREDENTIAL_WRAPPER"; do
	if [[ ! -r "$required" ]]; then
		echo "Daily status file is missing or unreadable: $required" >&2
		exit 2
	fi
done
for tool in claude python3; do
	if ! command -v "$tool" >/dev/null; then
		echo "$tool is unavailable" >&2
		exit 2
	fi
done
if [[ ! -x "$HOME_BASE_PYTHON" ]]; then
	echo "Home-base Python is missing: $HOME_BASE_PYTHON" >&2
	exit 2
fi
if ! "$HOME_BASE_PYTHON" "$CREDENTIAL_WRAPPER" --check; then
	echo "Harness credential is unavailable; claude would fall back to an expiring login" >&2
	exit 2
fi
SELF_LOGIN=$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("peer_review", {}).get("self_github_login", ""))
except Exception:
    print("")' "$HOME_BASE_DIR/model-config.json")

if $DRY_RUN; then
	printf 'STATUS_DRY_RUN_OK date=%s since=%s covers="%s" login=%s run_dir=%s\n' \
		"$DATE" "$SINCE" "$COVERS" "${SELF_LOGIN:-unknown}" "$RUN_DIR"
	exit 0
fi

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
if [[ $(wc -c < "$LOG_FILE") -gt 1048576 ]]; then
	mv "$LOG_FILE" "$LOG_FILE.1"
	: > "$LOG_FILE"
	chmod 600 "$LOG_FILE" "$LOG_FILE.1"
fi
log() { echo "$(date '+%F %T') $*" >> "$LOG_FILE"; }

if [[ -f "$RUN_DIR/posted" ]]; then
	log "[$DATE] already posted, skipping"
	exit 0
fi
mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
rm -f "$RUN_DIR/draft.json" "$RUN_DIR/review.json" "$RUN_DIR/threads.json" "$RUN_DIR/author.out" "$RUN_DIR/review.out" \
	"$RUN_DIR/review-1.json" "$RUN_DIR/revise.out" "$RUN_DIR/review-2.out"

run_model() {
	local phase="$1" out="$2" prompt="$3"
	# claude -p prints its errors, a usage limit included, on stdout; bot.py reads the file for one.
	(cd "$WORKSPACE_ROOT" && exec "$HOME_BASE_PYTHON" "$CREDENTIAL_WRAPPER" -- \
		claude -p --permission-mode auto "$prompt" > "$out" 2>> "$LOG_FILE") &
	local model_pid=$!
	(sleep "$TIMEOUT" && kill -TERM "$model_pid" 2>/dev/null && \
		log "[$DATE] TIMEOUT: $phase killed after ${TIMEOUT}s") &
	local watchdog_pid=$!
	wait "$model_pid" 2>/dev/null
	local status=$?
	kill "$watchdog_pid" 2>/dev/null
	wait "$watchdog_pid" 2>/dev/null
	if [[ $status -ne 0 ]]; then
		log "[$DATE] ERROR: $phase exited with status $status: $(tail -n 1 "$out" 2>/dev/null | cut -c1-200)"
	fi
	return "$status"
}

render_prompt() {
	sed -e "s|DATE_PLACEHOLDER|$DATE|g" -e "s|SINCE_PLACEHOLDER|$SINCE|g" \
		-e "s|COVERS_PLACEHOLDER|$COVERS|g" -e "s|RUN_DIR_PLACEHOLDER|$RUN_DIR|g" \
		-e "s|ROOT_PLACEHOLDER|$WORKSPACE_ROOT|g" -e "s|LOGIN_PLACEHOLDER|${SELF_LOGIN:-unknown}|g" \
		-e "s|DRAFT_SHA_PLACEHOLDER|${DRAFT_SHA:-}|g" "$1"
}

# True when review.json withholds exactly the current draft.
withheld_this_draft() {
	python3 -c 'import json,sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if r.get("verdict") == "withhold" and r.get("draft_sha256") == sys.argv[2] else 1)' \
		"$RUN_DIR/review.json" "$DRAFT_SHA"
}

# Thread permalinks for the author pass: transcripts carry no Slack thread id.
if ! python3 "$HOME_BASE_DIR/daily_status.py" threads "$WORKSPACE_ROOT" "$RUN_DIR/threads.json" 2>> "$LOG_FILE"; then
	log "[$DATE] WARNING: could not list thread workspaces"
	echo "[]" > "$RUN_DIR/threads.json"
fi
log "[$DATE] drafting status since $SINCE"
if run_model "status author" "$RUN_DIR/author.out" "$(render_prompt "$JOB_DIR/status-prompt.md")" && [[ -f "$RUN_DIR/draft.json" ]]; then
	DRAFT_SHA=$(shasum -a 256 "$RUN_DIR/draft.json" | cut -d' ' -f1)
	run_model "status reviewer" "$RUN_DIR/review.out" "$(render_prompt "$JOB_DIR/status-review-prompt.md")" || true
	# One revision for a withheld draft: fix the named items, then a fresh review of the new draft.
	if withheld_this_draft; then
		cp "$RUN_DIR/review.json" "$RUN_DIR/review-1.json"
		log "[$DATE] review withheld the draft; revising once"
		if run_model "status reviser" "$RUN_DIR/revise.out" "$(render_prompt "$JOB_DIR/status-revise-prompt.md")" \
			&& [[ -f "$RUN_DIR/draft.json" ]]; then
			REVISED_SHA=$(shasum -a 256 "$RUN_DIR/draft.json" | cut -d' ' -f1)
			if [[ "$REVISED_SHA" != "$DRAFT_SHA" ]]; then
				DRAFT_SHA="$REVISED_SHA"
				rm -f "$RUN_DIR/review.json"
				run_model "status reviewer (revised)" "$RUN_DIR/review-2.out" \
					"$(render_prompt "$JOB_DIR/status-review-prompt.md")" || true
			else
				log "[$DATE] revision left the draft unchanged"
			fi
		fi
	fi
else
	log "[$DATE] ERROR: no draft produced"
	rm -f "$RUN_DIR/draft.json"
fi

# bot.py posts exactly one message whatever happened above: the status, or a fixed
# withheld/unavailable notice, so a failed run is visible in the channel rather than silent.
if (cd "$WORKSPACE_ROOT" && "$HOME_BASE_PYTHON" "$HOME_BASE_DIR/bot.py" --post-daily-status "$RUN_DIR") >> "$LOG_FILE" 2>&1; then
	log "[$DATE] post step finished"
else
	log "[$DATE] ERROR: post step failed"
	exit 1
fi

# Keep a week of run directories for debugging.
find "$WORKSPACE_ROOT/work/daily-status" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
