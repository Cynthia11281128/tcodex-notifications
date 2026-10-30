#!/usr/bin/env bash
set -eu

usage() {
  printf '%s\n' \
    'Usage: bash listen.sh --server-dir PATH [--port PORT] [--channel NAME] [--once] SSH_DEST' \
    '       bash listen.sh --test-notification' \
    'Run locally on the Mac, not inside a server SSH session. Notifications use osascript.' \
    'Existing --notifier osascript commands remain supported.' \
    'SSH_DEST is your existing SSH alias or user@host. Default channel: default.' \
    'TCODEX_NOTIFY_SERVER_DIR can supply the absolute server project path.'
}

server_dir="${TCODEX_NOTIFY_SERVER_DIR:-}"
channel="${TCODEX_NOTIFY_CHANNEL:-default}"
destination=''
port=''
once=0
test_notification=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --port|--channel|--server-dir|--notifier)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      case "$1" in
        --port) port="$2" ;;
        --channel) channel="$2" ;;
        --server-dir) server_dir="$2" ;;
        --notifier)
          [ "$2" = 'osascript' ] || {
            printf '%s\n' 'Only osascript notifications are supported.' >&2
            exit 2
          }
          ;;
      esac
      shift 2 ;;
    --once) once=1; shift ;;
    --test-notification) test_notification=1; shift ;;
    -*) usage >&2; exit 2 ;;
    *)
      [ -z "$destination" ] || { usage >&2; exit 2; }
      destination="$1"; shift ;;
  esac
done
if [ "$test_notification" -eq 0 ] && [ -z "$destination" ]; then
  usage >&2
  exit 2
fi
if [ "$test_notification" -eq 0 ]; then
  case "$server_dir" in
    /*) ;;
    *)
      printf '%s\n' 'Set --server-dir or TCODEX_NOTIFY_SERVER_DIR to the absolute server project path.' >&2
      exit 2
      ;;
  esac
fi
[[ "$channel" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || {
  printf '%s\n' 'Invalid notification channel.' >&2; exit 2;
}
if [ -n "$port" ]; then
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || {
    printf '%s\n' 'Invalid SSH port.' >&2; exit 2;
  }
fi
if [ -n "${SSH_CONNECTION:-}" ] || [ -n "${SSH_TTY:-}" ]; then
  printf '%s\n' 'This listener must run on the Mac, not inside a server SSH session.' >&2
  exit 2
fi
command -v osascript >/dev/null 2>&1 || {
  printf '%s\n' 'osascript is unavailable. Run this listener locally on the Mac.' >&2
  exit 2
}

send_notification() {
  local message="$1"
  # Keep message data out of AppleScript source and event-stream stdin.
  osascript \
    -e 'on run argv' \
    -e 'display notification (item 1 of argv) with title "TCodex" sound name "Glass"' \
    -e 'end run' \
    "$message" < /dev/null > /dev/null
}

printf '%s\n' 'TCodex listener v6 (completion and question notifications).' >&2
if [ "$test_notification" -eq 1 ]; then
  printf '%s\n' 'Local notification in 5 seconds. No SSH connection is needed.' >&2
  sleep 5
  if send_notification 'TCodex local notification test'; then
    printf '%s\n' 'Local notification request sent via osascript. This does not confirm macOS displayed it.' >&2
    exit 0
  else
    printf '%s\n' 'Local notification command failed (osascript). Check the error above.' >&2
    exit 1
  fi
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
runtime_dir="$script_dir/../runtime"
if [ -L "$runtime_dir" ]; then
  printf '%s\n' 'Refusing a symlinked runtime directory.' >&2
  exit 2
fi
umask 077
mkdir -p "$runtime_dir"
work_dir="$(mktemp -d "$runtime_dir/listener.XXXXXXXX")"
fifo="$work_dir/events"
mkfifo "$fifo"
ssh_pid=''
cleanup() {
  if [ -n "$ssh_pid" ]; then
    kill "$ssh_pid" 2>/dev/null || :
    wait "$ssh_pid" 2>/dev/null || :
  fi
  rm -f "$fifo"
  rmdir "$work_dir" 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# SSH concatenates a remote command; quote paths for the remote POSIX shell.
shell_quote() {
  local escaped
  escaped="${1//\'/\'\\\'\'}"
  printf "'%s'" "$escaped"
}

ssh_options=(-n -T -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
if [ -n "$port" ]; then
  ssh_options+=(-p "$port")
fi
ready_pattern=$'^READY\t([0-9]{1,18})$'
event_pattern=$'^(EVENT|INPUT)\t([0-9]{1,18})\t([0-9]{1,12})\t([0-9a-f]{12})$'
cursor=''
delay=2
printf 'Listening for TCodex channel %s. Ctrl+C stops only this listener.\n' "$channel" >&2
while :; do
  remote_command="python3 -B $(shell_quote "$server_dir/server/stream-events.py") --channel $(shell_quote "$channel")"
  if [ -n "$cursor" ]; then
    remote_command="$remote_command --after $cursor"
  fi
  if [ "$once" -eq 1 ]; then
    remote_command="$remote_command --once"
  fi
  ssh "${ssh_options[@]}" "$destination" "$remote_command" > "$fifo" &
  ssh_pid=$!
  warned=0
  while IFS= read -r line; do
    if [[ "$line" =~ $ready_pattern ]]; then
      cursor="${BASH_REMATCH[1]}"
      delay=2
      printf 'Notification stream connected (cursor %s).\n' "$cursor" >&2
    elif [[ "$line" =~ $event_pattern ]]; then
      event_kind="${BASH_REMATCH[1]}"
      event_id="${BASH_REMATCH[2]}"
      session="${BASH_REMATCH[4]}"
      if [ -n "$cursor" ] && [ "$event_id" -gt "$cursor" ]; then
        printf 'Received event %s (session %s); notifying via osascript.\n' "$event_id" "$session" >&2
        if [ "$event_kind" = 'INPUT' ]; then
          message="TCodex is preparing a question (session $session)"
        else
          message="TCodex turn ended (session $session)"
        fi
        if ! send_notification "$message"; then
          printf 'Notification command failed for event %s; keeping the listener connected.\n' "$event_id" >&2
        fi
        cursor="$event_id"
      fi
    elif [ "$warned" -eq 0 ]; then
      printf '%s\n' 'Ignored unexpected remote output.' >&2
      warned=1
    fi
  done < "$fifo"
  status=0
  wait "$ssh_pid" || status=$?
  ssh_pid=''
  if [ "$once" -eq 1 ]; then
    exit "$status"
  fi
  printf 'SSH stream ended (status %s); retrying in %s seconds. Check SSH authentication if needed.\n' "$status" "$delay" >&2
  sleep "$delay"
  if [ "$delay" -lt 30 ]; then
    delay=$((delay * 2))
    [ "$delay" -le 30 ] || delay=30
  fi
done
