"""Opt-in native PTY test using only an isolated config and local mock API."""

import http.server
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
TMP = PROJECT.parents[1] / "tmp"
TMP.mkdir(exist_ok=True)
os.environ.update(TMPDIR=str(TMP), TMP=str(TMP), TEMP=str(TMP))
RUNTIME = os.environ.get("TCLAUDE_TEST_RUNTIME")


@unittest.skipUnless(RUNTIME, "Set TCLAUDE_TEST_RUNTIME to the verified native claude executable.")
class NativeRuntime(unittest.TestCase):
    def test_two_native_continues_then_cap(self):
        with tempfile.TemporaryDirectory(prefix="tclaude-native-", dir=TMP) as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            (home / ".claude.json").write_text(json.dumps({
                "hasCompletedOnboarding": True, "lastOnboardingVersion": "2.1.295",
                "customApiKeyResponses": {"approved": ["isolated-test-key"], "rejected": []},
                "projects": {str(root): {"hasTrustDialogAccepted": True}},
            }))
            counts = []

            class API(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    counts.append(self.path)
                    body = json.dumps({"type": "error", "error": {
                        "type": "rate_limit_error", "message": "isolated test"
                    }}).encode()
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), API)
            thread = threading.Thread(target=api.serve_forever, daemon=True)
            thread.start()
            env = os.environ.copy()
            for key in list(env):
                if key.startswith(("ANTHROPIC_", "CLAUDE_", "TCLAUDE_")) or key.lower().endswith("proxy"):
                    env.pop(key)
            runtime_dir = root / "runtime/.tclaude-auto-continue"
            env.update(
                CLAUDE_CONFIG_DIR=str(home), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{api.server_port}",
                ANTHROPIC_API_KEY="isolated-test-key", CLAUDE_CODE_MAX_RETRIES="0",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                TCLAUDE_NOTIFICATION_RUNTIME=str(runtime_dir),
                TMPDIR=str(TMP), TMP=str(TMP), TEMP=str(TMP),
            )
            settings = {"hooks": {}}
            import shlex
            command = shlex.join([sys.executable, "-B", str(PROJECT / "server/tclaude_retry.py")])
            for name in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "StopFailure"):
                hook = {"type": "command", "command": command, "timeout": 60}
                if name == "StopFailure":
                    hook["asyncRewake"] = True
                settings["hooks"][name] = [{"hooks": [hook]}]
            watcher = subprocess.Popen([
                sys.executable, "-B", str(PROJECT / "server/watch-tclaude-retries.py"),
                "--all-registered", "--runtime-dir", str(runtime_dir), "--tmp-dir", str(TMP),
                "--initial-delay", "1", "--max-delay", "1", "--max-retries", "2",
            ], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            master, slave = pty.openpty()
            process = subprocess.Popen([
                RUNTIME, "--settings", json.dumps(settings), "--setting-sources", "",
                "--permission-mode", "dontAsk", "--model", "claude-sonnet-4-5",
            ], cwd=root, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            checkpoint = None
            try:
                started = time.monotonic()
                sent = False
                while time.monotonic() - started < 45:
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        os.read(master, 65536)
                    if not sent and time.monotonic() - started >= 8:
                        os.write(master, b"\r")
                        time.sleep(0.5)
                        os.write(master, b"Reply with OK\r")
                        sent = True
                    for path in runtime_dir.glob("*.json"):
                        checkpoint = json.loads(path.read_text())
                    if checkpoint and checkpoint["disabled"] and checkpoint["attempts"] == 2:
                        break
                self.assertIsNotNone(checkpoint)
                self.assertEqual(checkpoint["attempts"], 2, checkpoint)
                self.assertTrue(checkpoint["disabled"], checkpoint)
                self.assertEqual(len(checkpoint["seen"]), 3)
                self.assertEqual(process.poll(), None)
                request_count = len(counts)
                for _ in range(10):
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        os.read(master, 65536)
                self.assertEqual(len(counts), request_count)
                self.assertGreaterEqual(request_count, 3)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                os.close(master)
                watcher.terminate()
                watcher.communicate(timeout=5)
                api.shutdown()
                api.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
