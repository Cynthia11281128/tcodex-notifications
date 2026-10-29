"""Run from the repository root with python3 -B tests/test_notifications.py."""

import concurrent.futures
import hashlib
import importlib.util
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parent.parent
TMP = PROJECT.parents[1] / "tmp"
TMP.mkdir(exist_ok=True)
os.environ.update(TMPDIR=str(TMP), TMP=str(TMP), TEMP=str(TMP))
spec = importlib.util.spec_from_file_location("notify_queue", PROJECT / "server/notify_queue.py")
queue_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_module)
NotificationQueue = queue_module.NotificationQueue


def event(turn="turn-1", thread="thread-1"):
    return {
        "type": "agent-turn-complete",
        "thread-id": thread,
        "turn-id": turn,
        "cwd": "/private/path",
        "input-messages": ["SECRET_PROMPT"],
        "last-assistant-message": "SECRET_RESPONSE",
    }


class NotificationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="notification-test-", dir=TMP)
        self.root = Path(self.temp.name)
        self.app = self.root / "tcodex-notification"
        shutil.copytree(PROJECT / "server", self.app / "server")
        shutil.copytree(PROJECT / "mac", self.app / "mac")
        self.runtime = self.app / "runtime"

    def tearDown(self):
        self.temp.cleanup()

    def queue(self, channel="default"):
        return NotificationQueue(channel, self.runtime)

    def python(self, script, *args):
        return subprocess.run(
            [sys.executable, "-B", str(self.app / "server" / script), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_queue_deduplicates_and_stores_no_private_payload(self):
        queue = self.queue()
        self.assertTrue(queue.enqueue(event()))
        self.assertFalse(queue.enqueue(event()))
        self.assertTrue(queue.enqueue(event("turn-2")))
        rows = queue.read_after(0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][2], hashlib.sha256(b"thread-1").hexdigest()[:12])
        queue.close()
        data = b"".join(path.read_bytes() for path in (self.runtime / "default").glob("*.json"))
        for secret in (b"SECRET_PROMPT", b"SECRET_RESPONSE", b"/private/path", b"thread-1"):
            self.assertNotIn(secret, data)
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
        for path in (self.runtime / "default").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_optional_fields_unknown_events_and_channels(self):
        queue = self.queue()
        self.assertFalse(queue.enqueue({"type": "approval-requested"}))
        self.assertFalse(queue.enqueue([]))
        self.assertTrue(queue.enqueue({"type": "agent-turn-complete"}))
        self.assertTrue(queue.enqueue({"type": "agent-turn-complete"}))
        other = self.queue("other")
        self.assertEqual(other.read_after(0), [])
        other.close()
        queue.close()
        with self.assertRaises(ValueError):
            self.queue("../../escape")

    def test_watcher_state_is_isolated_from_notification_channels(self):
        watcher_runtime = self.runtime / ".auto-continue"
        self.runtime.mkdir(mode=0o700)
        watcher_runtime.mkdir(mode=0o700)
        checkpoint = watcher_runtime / "checkpoint.json"
        checkpoint.write_text('{"offset": 123, "attempts": 2}\n')
        before = checkpoint.read_bytes()
        queue = self.queue()
        try:
            self.assertTrue(queue.enqueue(event()))
            queue.prune()
            self.assertEqual(len(queue.read_after(0)), 1)
        finally:
            queue.close()
        self.assertEqual(checkpoint.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.queue(".auto-continue")
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_retention_cap_and_monotonic_cursor(self):
        queue = self.queue()
        queue.enqueue(event("old"), now=int(time.time()) - 90000)
        self.assertEqual(queue.read_after(0), [])
        now = int(time.time())
        for i in range(1010):
            queue.atomic_write(
                queue.path / ("{:020d}-{:012d}-{}.json".format(i + 2, now, hashlib.sha256(str(i).encode()).hexdigest())),
                json.dumps([i + 2, now, "0123456789ab"]),
            )
        queue.prune(now)
        self.assertEqual(len(queue.event_files()), 1000)
        watermark = queue.high_water()
        queue.prune(now + 90000)
        self.assertEqual(queue.high_water(), watermark)
        queue.enqueue(event("new"), now=now + 90000)
        self.assertGreater(queue.read_after(watermark, now=now + 90000)[0][0], watermark)
        queue.close()

    def test_concurrent_writers(self):
        queue = self.queue()
        queue.close()

        def insert(index):
            writer = self.queue()
            try:
                return writer.enqueue(event("turn-" + str(index)))
            finally:
                writer.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(insert, range(40))))
        queue = self.queue()
        self.assertEqual(len(queue.read_after(0)), 40)
        queue.close()

    def test_refuses_symlinks_and_unsafe_permissions(self):
        target = self.root / "elsewhere"
        target.mkdir(mode=0o700)
        self.runtime.symlink_to(target)
        with self.assertRaises(ValueError):
            self.queue()
        self.runtime.unlink()
        self.runtime.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            self.queue()
        self.runtime.chmod(0o700)
        channel = self.runtime / "default"
        channel.mkdir(mode=0o700)
        db = channel / ".lock"
        target_file = target / "db"
        target_file.touch(mode=0o600)
        db.symlink_to(target_file)
        with self.assertRaises((ValueError, OSError)):
            self.queue()
        db.unlink()
        db.touch(mode=0o644)
        with self.assertRaises(ValueError):
            self.queue()

    def test_hook_failure_is_nonfatal_and_test_is_explicit(self):
        unknown = self.python("notify-event.py", json.dumps({"type": "unknown"}))
        self.assertEqual(unknown.returncode, 0)
        self.assertFalse(self.runtime.exists())
        malformed = self.python("notify-event.py", "{SECRET_BAD_JSON")
        self.assertEqual(malformed.returncode, 0)
        self.assertNotIn("SECRET_BAD_JSON", malformed.stderr)
        result = self.python("notify-event.py", json.dumps(event()))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.python("notify-event.py", "--test").returncode, 0)
        self.runtime.chmod(0o755)
        self.assertEqual(self.python("notify-event.py", json.dumps(event("later"))).returncode, 0)
        self.assertEqual(self.python("notify-event.py", "--test").returncode, 1)

    def test_stream_skips_initial_history_and_replays_after_cursor(self):
        self.python("notify-event.py", json.dumps(event()))
        initial = self.python("stream-events.py", "--once")
        self.assertEqual(initial.stdout, "READY\t1\n")
        self.python("notify-event.py", json.dumps(event("turn-2")))
        replay = self.python("stream-events.py", "--once", "--after", "1")
        self.assertTrue(replay.stdout.startswith("READY\t1\nEVENT\t2\t"))
        self.assertEqual(replay.stdout.count("EVENT"), 1)
        reset = self.python("stream-events.py", "--once", "--after", "99999")
        self.assertEqual(reset.stdout, "READY\t2\n")

    def test_hook_to_live_stream(self):
        process = subprocess.Popen(
            [sys.executable, "-B", str(self.app / "server/stream-events.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            self.assertEqual(process.stdout.readline(), "READY\t0\n")
            self.assertEqual(self.python("notify-event.py", "--test").returncode, 0)
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            self.assertTrue(process.stdout.readline().startswith("EVENT\t1\t"))
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_launcher_passes_notify_command_without_running_codex(self):
        launcher = self.app / "server/tcodex-notify.sh"
        mock = (
            'tcodex() { printf "%s\\0" "$@"; return 23; }; '
            'exec() { "$@"; }; launcher=$1; shift; source "$launcher" "$@"'
        )
        result = subprocess.run(
            ["bash", "-c", mock, "test", str(launcher), "resume", "--last", "space in prompt"],
            capture_output=True, timeout=10,
            env={**os.environ, "TCODEX_NOTIFY_CHANNEL": "test-channel"},
        )
        args = result.stdout.decode().split("\0")[:-1]
        self.assertEqual(result.returncode, 23)
        self.assertEqual(args[:3], ["-c", "tui.notifications=false", "-c"])
        command = json.loads(args[3].removeprefix("notify="))
        self.assertEqual(command[1:], [str(self.app / "server/notify-event.py"), "--channel", "test-channel"])
        self.assertEqual(args[4:], ["resume", "--last", "space in prompt"])
        self.assertFalse(self.runtime.exists())

    def run_listener(self, fake_ssh, *args, timeout=8, shell_setup=""):
        master, slave = pty.openpty()
        env = dict(os.environ)
        env.pop("SSH_CONNECTION", None)
        env.pop("SSH_TTY", None)
        env.pop("TCODEX_NOTIFY_CHANNEL", None)
        env["TCODEX_NOTIFY_SERVER_DIR"] = str(self.app)
        env["BASH_ENV"] = "/dev/null"
        native_mock = (
            'osascript() { printf "NATIVE_ARGUMENT:%s\\n" "$@" >&2; cat >/dev/null; }; '
            'export -f osascript; '
        )
        shell = fake_ssh + '; export -f ssh; ' + native_mock + shell_setup + 'exec bash "$@"'
        process = subprocess.Popen(
            ["bash", "-c", shell, "test", str(self.app / "mac/listen.sh"), *args],
            stdin=subprocess.DEVNULL, stdout=slave, stderr=subprocess.PIPE, env=env,
        )
        os.close(slave)
        output = b""
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
            process.wait(timeout=2)
            while select.select([master], [], [], 0)[0]:
                try:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        break
                    output += chunk
                except OSError:
                    break
            stderr = process.stderr.read().decode()
            return process.returncode, output, stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stderr.close()
            os.close(master)

    def test_native_local_notification_passes_data_without_ssh_or_runtime(self):
        code, output, stderr = self.run_listener(
            "ssh() { printf 'SSH_MUST_NOT_RUN'; return 99; }",
            "--test-notification",
            timeout=10,
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, b"")
        self.assertIn("TCodex listener v5 (macOS native notifications)", stderr)
        self.assertIn("NATIVE_ARGUMENT:on run argv", stderr)
        self.assertIn('NATIVE_ARGUMENT:display notification (item 1 of argv) with title "TCodex" sound name "Glass"', stderr)
        self.assertIn("NATIVE_ARGUMENT:TCodex local notification test", stderr)
        self.assertIn("Local notification request sent via osascript", stderr)
        self.assertNotIn("SSH_MUST_NOT_RUN", stderr)
        self.assertFalse(self.runtime.exists())

    def test_existing_explicit_native_notifier_command_remains_supported(self):
        code, output, stderr = self.run_listener(
            "ssh() { printf 'SSH_MUST_NOT_RUN'; return 99; }",
            "--notifier", "osascript", "--test-notification",
            shell_setup='sleep() { :; }; export -f sleep; ',
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, b"")
        self.assertIn("NATIVE_ARGUMENT:TCodex local notification test", stderr)
        self.assertNotIn("SSH_MUST_NOT_RUN", stderr)
        self.assertFalse(self.runtime.exists())

    def test_native_listener_preserves_stream_filters_duplicates_and_untrusted_output(self):
        fake = (
            "ssh() { printf 'READY\\t0\\n"
            "EVENT\\t1\\t12345\\t0123456789ab\\n"
            "EVENT\\t1\\t12345\\t0123456789ab\\n"
            "\\033]9;UNTRUSTED\\007\\n"
            "EVENT\\t2\\t12345\\tabcdef012345\\n'; }"
        )
        code, output, stderr = self.run_listener(
            fake, "--once", "--port", "2222", "server",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, b"")
        self.assertEqual(stderr.count("NATIVE_ARGUMENT:TCodex turn ended"), 2)
        self.assertIn("NATIVE_ARGUMENT:TCodex turn ended (session 0123456789ab)", stderr)
        self.assertIn("NATIVE_ARGUMENT:TCodex turn ended (session abcdef012345)", stderr)
        self.assertEqual(stderr.count("Received event "), 2)
        self.assertNotIn("UNTRUSTED", stderr)
        self.assertIn("Ignored unexpected remote output", stderr)
        self.assertEqual(list(self.runtime.glob("listener.*")), [])

    def test_native_notification_failure_does_not_stop_stream(self):
        fake = (
            "ssh() { printf 'READY\\t0\\n"
            "EVENT\\t1\\t12345\\t0123456789ab\\n"
            "EVENT\\t2\\t12345\\tabcdef012345\\n'; }"
        )
        setup = 'osascript() { return 42; }; export -f osascript; '
        code, output, stderr = self.run_listener(
            fake, "--once", "server",
            shell_setup=setup,
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, b"")
        self.assertEqual(stderr.count("Notification command failed for event "), 2)
        self.assertEqual(stderr.count("Received event "), 2)
        self.assertEqual(list(self.runtime.glob("listener.*")), [])

    def test_native_local_notification_failure_is_explicit(self):
        setup = (
            'osascript() { return 42; }; export -f osascript; '
            'sleep() { :; }; export -f sleep; '
        )
        code, output, stderr = self.run_listener(
            "ssh() { return 99; }", "--test-notification",
            shell_setup=setup,
        )
        self.assertEqual(code, 1, stderr)
        self.assertEqual(output, b"")
        self.assertIn("Local notification command failed (osascript)", stderr)
        self.assertNotIn("Local notification request sent", stderr)
        self.assertFalse(self.runtime.exists())

    def test_listener_rejects_unknown_notifier_before_ssh_or_runtime(self):
        code, output, stderr = self.run_listener(
            "ssh() { printf 'SSH_MUST_NOT_RUN'; return 99; }",
            "--notifier", "invalid", "--test-notification",
        )
        self.assertEqual(code, 2, stderr)
        self.assertEqual(output, b"")
        self.assertIn("Only osascript notifications are supported", stderr)
        self.assertFalse(self.runtime.exists())

    def test_listener_requires_local_native_notifier(self):
        setup = (
            'command() { if [ "$*" = "-v osascript" ]; then return 1; fi; builtin command "$@"; }; '
            'export -f command; '
        )
        code, output, stderr = self.run_listener(
            "ssh() { printf 'SSH_MUST_NOT_RUN'; return 99; }",
            "--test-notification",
            shell_setup=setup,
        )
        self.assertEqual(code, 2, stderr)
        self.assertEqual(output, b"")
        self.assertIn("osascript is unavailable", stderr)
        self.assertNotIn("SSH_MUST_NOT_RUN", stderr)
        self.assertFalse(self.runtime.exists())

    def test_listener_refuses_server_ssh_session(self):
        code, output, stderr = self.run_listener(
            "ssh() { printf 'SSH_MUST_NOT_RUN'; return 99; }",
            "--test-notification",
            shell_setup='export SSH_CONNECTION="test-server-session"; ',
        )
        self.assertEqual(code, 2, stderr)
        self.assertEqual(output, b"")
        self.assertIn("must run on the Mac", stderr)
        self.assertNotIn("NATIVE_ARGUMENT:", stderr)
        self.assertFalse(self.runtime.exists())

    def test_listener_requires_server_path_but_local_test_does_not(self):
        setup = 'unset TCODEX_NOTIFY_SERVER_DIR; '
        code, output, stderr = self.run_listener(
            "ssh() { printf 'SSH_MUST_NOT_RUN'; return 99; }",
            "--once", "server", shell_setup=setup,
        )
        self.assertEqual(code, 2, stderr)
        self.assertEqual(output, b"")
        self.assertIn("Set --server-dir or TCODEX_NOTIFY_SERVER_DIR", stderr)
        self.assertFalse(self.runtime.exists())
        code, output, stderr = self.run_listener(
            "ssh() { printf 'SSH_MUST_NOT_RUN'; return 99; }",
            "--test-notification",
            shell_setup=setup + 'sleep() { :; }; export -f sleep; ',
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, b"")
        self.assertIn("Local notification request sent", stderr)
        self.assertFalse(self.runtime.exists())

    def test_listener_environment_configuration_and_cli_override(self):
        fake = 'ssh() { printf "SSH_ARGUMENT:%s\\n" "$@" >&2; printf "READY\\t0\\n"; }'
        code, output, stderr = self.run_listener(
            fake, "--once", "server",
            shell_setup='export TCODEX_NOTIFY_CHANNEL="env-channel"; ',
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, b"")
        self.assertIn(str(self.app / "server/stream-events.py"), stderr)
        self.assertIn("--channel 'env-channel'", stderr)
        code, output, stderr = self.run_listener(
            fake, "--once", "--server-dir", "/opt/notification-project",
            "--channel", "cli-channel", "server",
            shell_setup='export TCODEX_NOTIFY_CHANNEL="env-channel"; ',
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, b"")
        self.assertIn("/opt/notification-project/server/stream-events.py", stderr)
        self.assertIn("--channel 'cli-channel'", stderr)
        self.assertNotIn("--channel 'env-channel'", stderr)

    def test_listener_rejects_relative_server_path(self):
        code, output, stderr = self.run_listener(
            "ssh() { printf 'SSH_MUST_NOT_RUN'; return 99; }",
            "--server-dir", "relative/project", "--once", "server",
        )
        self.assertEqual(code, 2, stderr)
        self.assertEqual(output, b"")
        self.assertIn("absolute server project path", stderr)
        self.assertFalse(self.runtime.exists())

    def test_listener_remote_quoting_and_end_to_end_event(self):
        # A quote and space in the remote path must remain data, not shell syntax.
        quoted = self.root / "a 'quoted path"
        self.app.rename(quoted)
        self.app = quoted
        self.runtime = quoted / "runtime"
        self.python("notify-event.py", json.dumps(event("old")))
        fake = (
            'ssh() { command="${@: -1}"; '
            'case "$command" in *"--port"*) return 99;; esac; '
            'bash -c "$command"; }'
        )
        code, output, stderr = self.run_listener(
            fake, "--once", "--server-dir", str(quoted), "server"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, b"")
        self.assertNotIn("NATIVE_ARGUMENT:", stderr)

    def test_listener_reconnects_with_cursor(self):
        fake = (
            "ssh() { command=\"${@: -1}\"; "
            "case \"$command\" in "
            "*'--after 1'*) printf 'READY\\t1\\nEVENT\\t1\\t123\\t0123456789ab\\n"
            "EVENT\\t2\\t123\\tabcdef012345\\n'; "
            "sleep 0.1; kill -TERM \"$$\";; "
            "*) printf 'READY\\t0\\nEVENT\\t1\\t123\\t0123456789ab\\n'; return 255;; "
            "esac; }"
        )
        code, output, stderr = self.run_listener(fake, "server")
        self.assertEqual(code, 143, stderr)
        self.assertEqual(output, b"")
        self.assertEqual(stderr.count("NATIVE_ARGUMENT:TCodex turn ended"), 2)
        self.assertIn("retrying", stderr)


if __name__ == "__main__":
    unittest.main()
