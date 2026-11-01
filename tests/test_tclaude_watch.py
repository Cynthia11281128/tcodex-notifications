"""Fail-closed registry, final error and native dispatch tests."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
TMP = PROJECT.parents[1] / "tmp"
TMP.mkdir(exist_ok=True)
os.environ.update(TMPDIR=str(TMP), TMP=str(TMP), TEMP=str(TMP))
sys.path.insert(0, str(PROJECT / "server"))
import tclaude_retry as retry
spec = importlib.util.spec_from_file_location("tclaude_watch", PROJECT / "server/watch-tclaude-retries.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class TClaudeWatch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tclaude-watch-", dir=TMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "runtime/.tclaude-auto-continue"
        self.sid = str(uuid.uuid4())
        self.prompt = str(uuid.uuid4())
        self.failure = str(uuid.uuid4())
        self.owner = {"pid": 123, "start": "99", "executable": "/native/claude-code-2.1.295/claude"}
        self.transcript = Path(self.temp.name) / "session.jsonl"
        self.row = {"type": "assistant", "sessionId": self.sid, "uuid": self.failure,
                    "isApiErrorMessage": True, "apiErrorStatus": 429}
        self.transcript.write_text(json.dumps(self.row) + "\n")
        retry.lifecycle({"session_id": self.sid, "hook_event_name": "SessionStart"}, self.root, self.owner)
        retry.lifecycle({"session_id": self.sid, "hook_event_name": "UserPromptSubmit",
                         "prompt_id": self.prompt}, self.root, self.owner)
        self.args = argparse.Namespace(runtime_dir=self.root, tmp_dir=TMP, session=None,
                                       initial_delay=3, max_delay=3, max_retries=5,
                                       observe_timeout=30, dry_run=False, once=False)
        self.watcher = watch.Watcher(self.args)
        self.addCleanup(self.watcher.selector.close)
        self.connection, self.peer = socket.socketpair()
        self.addCleanup(self.peer.close)
        self.addCleanup(self.connection.close)
        self.watcher.clients[self.connection] = {"raw": bytearray()}
        self.request = {"session": self.sid, "failure": self.failure, "prompt": self.prompt,
                        "owner": self.owner, "transcript": str(self.transcript)}

    def test_strict_final_structured_429(self):
        payload = watch.failure_payload(self.request)
        self.assertEqual(retry.final_429(payload), self.failure)
        for value in (None, "429", True, 503):
            self.row["apiErrorStatus"] = value
            self.transcript.write_text(json.dumps(self.row) + "\n")
            self.assertIsNone(retry.final_429(payload))
        self.transcript.write_text(json.dumps(dict(self.row, apiErrorStatus=429)) + "\n"
                                   + json.dumps({"type": "user", "sessionId": self.sid}) + "\n")
        self.assertIsNone(retry.final_429(payload))

    def test_cooldown_dispatch_reserved_before_reply(self):
        with mock.patch.object(watch, "owner_info", return_value=self.owner):
            self.assertTrue(self.watcher.register(self.connection, self.request, 100))
            self.watcher.dispatch(self.connection, self.request, 102)
            self.assertIn(self.connection, self.watcher.clients)
            self.watcher.dispatch(self.connection, self.request, 103)
        self.assertEqual(json.loads(self.peer.recv(4096)), {"approve": self.failure})
        with retry.state(self.root, self.sid) as box:
            self.assertEqual(box[0]["attempts"], 1)
            self.assertEqual(box[0]["job"]["phase"], "dispatching")

    def test_manual_prompt_cancels(self):
        with mock.patch.object(watch, "owner_info", return_value=self.owner):
            self.watcher.register(self.connection, self.request, 100)
            retry.lifecycle({"session_id": self.sid, "hook_event_name": "UserPromptSubmit",
                             "prompt_id": str(uuid.uuid4())}, self.root, self.owner)
            self.watcher.dispatch(self.connection, self.request, 103)
        self.assertEqual(json.loads(self.peer.recv(4096)), {"deny": True})

    def test_duplicate_cap_and_process_replacement(self):
        with retry.state(self.root, self.sid) as box:
            box[0]["attempts"] = 5
        with mock.patch.object(watch, "owner_info", return_value=self.owner):
            self.assertFalse(self.watcher.register(self.connection, self.request, 100))
        with retry.state(self.root, self.sid) as box:
            self.assertTrue(box[0]["disabled"])
        with mock.patch.object(watch, "owner_info", return_value=None):
            self.assertFalse(self.watcher.register(self.connection, self.request, 100))

    def test_crash_reservation_never_replayed(self):
        with mock.patch.object(watch, "owner_info", return_value=self.owner):
            self.watcher.register(self.connection, self.request, 100)
            self.watcher.dispatch(self.connection, self.request, 103)
            self.watcher.inventory(134)
        with retry.state(self.root, self.sid) as box:
            self.assertTrue(box[0]["disabled"])
            self.assertIsNone(box[0]["job"])

    def test_dry_run_does_not_touch_live_budget(self):
        self.args.dry_run = True
        with mock.patch.object(watch, "owner_info", return_value=self.owner):
            self.assertFalse(self.watcher.register(self.connection, self.request, 100))
        with retry.state(self.root, self.sid) as box:
            self.assertEqual(box[0]["attempts"], 0)
            self.assertEqual(box[0]["seen"], [])
        self.assertTrue((self.root / "dry-run" / (self.sid + ".json")).exists())

    def test_no_watcher_no_rewake(self):
        payload = dict(watch.failure_payload(self.request), prompt_id=self.prompt)
        self.assertEqual(retry.failure_hook(payload, self.root, TMP, self.owner), 0)

    def test_native_prompt_preserves_budget_manual_prompt_resets(self):
        with retry.state(self.root, self.sid) as box:
            box[0].update(attempts=2, job={
                "phase": "awaiting", "failure": self.failure, "due": time.time() + 30,
                "transcript": str(self.transcript), "observe_timeout": 30, "token": "unique",
            })
        new_prompt = str(uuid.uuid4())
        retry.lifecycle({
            "session_id": self.sid, "hook_event_name": "UserPromptSubmit",
            "prompt_id": new_prompt, "prompt": retry.FEEDBACK + " [tclaude-retry:unique]",
        }, self.root, self.owner)
        with retry.state(self.root, self.sid) as box:
            self.assertEqual(box[0]["attempts"], 2)
            self.assertEqual(box[0]["prompt"], new_prompt)
            self.assertIsNone(box[0]["job"])
        retry.lifecycle({
            "session_id": self.sid, "hook_event_name": "UserPromptSubmit",
            "prompt_id": str(uuid.uuid4()), "prompt": "new task",
        }, self.root, self.owner)
        with retry.state(self.root, self.sid) as box:
            self.assertEqual(box[0]["attempts"], 0)

    def test_partial_rollout_never_matches(self):
        self.transcript.write_text(json.dumps(self.row))
        self.assertIsNone(retry.final_429(watch.failure_payload(self.request)))

    def test_selected_session_excludes_other_sessions(self):
        self.watcher.selected = {str(uuid.uuid4())}
        self.assertFalse(self.watcher.register(self.connection, self.request, 100))

    def test_unverified_runtime_refused(self):
        self.request["owner"] = dict(self.owner, executable="/native/claude-code-new/claude")
        self.assertFalse(self.watcher.register(self.connection, self.request, 100))

    def test_unobserved_wake_disables_retry(self):
        with retry.state(self.root, self.sid) as box:
            box[0]["job"] = {
                "phase": "awaiting", "failure": self.failure, "due": 100,
                "transcript": str(self.transcript), "observe_timeout": 30,
            }
        with mock.patch.object(watch, "owner_info", return_value=self.owner):
            self.watcher.inventory(101)
        with retry.state(self.root, self.sid) as box:
            self.assertTrue(box[0]["disabled"])

    def test_checkpoint_invalid_refused(self):
        (self.root / (self.sid + ".json")).write_text('{"version": 2}\n')
        with self.assertRaises(ValueError):
            with retry.state(self.root, self.sid):
                pass

    def test_parser_explicit_scope_defaults(self):
        args = watch.parser().parse_args(["--all-registered"])
        self.assertEqual((args.initial_delay, args.max_delay, args.max_retries), (3, 3, 5))
        with self.assertRaises(SystemExit):
            watch.parser().parse_args([])

    def test_real_socket_lock_and_shutdown_denies_pending_client(self):
        command = [
            sys.executable, "-B", str(PROJECT / "server/watch-tclaude-retries.py"),
            "--all-registered", "--runtime-dir", str(self.root), "--tmp-dir", str(TMP),
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        address = retry.socket_path(self.root, TMP)
        try:
            deadline = time.monotonic() + 5
            while not address.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(address.exists())
            duplicate = subprocess.run(command + ["--once"], capture_output=True, timeout=5)
            self.assertEqual(duplicate.returncode, 1)
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(5)
                connection.connect(str(address))
                time.sleep(0.2)
                process.terminate()
                self.assertEqual(json.loads(connection.recv(4096)), {"deny": True})
            process.communicate(timeout=5)
            self.assertFalse(address.exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
