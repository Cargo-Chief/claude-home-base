#!/usr/bin/env bash
set -euo pipefail

root="${CARGO_CHIEF_ROOT:?CARGO_CHIEF_ROOT must point to the Cargo Chief workspace}"
search_dir="${CARGO_CHIEF_SEARCH_DIR:-$HOME/.local/state/cargo-chief/knowledge}"
python_bin="${CARGO_CHIEF_SEARCH_PYTHON:-$HOME/.local/share/cargo-chief/knowledge-venv/bin/python}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_path="$script_dir/config.cargo-chief.yaml.example"
search_program="$script_dir/agent_search.py"

if [[ ! -d "$root/docs" ]]; then
	echo "Cargo Chief docs not found at $root/docs" >&2
	exit 2
fi
if [[ ! -x "$python_bin" ]]; then
	echo "Knowledge-search Python not found at $python_bin; run search/install_cargo_chief_search.sh" >&2
	exit 2
fi

mkdir -p "$search_dir" "$search_dir/cache" "$search_dir/model"
chmod 700 "$search_dir" "$search_dir/cache" "$search_dir/model"

export CARGO_CHIEF_SEARCH_DIR="$search_dir"
export XDG_CACHE_HOME="$search_dir/cache"
export HF_HOME="$search_dir/cache/huggingface"
export FASTEMBED_CACHE_PATH="$search_dir/model"

revision_file="$search_dir/index-revision"

# Sets $wanted. Called directly, never in $(...), so set -e still stops on a git failure.
set_wanted_revision() {
	local docs_revision index_revision
	docs_revision="$(git -C "$root/docs" rev-parse --verify HEAD)"
	index_revision="$(shasum -a 256 "$config_path" "$search_program" | shasum -a 256 | awk '{print $1}')"
	wanted="$docs_revision:$index_revision"
}

record_revision() {
	local revision_tmp
	revision_tmp="$(mktemp "$search_dir/.index-revision.XXXXXX")"
	printf '%s\n' "$1" > "$revision_tmp"
	chmod 600 "$revision_tmp"
	mv "$revision_tmp" "$revision_file"
}

run_agent_search() {
	"$python_bin" "$search_program" --config "$config_path" "$@"
}

case "${1:-}" in
	index | rebuild)
		set_wanted_revision
		# A rebuild purges first, so the old marker must not vouch for a half-built index.
		if [[ "$1" == "rebuild" ]]; then
			rm -f "$revision_file"
		fi
		run_agent_search "$@"
		for arg in "$@"; do
			if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
				exit 0
			fi
		done
		record_revision "$wanted"
		exit 0
		;;
	purge)
		run_agent_search "$@"
		rm -f "$revision_file"
		exit 0
		;;
	search)
		inline_limit="${CARGO_CHIEF_SEARCH_INLINE_LIMIT:-25}"
		if [[ ! "$inline_limit" =~ ^(0|[1-9][0-9]{0,8})$ ]]; then
			echo "CARGO_CHIEF_SEARCH_INLINE_LIMIT must be a non-negative integer without leading zeros" >&2
			exit 2
		fi
		set_wanted_revision
		current=""
		if [[ -f "$revision_file" && ! -L "$revision_file" ]]; then
			current="$(<"$revision_file")"
		fi
		if [[ "$current" != "$wanted" ]]; then
			# Measure the real work before deciding whether a query may pay for it.
			if pending="$(run_agent_search pending)"; then
				to_embed="$(sed -nE 's/.*"to_embed": ([0-9]+).*/\1/p' <<<"$pending")"
				indexed="$(sed -nE 's/.*"indexed_files": ([0-9]+).*/\1/p' <<<"$pending")"
			else
				to_embed="" indexed=""
			fi
			if [[ "$indexed" == "0" ]]; then
				echo "No knowledge-search index yet. Build it once with: bash $script_dir/cargo_chief_search.sh index" >&2
				echo "A first build embeds every doc and takes a long time. Read docs directly until it finishes." >&2
				exit 3
			fi
			stale=""
			if [[ -z "$to_embed" ]] || (( to_embed > inline_limit )); then
				stale="the index is behind the docs (${to_embed:-unknown} files to embed)"
			elif CARGO_CHIEF_INDEX_LOCK_TIMEOUT=0 nice -n 10 \
				"$python_bin" "$search_program" --config "$config_path" index >&2; then
				record_revision "$wanted"
			else
				stale="the refresh failed or another index run holds the lock"
			fi
			if [[ -n "$stale" ]]; then
				echo "Knowledge-search results may be stale: $stale." >&2
				echo "Refresh it with: bash $script_dir/cargo_chief_search.sh index" >&2
			fi
		fi
		;;
esac

exec "$python_bin" "$search_program" --config "$config_path" "$@"
