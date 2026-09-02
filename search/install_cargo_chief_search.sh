#!/usr/bin/env bash
set -euo pipefail

python_bin="${1:-/opt/homebrew/bin/python3}"
venv_dir="${CARGO_CHIEF_SEARCH_VENV:-$HOME/.local/share/cargo-chief/knowledge-venv}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -x "$python_bin" ]]; then
	echo "Python not found at $python_bin; pass a Python 3.12+ executable as the first argument" >&2
	exit 2
fi

"$python_bin" -c 'import sqlite3,sys; assert sys.version_info >= (3,12); c=sqlite3.connect(":memory:"); assert hasattr(c,"enable_load_extension"); c.close()' || {
	echo "Python 3.12+ with SQLite extension loading is required" >&2
	exit 2
}

"$python_bin" -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade pip
"$venv_dir/bin/python" -m pip install -r "$script_dir/requirements.txt"

echo "Installed Cargo Chief knowledge search at $venv_dir"
echo "Next: export CARGO_CHIEF_ROOT, then run search/cargo_chief_search.sh doctor"
