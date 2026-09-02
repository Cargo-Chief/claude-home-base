#!/usr/bin/env bash
set -euo pipefail

root="${CARGO_CHIEF_ROOT:?CARGO_CHIEF_ROOT must point to the Cargo Chief workspace}"
search_dir="${CARGO_CHIEF_SEARCH_DIR:-$HOME/.local/state/cargo-chief/knowledge}"
python_bin="${CARGO_CHIEF_SEARCH_PYTHON:-$HOME/.local/share/cargo-chief/knowledge-venv/bin/python}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

exec "$python_bin" "$script_dir/luoji_search.py" \
	--config "$script_dir/config.cargo-chief.yaml.example" "$@"
