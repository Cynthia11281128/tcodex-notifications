#!/usr/bin/env bash
set -eu

if ! command -v tcodex >/dev/null 2>&1; then
  printf '%s\n' 'tcodex is not on PATH. Use a shell where the existing tcodex installation works.' >&2
  exit 127
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'python3 is required for the notification bridge.' >&2
  exit 127
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python_bin="$(command -v python3)"
channel="${TCODEX_NOTIFY_CHANNEL:-default}"
notify_config="$("$python_bin" -c '
import json, re, sys
python, script, channel = sys.argv[1:]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", channel):
    sys.exit("Invalid TCODEX_NOTIFY_CHANNEL; use 1-64 letters, digits, underscores or hyphens.")
print("notify=" + json.dumps([python, script, "--channel", channel], ensure_ascii=True))
' "$python_bin" "$script_dir/notify-event.py" "$channel")"

printf 'TCodex notification channel: %s. Keep the Mac listener connected.\n' "$channel" >&2
exec tcodex \
  -c 'tui.notifications=false' \
  -c "$notify_config" \
  "$@"
