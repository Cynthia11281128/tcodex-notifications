"""Local notifications; subprocess mocks never send desktop notifications."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("local_notify", PROJECT / "mac/notify-event.py")
local_notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_notify)


class LocalHookTest(unittest.TestCase):
    def invoke(self, *args, error=None, platform="darwin"):
        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(sys, "argv", ["notify-event.py", *args]),
            patch.object(sys, "platform", platform),
            patch.object(local_notify.subprocess, "run", side_effect=error) as run,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = local_notify.main()
        return code, stdout.getvalue(), stderr.getvalue(), run

    def test_complete_event_uses_fixed_private_message_and_isolated_io(self):
        payload = {
            "type": "agent-turn-complete",
            "cwd": "/SECRET_PATH",
            "input-messages": ['SECRET_PROMPT " & do shell script "bad"'],
            "last-assistant-message": "SECRET_RESPONSE",
        }
        code, stdout, stderr, run = self.invoke(json.dumps(payload))
        self.assertEqual((code, stdout, stderr), (0, "", ""))
        run.assert_called_once_with(
            [
                "osascript",
                "-e", "on run argv",
                "-e", 'display notification (item 1 of argv) with title "TCodex" sound name "Glass"',
                "-e", "end run",
                "TCodex turn ended (local)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )

    def test_optional_fields_and_repeated_events_each_notify(self):
        for _ in range(2):
            code, _, _, run = self.invoke('{"type":"agent-turn-complete"}')
            self.assertEqual(code, 0)
            run.assert_called_once()

    def test_unknown_and_non_object_events_do_not_notify(self):
        for raw in ('{"type":"approval-requested"}', "[]", "null", "1", "{}"):
            with self.subTest(raw=raw):
                code, stdout, stderr, run = self.invoke(raw)
                self.assertEqual((code, stdout, stderr), (0, "", ""))
                run.assert_not_called()
        code, _, _, run = self.invoke()
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_bad_json_is_nonfatal_without_logging_payload(self):
        code, stdout, stderr, run = self.invoke("{SECRET_BAD_JSON")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("notification failed", stderr)
        self.assertNotIn("SECRET", stderr)
        run.assert_not_called()

    def test_notification_failures_are_nonfatal_except_explicit_test(self):
        errors = [
            FileNotFoundError("SECRET"),
            subprocess.CalledProcessError(42, "SECRET"),
            subprocess.TimeoutExpired("SECRET", 10),
        ]
        for error in errors:
            for testing in (False, True):
                with self.subTest(error=type(error).__name__, testing=testing):
                    args = ["--test"] if testing else ['{"type":"agent-turn-complete"}']
                    code, stdout, stderr, run = self.invoke(*args, error=error)
                    self.assertEqual(code, 1 if testing else 0)
                    self.assertEqual(stdout, "")
                    self.assertIn("notification failed", stderr)
                    self.assertNotIn("SECRET", stderr)
                    run.assert_called_once()

    def test_explicit_test_notification(self):
        code, stdout, stderr, run = self.invoke("--test")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("does not confirm macOS displayed it", stdout)
        self.assertEqual(run.call_args.args[0][-1], "TCodex local notification test")

    def test_non_macos_does_not_call_notifier(self):
        for args, expected in ((["--test"], 1), (['{"type":"agent-turn-complete"}'], 0)):
            code, _, stderr, run = self.invoke(*args, platform="linux")
            self.assertEqual(code, expected)
            self.assertIn("notification failed", stderr)
            run.assert_not_called()


class LocalLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tmp-", dir=PROJECT / "tests")
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / "a 'quoted path"
        shutil.copytree(PROJECT / "mac", self.app / "mac")

    def tearDown(self):
        self.temp.cleanup()

    def launch(self, *args, setup=""):
        shell = (
            'uname() { printf "Darwin\\n"; }; '
            'osascript() { return 99; }; '
            'tcodex() { printf "%s\\0" "$PWD" "$@"; return 23; }; '
            'exec() { "$@"; }; '
            + setup
            + 'launcher=$1; shift; source "$launcher" "$@"'
        )
        return subprocess.run(
            ["bash", "-c", shell, "test", str(self.app / "mac/tcodex-notify.sh"), *args],
            cwd=self.root,
            env={**os.environ, "BASH_ENV": "/dev/null"},
            capture_output=True,
            timeout=10,
        )

    def test_launcher_preserves_cwd_arguments_and_exit_code_with_quoted_path(self):
        before = sorted(self.root.rglob("*"))
        result = self.launch("resume", "--last", "space in prompt", "a 'quote")
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        cwd, *args = result.stdout.decode().split("\0")[:-1]
        self.assertEqual(cwd, str(self.root))
        self.assertEqual(args[:3], ["-c", "tui.notifications=false", "-c"])
        command = json.loads(args[3].removeprefix("notify="))
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(command[1:], ["-B", str(self.app / "mac/notify-event.py")])
        self.assertEqual(args[4:], ["resume", "--last", "space in prompt", "a 'quote"])
        self.assertEqual(sorted(self.root.rglob("*")), before)
        self.assertIn(b"local notifications enabled", result.stderr)

    def test_launcher_rejects_non_macos(self):
        result = self.launch(setup='uname() { printf "Linux\\n"; }; ')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"require macOS", result.stderr)

    def test_launcher_checks_each_dependency(self):
        for dependency in ("tcodex", "python3", "osascript"):
            with self.subTest(dependency=dependency):
                setup = (
                    'command() { if [ "$*" = "-v ' + dependency + '" ]; '
                    'then return 1; fi; builtin command "$@"; }; '
                )
                result = self.launch(setup=setup)
                self.assertEqual(result.returncode, 127)
                self.assertEqual(result.stdout, b"")
                self.assertIn(("require " + dependency + " on PATH").encode(), result.stderr)


if __name__ == "__main__":
    unittest.main()
