#!/usr/bin/env bash
set -eu

if [ "$(uname -s)" != 'Darwin' ]; then
  printf '%s\n' 'Local TCodex notifications require macOS.' >&2
  exit 2
fi

for dependency in tcodex python3 osascript; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf 'Local TCodex notifications require %s on PATH.\n' "$dependency" >&2
    exit 127
  fi
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python_bin="$(command -v python3)"
notify_config="$("$python_bin" -B -c '
import json, sys
from pathlib import Path
python, script = sys.argv[1:]
print("notify=" + json.dumps([str(Path(python).resolve()), "-B", script], ensure_ascii=True))
' "$python_bin" "$script_dir/notify-event.py")"

printf '%s\n' 'TCodex local notifications enabled for this session.' >&2
exec tcodex \
  -c 'tui.notifications=false' \
  -c "$notify_config" \
  "$@"
