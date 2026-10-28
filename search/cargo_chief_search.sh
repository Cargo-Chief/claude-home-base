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

inline_limit="${CARGO_CHIEF_SEARCH_INLINE_LIMIT:-25}"
if [[ ! "$inline_limit" =~ ^[0-9]+$ ]]; then
	echo "CARGO_CHIEF_SEARCH_INLINE_LIMIT must be a non-negative integer" >&2
	exit 2
fi

docs_revision="$(git -C "$root/docs" rev-parse --verify HEAD)"
index_revision="$(shasum -a 256 "$config_path" "$search_program" | shasum -a 256 | awk '{print $1}')"
wanted_revision="$docs_revision:$index_revision"
revision_file="$search_dir/index-revision"

record_revision() {
	revision_tmp="$(mktemp "$search_dir/.index-revision.XXXXXX")"
	trap 'rm -f "$revision_tmp"' EXIT
	printf '%s\n' "$wanted_revision" > "$revision_tmp"
	chmod 600 "$revision_tmp"
	mv "$revision_tmp" "$revision_file"
	trap - EXIT
}

case "${1:-}" in
	index | rebuild)
		"$python_bin" "$search_program" --config "$config_path" "$@"
		record_revision
		exit 0
		;;
	purge)
		rm -f "$revision_file"
		;;
esac

if [[ "${1:-}" == "search" ]]; then
	current_revision=""
	if [[ -f "$revision_file" && ! -L "$revision_file" ]]; then
		current_revision="$(<"$revision_file")"
	fi
	if [[ -z "$current_revision" ]]; then
		echo "No knowledge-search index yet. Build it once with: bash $script_dir/cargo_chief_search.sh index" >&2
		echo "A first build embeds every doc and can take an hour. Read docs directly until it finishes." >&2
		exit 3
	fi
	if [[ "$current_revision" != "$wanted_revision" ]]; then
		# Refresh inline only when it is small; a large refresh would stall the query and the host.
		indexed_docs="${current_revision%%:*}"
		if ! changed="$(git -C "$root/docs" diff --name-only "$indexed_docs" "$docs_revision" -- '*.md' 2>/dev/null | wc -l | tr -d ' ')"; then
			changed="unknown"
		fi
		if [[ "$changed" != "unknown" ]] && (( changed <= inline_limit )); then
			nice -n 10 "$python_bin" "$search_program" --config "$config_path" index >&2
			record_revision
		else
			echo "Knowledge-search index is behind the docs ($changed changed files); results may be stale." >&2
			echo "Refresh it with: bash $script_dir/cargo_chief_search.sh index" >&2
		fi
	fi
fi

exec "$python_bin" "$search_program" --config "$config_path" "$@"
