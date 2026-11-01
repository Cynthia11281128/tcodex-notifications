"""Isolated tests for TClaude hooks and the explicit dual-source listener."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
TMP = PROJECT.parents[1] / "tmp"
TMP.mkdir(exist_ok=True)
os.environ.update(TMPDIR=str(TMP), TMP=str(TMP), TEMP=str(TMP))
sys.path.insert(0, str(PROJECT / "server"))
spec = importlib.util.spec_from_file_location("tclaude_events", PROJECT / "server/tclaude-events.py")
events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(events)
from notify_queue import NotificationQueue


class TClaudeNotifications(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tclaude-notify-", dir=TMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        shutil.copytree(PROJECT / "server", self.app / "server")
        shutil.copytree(PROJECT / "mac", self.app / "mac")
        self.session = str(uuid.uuid4())
        self.prompt = str(uuid.uuid4())

    def payload(self, kind="Stop"):
        return {"hook_event_name": kind, "session_id": self.session, "prompt_id": self.prompt}

    def run_hook(self, payload):
        return subprocess.run(
            [sys.executable, "-B", str(self.app / "server/tclaude-events.py")],
            input=json.dumps(payload), text=True, capture_output=True, timeout=10,
        )

    def test_completion_dedup_privacy_and_cross_source(self):
        payload = self.payload()
        payload.update(last_assistant_message="SECRET", cwd="/private/path")
        self.assertEqual(self.run_hook(payload).returncode, 0)
        self.assertEqual(self.run_hook(payload).returncode, 0)
        queue = NotificationQueue(runtime_dir=self.app / "runtime")
        self.addCleanup(queue.close)
        self.assertEqual(len(queue.read_after(0)), 1)
        queue.enqueue({"type": "agent-turn-complete", "thread-id": self.session, "turn-id": self.prompt})
        self.assertEqual(len(queue.read_after(0)), 2)
        self.assertNotIn("SECRET", "".join(p.read_text() for p in queue.event_files()))

    def test_question_per_call(self):
        payload = self.payload("PreToolUse")
        payload.update(tool_name="AskUserQuestion", tool_use_id="call-1",
                       tool_input={"questions": [{"header": "Choice", "question": "SECRET"}]})
        first = events.normalize(payload)
        self.assertEqual(first["type"], "user-input-requested")
        self.run_hook(payload)
        self.run_hook(payload)
        payload["tool_use_id"] = "call-2"
        self.run_hook(payload)
        queue = NotificationQueue(runtime_dir=self.app / "runtime")
        self.addCleanup(queue.close)
        self.assertEqual(len(queue.read_after(0)), 2)

    def test_shapes_and_subagent_ignored(self):
        for payload in ([], {}, self.payload("SubagentStop"),
                        dict(self.payload(), agent_id="agent"), dict(self.payload(), prompt_id=None)):
            self.assertIsNone(events.normalize(payload))
        self.assertIsNone(events.normalize(dict(self.payload("PreToolUse"), tool_name="Bash")))

    def test_notification_failure_is_advisory(self):
        (self.app / "runtime").mkdir(mode=0o755)
        self.assertEqual(self.run_hook(self.payload()).returncode, 0)
        result = subprocess.run([sys.executable, "-B", str(self.app / "server/tclaude-events.py"),
                                 "--test", "complete"], capture_output=True)
        self.assertEqual(result.returncode, 1)

    def test_launcher_injects_native_hooks_and_preserves_args(self):
        mock = 'tclaude() { printf "%s\\n" "$@"; }; exec() { "$@"; }; source "$1" --resume abc'
        result = subprocess.run(["bash", "-c", mock, "_", str(self.app / "server/tclaude-notify.sh")],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "--settings")
        hooks = json.loads(lines[1])["hooks"]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "^AskUserQuestion$")
        self.assertTrue(hooks["StopFailure"][0]["hooks"][1]["asyncRewake"])
        self.assertEqual(lines[2:], ["--resume", "abc"])
        self.assertNotIn("PermissionRequest", hooks)

    def test_dual_sources_one_command(self):
        setup = r'''
osascript() { printf 'NOTICE:%s\n' "$@" >&2; }
ssh() {
  case "$*" in
    *"/codex/"*) printf 'READY\t0\nEVENT\t1\t1\tabcdef123456\nINPUT\t2\t1\tabcdef123456\n' ;;
    *"/claude/"*) printf 'READY\t0\nEVENT\t1\t1\tabcdef123456\nINPUT\t2\t1\tabcdef123456\n' ;;
    *) return 33 ;;
  esac
}
export -f ssh osascript
bash "$1" --tcodex-dir /codex --tclaude-dir /claude --channel taiji --once host
'''
        env = os.environ.copy()
        for key in ("SSH_CONNECTION", "SSH_TTY", "TCODEX_NOTIFY_SERVER_DIR",
                    "TCODEX_NOTIFY_TCODEX_DIR", "TCODEX_NOTIFY_TCLAUDE_DIR"):
            env.pop(key, None)
        result = subprocess.run(["bash", "-c", setup, "_", str(self.app / "mac/listen.sh")],
                                env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for source in ("TCodex", "TClaude"):
            self.assertIn(source + " turn ended", result.stderr)
            self.assertIn(source + " is preparing a question", result.stderr)
        self.assertIn('NOTICE:TClaude\n', result.stderr)
        self.assertFalse(list((self.app / "runtime").glob("listener.*")))

    def test_mixed_directory_arguments_rejected(self):
        result = subprocess.run(["bash", str(self.app / "mac/listen.sh"), "--server-dir", "/old",
                                 "--tclaude-dir", "/new", "host"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_one_source_failure_does_not_hide_other_source(self):
        setup = r'''
osascript() { printf 'NOTICE:%s\n' "$@" >&2; }
ssh() {
  case "$*" in
    *"/codex/"*) return 42 ;;
    *"/claude/"*) printf 'READY\t0\nEVENT\t1\t1\tabcdef123456\n' ;;
  esac
}
export -f ssh osascript
bash "$1" --tcodex-dir /codex --tclaude-dir /claude --once host
'''
        env = os.environ.copy()
        for key in ("SSH_CONNECTION", "SSH_TTY", "TCODEX_NOTIFY_SERVER_DIR",
                    "TCODEX_NOTIFY_TCODEX_DIR", "TCODEX_NOTIFY_TCLAUDE_DIR"):
            env.pop(key, None)
        result = subprocess.run(["bash", "-c", setup, "_", str(self.app / "mac/listen.sh")],
                                env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 42)
        self.assertIn("TClaude turn ended", result.stderr)

    def test_completion_key_is_stable_across_transcript_flush(self):
        payload = self.payload("StopFailure")
        payload["transcript_path"] = str(self.root / "transcript.jsonl")
        first = events.normalize(payload)
        (self.root / "transcript.jsonl").write_text('{"type":"assistant"}\n')
        self.assertEqual(events.normalize(payload), first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
