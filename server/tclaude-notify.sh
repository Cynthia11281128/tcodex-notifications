#!/usr/bin/env bash
set -eu

command -v tclaude >/dev/null 2>&1 || {
  printf '%s\n' 'tclaude is not on PATH.' >&2
  exit 127
}
command -v python3 >/dev/null 2>&1 || exit 127
for argument in "$@"; do
  case "$argument" in
    --settings|--settings=*)
      printf '%s\n' 'This entrypoint owns --settings; use normal user/project settings for additional configuration.' >&2
      exit 2 ;;
  esac
done
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python_bin="$(command -v python3)"
channel="${TCLAUDE_NOTIFY_CHANNEL:-${TCODEX_NOTIFY_CHANNEL:-default}}"
export TCLAUDE_NOTIFICATION_RUNTIME="${TCLAUDE_NOTIFICATION_RUNTIME:-$script_dir/../runtime/.tclaude-auto-continue}"
export TMPDIR="${TCLAUDE_NOTIFY_TMP_DIR:-$script_dir/../../../tmp}"
export TMP="$TMPDIR" TEMP="$TMPDIR"
case "$(cd -- "$TMPDIR" && pwd -P)" in
  /tmp|/tmp/*) printf '%s\n' 'Temporary directory must not be under /tmp.' >&2; exit 2 ;;
esac
settings="$("$python_bin" -B -c '
import json, re, shlex, sys
python, directory, channel = sys.argv[1:]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", channel):
    sys.exit("Invalid notification channel.")
notify = shlex.join([python, "-B", directory + "/tclaude-events.py", "--channel", channel])
retry = shlex.join([python, "-B", directory + "/tclaude_retry.py"])
def hook(command, **options):
    return {"type": "command", "command": command, "timeout": 3, **options}
hooks = {
    "SessionStart": [{"hooks": [hook(retry)]}],
    "UserPromptSubmit": [{"hooks": [hook(retry)]}],
    "SessionEnd": [{"hooks": [hook(retry)]}],
    "Stop": [{"hooks": [hook(notify), hook(retry)]}],
    "StopFailure": [{"hooks": [hook(notify), hook(retry, asyncRewake=True, timeout=3600)]}],
    "PreToolUse": [{"matcher": "^AskUserQuestion$", "hooks": [hook(notify)]}],
}
print(json.dumps({"hooks": hooks}, ensure_ascii=True))
' "$python_bin" "$script_dir" "$channel")"
printf 'TClaude notification channel: %s. Review hooks in /hooks; automatic continuation requires a separate watcher.\n' "$channel" >&2
exec tclaude --settings "$settings" "$@"
