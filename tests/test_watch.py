"""Regression tests; all test-generated files stay in the workspace tmp."""

import argparse
from contextlib import closing
from dataclasses import asdict
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid


sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
TMP = PROJECT.parents[1] / "tmp"
TMP.mkdir(exist_ok=True)
os.environ.update(TMPDIR=str(TMP), TMP=str(TMP), TEMP=str(TMP))
WATCHER = PROJECT / "server/watch-retries.py"
spec = importlib.util.spec_from_file_location("auto_continue", WATCHER)
watch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watch
spec.loader.exec_module(watch)


def uid():
    return str(uuid.uuid4())


def event(kind, turn, **fields):
    return {"type": "event_msg", "payload": {
        "type": kind, "turn_id": turn, **fields,
    }}


def failure(turn, status=429):
    return event("task_complete", turn, error={"codex_error_info": {
        "response_too_many_failed_attempts": {"http_status_code": status},
    }})


class FakeGuards:
    def __init__(self):
        self.current_owner = watch.Owner(123, "456", "/test/runtime/codex")
        self.current_history = "failed"
        self.empty = True

    def owner(self, _thread):
        return self.current_owner

    def history(self, _thread, _turn):
        return self.current_history

    def queue_empty(self, _thread):
        return self.empty


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="auto-continue-test-", dir=TMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.rollouts = self.home / "sessions/2026/10/29"
        self.rollouts.mkdir(parents=True)
        self.thread = uid()
        self.metadata = {"id": self.thread, "source": "cli"}
        self.path = self.rollouts / ("rollout-test-" + self.thread + ".jsonl")
        self.path.write_text(json.dumps({
            "type": "session_meta", "payload": self.metadata,
        }) + "\n")
        self.args = argparse.Namespace(
            codex_home=self.home, initial_delay=60, max_delay=300,
            max_retries=5, dry_run=False, tmp_dir=TMP, command_timeout=15,
            observe_timeout=30,
        )
        self.guards = FakeGuards()
        self.sent = []
        self.store = self.open_store()
        self.session = watch.Session(
            self.path, self.metadata, self.store, self.args, self.guards, self.sender
        )

    def open_store(self):
        store = watch.Store(self.root / "runtime", self.home, self.thread)
        self.addCleanup(lambda: store.close() if store.fd >= 0 else None)
        return store

    def close_store(self):
        self.store.close()
        self.store.fd = -1

    def restart(self):
        self.close_store()
        self.store = self.open_store()
        self.session = watch.Session(
            self.path, self.metadata, self.store, self.args, self.guards, self.sender
        )

    def append(self, *rows):
        with self.path.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def sender(self, *args):
        self.sent.append(args)
        return uid()

    def schedule(self, turn=None, now=1000):
        turn = turn or uid()
        self.append(event("task_started", turn), failure(turn))
        self.session.step(now)
        return turn

    def test_only_structured_exhausted_429_matches(self):
        self.assertTrue(watch.exhausted_429(failure(uid())["payload"]["error"]))
        self.assertTrue(watch.exhausted_429({
            "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 429}}
        }))
        for value in (None, [], {}, {"message": "exceeded retry limit 429"},
                      {"codex_error_info": []},
                      {"codex_error_info": {"response_too_many_failed_attempts": []}},
                      {"codexErrorInfo": {
                          "responseTooManyFailedAttempts": {"httpStatusCode": "429"}}}):
            self.assertFalse(watch.exhausted_429(value))
        self.assertFalse(watch.exhausted_429(failure(uid(), 500)["payload"]["error"]))

    def test_cooldown_and_exact_thread_queue(self):
        self.schedule()
        self.session.step(1059)
        self.assertFalse(self.sent)
        self.session.step(1060)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1:4], (self.home, self.thread, TMP))
        self.assertEqual(self.session.data["attempts"], 1)
        self.assertEqual(self.session.data["job"]["phase"], "awaiting")

    def test_initial_history_skipped(self):
        self.append(failure(uid()))
        self.close_store()
        self.store.path.unlink()
        self.store = self.open_store()
        session = watch.Session(
            self.path, self.metadata, self.store, self.args, self.guards, self.sender
        )
        session.step(999999)
        self.assertIsNone(session.data["job"])
        self.assertFalse(self.sent)

    def test_partial_line_waits(self):
        turn = uid()
        text = json.dumps(failure(turn))
        with self.path.open("a") as stream:
            stream.write(text[:30])
        self.session.step(1000)
        self.assertIsNone(self.session.data["job"])
        with self.path.open("a") as stream:
            stream.write(text[30:] + "\n")
        self.session.step(1001)
        self.assertEqual(self.session.data["job"]["turn"], turn)

    def test_new_session_failure_before_discovery_is_not_skipped(self):
        turn = uid()
        self.append(event("task_started", turn), failure(turn))
        self.close_store()
        self.store.path.unlink()
        self.store = self.open_store()
        session = watch.Session(
            self.path, self.metadata, self.store, self.args, self.guards,
            self.sender, skip_existing=False,
        )
        session.step(1000)
        self.assertEqual(session.data["job"]["turn"], turn)

    def test_initial_partial_history_skipped(self):
        self.append(failure(uid()))
        with self.path.open("a") as stream:
            stream.write('{"type":"event_msg"')
        self.close_store()
        self.store.path.unlink()
        self.store = self.open_store()
        session = watch.Session(
            self.path, self.metadata, self.store, self.args, self.guards, self.sender
        )
        with self.path.open("a") as stream:
            stream.write(', "payload": {}}\n')
        session.step(1000)
        self.assertIsNone(session.data["disabled"])
        self.assertIsNone(session.data["job"])

    def test_restart_resumes_cooldown_and_dedup(self):
        turn = self.schedule()
        self.restart()
        self.session.step(1060)
        self.assertEqual(len(self.sent), 1)
        self.restart()
        self.append(failure(turn))
        self.session.step(1061)
        self.assertEqual(len(self.sent), 1)

    def test_moved_runtime_preserves_checkpoint_lock_and_retry_sequence(self):
        turn = self.schedule()
        self.session.step(1060)
        next_turn = uid()
        self.append(event("task_started", next_turn), failure(next_turn))
        self.session.step(1061)
        before = self.store.path.read_bytes()
        state = dict(self.session.data)
        old_runtime = self.root / "runtime"
        new_runtime = self.root / "notification/runtime/.auto-continue"
        new_runtime.parent.mkdir(parents=True, mode=0o700)
        old_runtime.rename(new_runtime)
        with self.assertRaises(BlockingIOError):
            watch.Store(new_runtime, self.home, self.thread)
        self.close_store()
        self.store = watch.Store(new_runtime, self.home, self.thread)
        self.addCleanup(lambda: self.store.close() if self.store.fd >= 0 else None)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load(), state)
        self.assertEqual(new_runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.session = watch.Session(
            self.path, self.metadata, self.store, self.args, self.guards, self.sender
        )
        self.assertEqual(self.session.data["attempts"], 1)
        self.assertEqual(self.session.data["job"]["due"], 1181)
        self.append(failure(turn))
        self.session.step(1180)
        self.assertEqual(len(self.sent), 1)
        self.session.step(1181)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.session.data["attempts"], 2)
        final_turn = uid()
        self.append(event("task_started", final_turn), event("task_complete", final_turn))
        self.session.step(1182)
        self.assertEqual(self.session.data["attempts"], 0)
        self.assertIsNone(self.session.data["job"])

    def test_merged_paths_and_cli_defaults(self):
        args = watch.parser().parse_args(["--all-cli"])
        self.assertEqual(watch.PROJECT, PROJECT)
        self.assertEqual(args.runtime_dir, PROJECT / "runtime/.auto-continue")
        self.assertEqual(args.tmp_dir, TMP)
        self.assertEqual(TMP.relative_to(PROJECT.parents[1]), Path("tmp"))
        self.assertEqual((args.initial_delay, args.max_delay, args.max_retries),
                         (3, 3, 5))
        explicit = watch.parser().parse_args([
            "--thread", self.thread, "--runtime-dir", str(self.root / "custom"),
        ])
        self.assertEqual(explicit.runtime_dir, self.root / "custom")
        self.assertEqual(explicit.thread, [self.thread])

    def test_manual_new_turn_cancels(self):
        self.schedule()
        self.append(event("task_started", uid()))
        self.session.step(1060)
        self.assertFalse(self.sent)
        self.assertIsNone(self.session.data["job"])
        self.assertEqual(self.session.data["attempts"], 0)

    def test_manual_user_input_cancels_even_before_new_turn(self):
        self.schedule()
        self.append({"type": "response_item", "payload": {
            "role": "user", "content": [{"type": "input_text", "text": "stop"}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text"],
            },
        }})
        self.session.step(1060)
        self.assertFalse(self.sent)

    def test_interruption_and_other_errors_cancel(self):
        for terminal in (
            event("turn_aborted", uid()),
            event("task_complete", uid(), error={"message": "unauthorized"}),
        ):
            self.schedule()
            self.append(terminal)
            self.session.step(1060)
            self.assertIsNone(self.session.data["job"])
            self.assertFalse(self.sent)

    def test_busy_unavailable_or_closed_never_sends(self):
        for attribute, value in (
            ("current_owner", None),
            ("empty", False),
            ("empty", None),
            ("current_history", "changed"),
            ("current_history", "unavailable"),
        ):
            self.guards = FakeGuards()
            self.session.guards = self.guards
            self.schedule()
            setattr(self.guards, attribute, value)
            self.session.step(1060)
            self.assertFalse(self.sent)
            self.assertIsNone(self.session.data["job"])

    def test_process_replaced_during_cooldown_never_sends(self):
        self.schedule()
        self.guards.current_owner = watch.Owner(456, "789", "/test/runtime/codex")
        self.session.step(1060)
        self.assertFalse(self.sent)

    def test_user_turn_arriving_during_guard_checks_cancels(self):
        self.schedule()
        original = self.guards.history
        appended = False

        def history(*args):
            nonlocal appended
            if not appended:
                self.append(event("task_started", uid()))
                appended = True
            return original(*args)

        self.guards.history = history
        self.session.step(1060)
        self.assertFalse(self.sent)

    def test_default_fixed_delay_and_retry_cap(self):
        args = watch.parser().parse_args(["--all-cli"])
        self.args.initial_delay = args.initial_delay
        self.args.max_delay = args.max_delay
        now = 1000
        self.schedule(now=now)
        for attempt in range(1, 6):
            self.assertEqual(self.session.data["job"]["due"], now + 3)
            self.session.step(now + 2)
            self.assertEqual(len(self.sent), attempt - 1)
            now += 3
            self.session.step(now)
            self.assertEqual(len(self.sent), attempt)
            turn = uid()
            self.append(event("task_started", turn), failure(turn))
            self.session.step(now)
        self.assertEqual(self.session.data["disabled"], "retry cap reached")
        self.assertIsNone(self.session.data["job"])

    def test_backoff_cap_and_success_reset(self):
        now = 1000
        self.schedule(now=now)
        for attempt, delay in enumerate((60, 120, 240, 300, 300), 1):
            self.assertEqual(self.session.data["job"]["due"], now + delay)
            now += delay
            self.session.step(now)
            self.assertEqual(len(self.sent), attempt)
            turn = uid()
            self.append(event("task_started", turn))
            self.append({"type": "response_item", "payload": {
                "role": "user", "content": [{"type": "input_text", "text": "continue"}],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["user.text"],
                },
            }})
            self.append(failure(turn))
            self.session.step(now)
        self.assertEqual(self.session.data["disabled"], "retry cap reached")
        self.assertIsNone(self.session.data["job"])
        self.session.step(now + 99999)
        self.assertEqual(len(self.sent), 5)
        turn = uid()
        self.append(event("task_started", turn), event("task_complete", turn))
        self.session.step(now + 100000)
        self.assertEqual(self.session.data["attempts"], 0)
        self.assertIsNone(self.session.data["disabled"])

    def test_timeout_and_unobserved_submission_do_not_resend(self):
        self.schedule()
        self.session.sender = mock.Mock(side_effect=subprocess.TimeoutExpired("queue", 1))
        self.session.step(1060)
        self.assertEqual(self.session.data["disabled"], "uncertain submission")
        self.session.step(10000)
        self.assertEqual(self.session.sender.call_count, 1)
        self.restart()
        self.session.step(10001)
        self.assertFalse(self.sent)

    def test_successful_queue_without_new_turn_stops(self):
        self.schedule()
        self.session.step(1060)
        self.session.step(1090)
        self.assertEqual(self.session.data["disabled"], "submission not observed")
        self.session.step(10000)
        self.assertEqual(len(self.sent), 1)

    def test_crash_after_dispatch_reservation_never_replays(self):
        self.schedule()
        self.session.data["job"]["phase"] = "dispatching"
        self.store.save(self.session.data)
        self.restart()
        self.session.step(10000)
        self.assertFalse(self.sent)
        self.assertEqual(self.session.data["disabled"], "uncertain previous submission")

    def test_single_watcher_lock(self):
        with self.assertRaises(BlockingIOError):
            watch.Store(self.root / "runtime", self.home, self.thread)

    def test_corrupt_checkpoint_fails_closed(self):
        self.close_store()
        self.store.path.write_text("{")
        self.store = self.open_store()
        with self.assertRaises(ValueError):
            watch.Session(self.path, self.metadata, self.store, self.args, self.guards)

    def test_invalid_or_oversized_line_and_truncation_fail_closed(self):
        self.schedule()
        with self.path.open("a") as stream:
            stream.write("not-json\n")
        self.session.step(1060)
        self.assertFalse(self.sent)
        self.assertEqual(self.session.data["disabled"], "invalid rollout JSON")
        self.path.write_text("")
        self.session.step(1061)
        self.assertEqual(self.session.data["disabled"], "rollout replaced or truncated")

    def test_oversized_line_fails_closed(self):
        self.schedule()
        with self.path.open("a") as stream:
            stream.write("x" * 65)
        with mock.patch.object(watch, "MAX_LINE", 64):
            self.session.step(1060)
        self.assertFalse(self.sent)
        self.assertEqual(self.session.data["disabled"], "oversized rollout line")

    def test_batch_limit_defers_dispatch_until_caught_up(self):
        self.schedule()
        self.append(*[{"type": "response_item", "payload": {}} for _ in range(4)],
                    event("task_started", uid()))
        with mock.patch.object(watch, "MAX_BATCH", 2):
            self.session.step(1060)
            self.assertFalse(self.sent)
            self.session.step(1061)
            self.assertFalse(self.sent)
            self.session.step(1062)
        self.assertFalse(self.sent)
        self.assertIsNone(self.session.data["job"])

    def test_dry_run_never_sends(self):
        self.args.dry_run = True
        self.schedule()
        self.session.step(1060)
        self.assertFalse(self.sent)
        self.assertEqual(self.session.data["attempts"], 0)

    def test_subagent_metadata_rejected(self):
        self.assertEqual(watch.session_metadata(self.path), self.metadata)
        self.path.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": uid(), "source": {"subagent": "review"}},
        }) + "\n")
        self.assertIsNone(watch.session_metadata(self.path))

    def test_read_only_history_and_queue_guards(self):
        turn = uid()
        with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as conn:
            conn.execute("CREATE TABLE thread_turns "
                         "(thread_id, turn_id, status, error_json, rollout_ordinal)")
            conn.execute("INSERT INTO thread_turns VALUES (?,?,?,?,?)",
                         (self.thread, turn, "failed",
                          json.dumps(failure(turn)["payload"]["error"]), 1))
            conn.commit()
        with closing(sqlite3.connect(self.home / "queue_1.sqlite")) as conn:
            conn.execute("CREATE TABLE queued_items (thread_id)")
        guards = watch.Guards(self.home)
        self.assertEqual(guards.history(self.thread, turn), "failed")
        self.assertEqual(guards.history(self.thread, uid()), "changed")
        self.assertTrue(guards.queue_empty(self.thread))
        with closing(sqlite3.connect(self.home / "queue_1.sqlite")) as conn:
            conn.execute("INSERT INTO queued_items VALUES (?)", (self.thread,))
            conn.commit()
        self.assertFalse(guards.queue_empty(self.thread))

    def test_owner_requires_matching_lock_and_interactive_codex(self):
        lockdir = self.home / "thread-writer-locks"
        lockdir.mkdir()
        lock = lockdir / (self.thread + ".lock")
        lock.touch()
        proc = self.root / "proc"
        fd = proc / "123/fd"
        fd.mkdir(parents=True)
        (proc / "123/exe").symlink_to("/test/runtime/codex")
        (fd / "0").symlink_to("/dev/pts/99")
        (proc / "123/stat").write_text("123 (codex) S " + "0 " * 18 + "987 0")
        info = lock.stat()
        (proc / "locks").write_text(
            "1: FLOCK ADVISORY WRITE 123 {:02x}:{:02x}:{} 0 EOF\n".format(
                os.major(info.st_dev), os.minor(info.st_dev), info.st_ino
            )
        )
        guards = watch.Guards(self.home, proc)
        self.assertEqual(guards.owner(self.thread), watch.Owner(123, "987", "/test/runtime/codex"))
        (fd / "0").unlink()
        (fd / "0").symlink_to("/dev/null")
        self.assertIsNone(guards.owner(self.thread))

    def test_sender_uses_original_runtime_and_home_without_gateway(self):
        owner = self.guards.current_owner
        process = mock.Mock()
        process.communicate.return_value = (
            "Queued message {} for thread {}.\n".format(uid(), self.thread), ""
        )
        process.returncode = 0
        with mock.patch.object(watch.subprocess, "Popen", return_value=process) as popen:
            submission = watch.queue_continue(owner, self.home, self.thread, TMP, 15)
        self.assertIsNotNone(submission)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [
            owner.executable, "-c", "notify=[]", "queue",
            "--thread", self.thread, "--message", "continue",
        ])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.home))
        self.assertEqual(kwargs["env"]["TMPDIR"], str(TMP))

    def test_sender_timeout_terminates_child_without_retry(self):
        process = mock.Mock()
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("queue", 15), ("", ""),
        ]
        process.poll.return_value = None
        process.pid = 123
        with mock.patch.object(watch.subprocess, "Popen", return_value=process), \
                mock.patch.object(watch.os, "killpg") as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                watch.queue_continue(self.guards.current_owner, self.home, self.thread, TMP, 15)
        kill.assert_called_once_with(123, watch.signal.SIGTERM)
        self.assertEqual(process.communicate.call_count, 2)

    def test_missing_fields_and_nonfinite_checkpoint_fail_closed(self):
        data = json.loads(self.store.path.read_text())
        for key in ("active_turn", "job", "disabled"):
            malformed = dict(data)
            malformed.pop(key)
            self.store.save(malformed)
            with self.assertRaises(ValueError):
                self.store.load()
        data["job"] = {"turn": uid(), "due": float("nan"), "phase": "cooldown"}
        self.store.save(data)
        with self.assertRaises(ValueError):
            self.store.load()

    def test_cli_dry_run_has_separate_checkpoints_and_never_replays_history(self):
        self.append(failure(uid()))
        result = subprocess.run([
            sys.executable, "-B", str(WATCHER),
            "--thread", self.thread, "--codex-home", str(self.home),
            "--runtime-dir", str(self.root / "cli-runtime"),
            "--tmp-dir", str(TMP), "--dry-run", "--once",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(" INFO Watching ", result.stderr)
        self.assertIn("Registered session files: 1; live interactive sessions: 0; "
                      "inactive session files: 1.", result.stderr)
        checkpoints = list((self.root / "cli-runtime/dry-run").glob("*/*.json"))
        self.assertEqual(len(checkpoints), 1)
        self.assertIsNone(json.loads(checkpoints[0].read_text())["job"])
        self.assertEqual(list((self.root / "cli-runtime").glob("*.json")), [])

    def test_cli_default_runtime_isolates_notifications_and_dry_run(self):
        project = self.root / "workspace/infra/tcodex-notification"
        watcher_script = project / "server/watch-retries.py"
        watcher_script.parent.mkdir(parents=True)
        shutil.copyfile(WATCHER, watcher_script)
        runtime = project / "runtime"
        channel = runtime / "default"
        runtime.mkdir(mode=0o700)
        channel.mkdir(mode=0o700)
        notification = channel / "event.json"
        notification.write_bytes(b"notification data\n")
        self.close_store()
        (self.root / "runtime").rename(runtime / ".auto-continue")
        checkpoint = runtime / ".auto-continue" / self.store.path.relative_to(
            self.root / "runtime"
        )
        before = checkpoint.read_bytes()
        self.append(failure(uid()))
        result = subprocess.run([
            sys.executable, "-B", str(watcher_script),
            "--thread", self.thread, "--codex-home", str(self.home),
            "--tmp-dir", str(TMP), "--dry-run", "--once",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        dry_checkpoints = list((runtime / ".auto-continue/dry-run").glob("*/*.json"))
        self.assertEqual(len(dry_checkpoints), 1)
        self.assertIsNone(json.loads(dry_checkpoints[0].read_text())["job"])
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual(notification.read_bytes(), b"notification data\n")
        self.assertFalse((project / "server/runtime").exists())

    def test_inventory_lists_only_live_sessions_and_suppresses_unchanged_output(self):
        live = uid()
        inactive = uid()
        sessions = {live: object(), inactive: object()}
        guards = mock.Mock()
        owner = self.guards.current_owner
        guards.owner.side_effect = lambda thread: owner if thread == live else None
        with self.assertLogs(watch.LOG, level="INFO") as logs:
            snapshot = watch.report_live_sessions(sessions, guards)
        output = "\n".join(logs.output)
        self.assertIn("Registered session files: 2; live interactive sessions: 1; "
                      "inactive session files: 1.", output)
        self.assertIn("Live session " + live, output)
        self.assertNotIn(inactive, output)
        with mock.patch.object(watch.LOG, "info") as info:
            self.assertEqual(watch.report_live_sessions(sessions, guards, snapshot), snapshot)
        info.assert_not_called()

    def test_inventory_reports_sessions_becoming_live_or_exiting(self):
        sessions = {self.thread: object()}
        guards = mock.Mock()
        guards.owner.return_value = None
        with mock.patch.object(watch.LOG, "info"):
            snapshot = watch.report_live_sessions(sessions, guards)
        guards.owner.return_value = self.guards.current_owner
        with self.assertLogs(watch.LOG, level="INFO") as logs:
            snapshot = watch.report_live_sessions(sessions, guards, snapshot)
        self.assertIn("Live session " + self.thread, "\n".join(logs.output))
        guards.owner.return_value = None
        with self.assertLogs(watch.LOG, level="INFO") as logs:
            watch.report_live_sessions(sessions, guards, snapshot)
        self.assertIn("is no longer live", "\n".join(logs.output))

    def test_cli_conflicting_watcher_fails_and_does_not_write(self):
        before = self.store.path.read_text()
        result = subprocess.run([
            sys.executable, "-B", str(WATCHER),
            "--thread", self.thread, "--codex-home", str(self.home),
            "--runtime-dir", str(self.root / "runtime"), "--tmp-dir", str(TMP),
            "--once",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("another watcher", result.stderr)
        self.assertEqual(self.store.path.read_text(), before)

    def test_cli_requires_explicit_scope_and_rejects_tmp(self):
        with self.assertRaises(SystemExit):
            watch.parser().parse_args([])
        with self.assertRaises(SystemExit):
            watch.main(["--thread", self.thread, "--codex-home", str(self.home),
                        "--tmp-dir", "/tmp"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
