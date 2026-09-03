#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
identity_dir="${CARGO_CHIEF_IDENTITY_DIR:-$HOME/.local/share/cargo-chief/identity}"
search_dir="${CARGO_CHIEF_IDENTITY_SEARCH_DIR:-$HOME/.local/state/cargo-chief/identity-search}"
canonical_identity_dir="$HOME/.local/share/cargo-chief/identity"
canonical_search_dir="$HOME/.local/state/cargo-chief/identity-search"
python_bin="${CARGO_CHIEF_SEARCH_PYTHON:-$HOME/.local/share/cargo-chief/knowledge-venv/bin/python}"
config_path="$script_dir/config.agent-identity.yaml.example"
search_program="$script_dir/agent_search.py"

if [[ "$identity_dir" != "$canonical_identity_dir" ]]; then
	echo "CARGO_CHIEF_IDENTITY_DIR must equal $canonical_identity_dir" >&2
	exit 2
fi
if [[ "$search_dir" != "$canonical_search_dir" ]]; then
	echo "CARGO_CHIEF_IDENTITY_SEARCH_DIR must equal $canonical_search_dir" >&2
	exit 2
fi

export CARGO_CHIEF_IDENTITY_DIR="$identity_dir"
export CARGO_CHIEF_IDENTITY_SEARCH_DIR="$search_dir"
export CARGO_CHIEF_SEARCH_DIR="$search_dir"
export XDG_CACHE_HOME="$search_dir/cache"
export HF_HOME="$search_dir/cache/huggingface"
export FASTEMBED_CACHE_PATH="$search_dir/model"

if [[ ! -x "$python_bin" ]]; then
	echo "Knowledge-search Python not found; run search/install_cargo_chief_search.sh" >&2
	exit 2
fi

mkdir -p "$search_dir" "$search_dir/cache" "$search_dir/model"
chmod 700 "$search_dir" "$search_dir/cache" "$search_dir/model"
"$python_bin" "$repo_dir/agent_identity.py" --root "$identity_dir" check >/dev/null

write_revision() {
	local content_revision index_revision wanted_revision revision_tmp
	content_revision="$("$python_bin" "$repo_dir/agent_identity.py" --root "$identity_dir" revision)"
	index_revision="$(shasum -a 256 "$config_path" "$search_program" | shasum -a 256 | awk '{print $1}')"
	wanted_revision="$content_revision:$index_revision"
	revision_tmp="$(mktemp "$search_dir/.index-revision.XXXXXX")"
	trap 'rm -f "$revision_tmp"' EXIT
	printf '%s\n' "$wanted_revision" > "$revision_tmp"
	chmod 600 "$revision_tmp"
	mv "$revision_tmp" "$search_dir/index-revision"
	trap - EXIT
}

if [[ "${1:-}" == "index" ]]; then
	"$python_bin" "$search_program" --config "$config_path" "$@"
	write_revision
	exit 0
elif [[ "${1:-}" == "search" ]]; then
	content_revision="$("$python_bin" "$repo_dir/agent_identity.py" --root "$identity_dir" revision)"
	index_revision="$(shasum -a 256 "$config_path" "$search_program" | shasum -a 256 | awk '{print $1}')"
	wanted_revision="$content_revision:$index_revision"
	revision_file="$search_dir/index-revision"
	current_revision=""
	if [[ -f "$revision_file" && ! -L "$revision_file" ]]; then
		current_revision="$(<"$revision_file")"
	fi
	if [[ "$current_revision" != "$wanted_revision" ]]; then
		"$python_bin" "$search_program" --config "$config_path" index >&2
		write_revision
	fi
fi

exec "$python_bin" "$search_program" --config "$config_path" "$@"
