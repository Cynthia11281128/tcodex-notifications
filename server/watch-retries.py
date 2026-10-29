#!/usr/bin/env python3
"""Continue explicitly selected, live TCodex CLI threads after exhausted 429s."""

import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass


LOG = logging.getLogger("tcodex-auto-continue")
PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_TMP = PROJECT.parents[1] / "tmp"
MAX_LINE = 16 * 1024 * 1024
MAX_BATCH = 1000
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def thread_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("Thread must be a session UUID.")


def exhausted_429(error):
    if not isinstance(error, dict):
        return False
    info = error.get("codex_error_info", error.get("codexErrorInfo"))
    if not isinstance(info, dict):
        return False
    attempts = info.get(
        "response_too_many_failed_attempts", info.get("responseTooManyFailedAttempts")
    )
    return (
        isinstance(attempts, dict)
        and type(attempts.get("http_status_code", attempts.get("httpStatusCode"))) is int
        and attempts.get("http_status_code", attempts.get("httpStatusCode")) == 429
    )


def private_dir(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Runtime directory must be owned by this user with mode 0700.")


def private_open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        os.close(fd)
        raise ValueError("Runtime file must be owned by this user with mode 0600.")
    return fd


class Store:
    """Per-home, per-thread lifetime lock and atomic checkpoints."""

    def __init__(self, root, home, thread):
        private_dir(root)
        self.directory = root / hashlib.sha256(str(home).encode()).hexdigest()
        private_dir(self.directory)
        self.path = self.directory / (thread + ".json")
        self.fd = private_open(
            self.directory / (thread + ".lock"), os.O_CREAT | os.O_RDWR
        )
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise
        self.home = str(home)
        self.thread = thread

    def load(self):
        try:
            fd = private_open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as stream:
            data = json.load(stream)
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or data.get("home") != self.home
            or data.get("thread") != self.thread
            or type(data.get("offset")) is not int
            or data["offset"] < 0
            or type(data.get("attempts")) is not int
            or data["attempts"] < 0
            or not isinstance(data.get("seen"), list)
            or not all(isinstance(item, str) for item in data["seen"])
            or not isinstance(data.get("path"), str)
            or not isinstance(data.get("identity"), list)
            or len(data["identity"]) != 2
            or not all(type(item) is int for item in data["identity"])
            or not all(key in data for key in (
                "job", "active_turn", "auto_turn", "disabled", "skip_partial"
            ))
            or type(data.get("skip_partial")) is not bool
            or any(data.get(key) is not None and not isinstance(data[key], str)
                   for key in ("active_turn", "auto_turn", "disabled"))
        ):
            raise ValueError("Invalid checkpoint; refusing to replay events.")
        job = data.get("job")
        if job is not None and (
            not isinstance(job, dict)
            or job.get("phase") not in ("cooldown", "dispatching", "awaiting")
            or not isinstance(job.get("turn"), str)
            or type(job.get("due")) not in (int, float)
            or not math.isfinite(job.get("due"))
        ):
            raise ValueError("Invalid pending checkpoint.")
        if job is not None and job["phase"] == "cooldown":
            owner = job.get("owner")
            if owner is not None and (
                not isinstance(owner, dict)
                or type(owner.get("pid")) is not int
                or not isinstance(owner.get("start_ticks"), str)
                or not isinstance(owner.get("executable"), str)
            ):
                raise ValueError("Invalid pending owner.")
        return data

    def save(self, data):
        pending = self.directory / (".pending-" + uuid.uuid4().hex)
        try:
            fd = private_open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, ensure_ascii=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            pending.unlink(missing_ok=True)

    def close(self):
        os.close(self.fd)


@dataclass(frozen=True)
class Owner:
    pid: int
    start_ticks: str
    executable: str


class Guards:
    """Read-only checks of local Codex ownership, history, and queued input."""

    def __init__(self, home, proc=Path("/proc")):
        self.home = home
        self.proc = proc

    def owner(self, thread):
        lock = self.home / "thread-writer-locks" / (thread + ".lock")
        try:
            info = lock.stat()
            key = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
            for line in (self.proc / "locks").read_text().splitlines():
                fields = line.split()
                if len(fields) < 8 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
                    continue
                major, minor, inode = fields[5].split(":")
                if (int(major, 16), int(minor, 16), int(inode)) != key:
                    continue
                pid = int(fields[4])
                directory = self.proc / str(pid)
                if directory.stat().st_uid != os.getuid():
                    return None
                executable = os.readlink(directory / "exe")
                if Path(executable).name != "codex":
                    return None
                # A held writer lock alone also covers headless exec; require a live TUI.
                if not os.readlink(directory / "fd/0").startswith("/dev/pts/"):
                    return None
                process_stat = (directory / "stat").read_text()
                start_ticks = process_stat.rsplit(")", 1)[1].split()[19]
                return Owner(pid, start_ticks, executable)
        except (OSError, ValueError, IndexError):
            return None
        return None

    def history(self, thread, turn):
        try:
            uri = (self.home / "thread_history_1.sqlite").as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
                row = conn.execute(
                    "SELECT turn_id, status, error_json FROM thread_turns "
                    "WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",
                    (thread,),
                ).fetchone()
            if row is None or row[0] != turn or row[1] != "failed":
                return "changed"
            return "failed" if exhausted_429(json.loads(row[2] or "{}")) else "changed"
        except (OSError, sqlite3.Error, ValueError):
            return "unavailable"

    def queue_empty(self, thread):
        try:
            uri = (self.home / "queue_1.sqlite").as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
                count = conn.execute(
                    "SELECT count(*) FROM queued_items WHERE thread_id=?", (thread,)
                ).fetchone()[0]
            return count == 0
        except (OSError, sqlite3.Error):
            return None


def queue_continue(owner, home, thread, tmp_dir, timeout):
    env = os.environ.copy()
    env.update(
        CODEX_HOME=str(home), TMPDIR=str(tmp_dir), TMP=str(tmp_dir), TEMP=str(tmp_dir)
    )
    # The original runtime writes the shared queue. No gateway or model request is needed.
    process = subprocess.Popen(
        [
            owner.executable, "-c", "notify=[]", "queue",
            "--thread", thread, "--message", "continue",
        ],
        env=env,
        cwd=tmp_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _stderr = process.communicate(timeout=timeout)
        match = re.search(
            r"Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})\.", stdout
        )
        if process.returncode == 0 and match and match[2] == thread:
            return match[1]
        return None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()


class Session:
    def __init__(self, path, metadata, store, args, guards, sender=queue_continue,
                 skip_existing=True):
        self.path = path
        self.thread = metadata["id"]
        self.store = store
        self.args = args
        self.guards = guards
        self.sender = sender
        info = path.stat()
        identity = [info.st_dev, info.st_ino]
        self.data = store.load()
        if self.data is None:
            with path.open("rb") as stream:
                stream.seek(max(0, info.st_size - 1))
                skip_partial = skip_existing and info.st_size > 0 and stream.read(1) != b"\n"
            self.data = {
                "version": 1, "home": str(args.codex_home), "thread": self.thread,
                "path": str(path), "identity": identity,
                "offset": info.st_size if skip_existing else 0,
                "attempts": 0, "seen": [], "job": None, "active_turn": None,
                "auto_turn": None, "disabled": None, "skip_partial": skip_partial,
            }
            LOG.debug("Registered rollout %s%s.", self.thread,
                      "; existing history skipped" if skip_existing else "; new session")
        elif self.data["path"] != str(path) or self.data["identity"] != identity:
            self.data["job"] = None
            self.data["disabled"] = "rollout replaced"
            LOG.error("Thread %s rollout replaced; automatic continuation disabled.", self.thread)
        elif self.data.get("job") and self.data["job"]["phase"] == "dispatching":
            self.data["job"] = None
            self.data["disabled"] = "uncertain previous submission"
            LOG.error("Thread %s has an uncertain submission; not resending.", self.thread)
        store.save(self.data)

    def cancel(self, reason, reset=True):
        if self.data["job"] is not None:
            LOG.info("Cancelled continuation for %s: %s.", self.thread, reason)
        self.data["job"] = None
        self.data["auto_turn"] = None
        if reset:
            self.data["attempts"] = 0

    def consume(self, row, now):
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return
        payload = row["payload"]
        if row.get("type") == "response_item" and payload.get("role") == "user":
            metadata = payload.get("internal_chat_message_metadata_passthrough", {})
            if not isinstance(metadata, dict):
                return
            kinds = metadata.get("content_item_kinds")
            if not isinstance(kinds, list) or "user.text" not in kinds:
                return
            content = payload.get("content", [])
            if not isinstance(content, list):
                return
            text = "".join(
                item.get("text", "") for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
            if self.data["auto_turn"] and text == "continue":
                return
            self.cancel("user input")
            return
        if row.get("type") != "event_msg":
            return
        kind = payload.get("type")
        turn = payload.get("turn_id")
        if not isinstance(turn, str) or not UUID_PATTERN.fullmatch(turn):
            return
        if kind == "task_started":
            if self.data["active_turn"] == turn:
                return
            job = self.data["job"]
            if job and job["phase"] == "awaiting":
                LOG.info("Thread %s started turn %s after queued continue.", self.thread, turn)
                self.data["job"] = None
                self.data["auto_turn"] = turn
            else:
                self.cancel("new turn started")
            self.data["active_turn"] = turn
            # Uncertain delivery or a reached cap requires fresh user activity to rearm.
            self.data["disabled"] = None
        elif kind == "turn_aborted":
            self.cancel("turn interrupted")
            self.data["active_turn"] = None
        elif kind == "task_complete":
            if turn in self.data["seen"]:
                return
            if self.data["active_turn"] not in (None, turn):
                return
            self.data["seen"] = (self.data["seen"] + [turn])[-256:]
            self.data["active_turn"] = None
            if not exhausted_429(payload.get("error")):
                self.cancel("turn ended without exhausted 429")
                self.data["disabled"] = None
                return
            if self.data["disabled"]:
                return
            if self.data["attempts"] >= self.args.max_retries:
                self.data["job"] = None
                self.data["disabled"] = "retry cap reached"
                LOG.warning("Thread %s reached %s automatic continues; stopping.",
                            self.thread, self.args.max_retries)
                return
            delay = min(
                self.args.initial_delay * 2 ** self.data["attempts"],
                self.args.max_delay,
            )
            owner = self.guards.owner(self.thread)
            self.data["job"] = {
                "phase": "cooldown", "turn": turn, "due": now + delay,
                "owner": asdict(owner) if owner else None,
            }
            self.data["auto_turn"] = None
            LOG.info("Thread %s exhausted 429; continue scheduled in %ss (%s/%s).",
                     self.thread, delay, self.data["attempts"] + 1, self.args.max_retries)

    def read_new(self, now):
        info = self.path.stat()
        if [info.st_dev, info.st_ino] != self.data["identity"] or info.st_size < self.data["offset"]:
            self.cancel("rollout replaced or truncated", reset=False)
            self.data["disabled"] = "rollout replaced or truncated"
            return False
        with self.path.open("rb") as stream:
            stream.seek(self.data["offset"])
            for _ in range(MAX_BATCH):
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    return True
                if len(line) > MAX_LINE:
                    self.cancel("oversized rollout line", reset=False)
                    self.data["disabled"] = "oversized rollout line"
                    return False
                if not line.endswith(b"\n"):
                    return False
                self.data["offset"] = stream.tell()
                if self.data.get("skip_partial"):
                    self.data["skip_partial"] = False
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    self.cancel("invalid rollout JSON", reset=False)
                    self.data["disabled"] = "invalid rollout JSON"
                    return False
                self.consume(row, now)
            return stream.tell() == info.st_size

    def step(self, now=None):
        now = time.time() if now is None else now
        before = json.dumps(self.data, sort_keys=True)
        try:
            caught_up = self.read_new(now)
        except OSError:
            self.cancel("rollout unavailable", reset=False)
            self.data["disabled"] = "rollout unavailable"
            caught_up = False
        if json.dumps(self.data, sort_keys=True) != before:
            self.store.save(self.data)
        job = self.data["job"]
        if not caught_up or job is None or self.data["disabled"]:
            return
        if job["phase"] == "awaiting":
            if now >= job["due"]:
                self.cancel("no new turn observed after submission", reset=False)
                self.data["disabled"] = "submission not observed"
                self.store.save(self.data)
            return
        if now < job["due"]:
            return
        if self.args.dry_run:
            LOG.info("DRY RUN: would queue continue for %s, failed turn %s.",
                     self.thread, job["turn"])
            self.data["job"] = None
            self.store.save(self.data)
            return
        owner = self.guards.owner(self.thread)
        history = self.guards.history(self.thread, job["turn"])
        empty = self.guards.queue_empty(self.thread)
        if (
            owner is None or asdict(owner) != job.get("owner")
            or history != "failed" or empty is not True
        ):
            self.cancel("not live, history changed/unavailable, or queued user input")
            self.store.save(self.data)
            return
        # Drain again after SQLite/proc checks. Refuse a partial or concurrently appended line.
        if not self.read_new(now) or self.data["job"] != job:
            self.store.save(self.data)
            return
        if self.guards.owner(self.thread) != owner:
            self.cancel("original CLI process changed")
            self.store.save(self.data)
            return
        if (
            self.path.stat().st_size != self.data["offset"]
            or self.guards.history(self.thread, job["turn"]) != "failed"
            or self.guards.queue_empty(self.thread) is not True
        ):
            return
        self.data["attempts"] += 1
        self.data["job"] = {
            "phase": "dispatching", "turn": job["turn"], "due": now,
        }
        # Reserve before the side effect. Crashes can lose one continue, never replay it.
        self.store.save(self.data)
        try:
            submission = self.sender(
                owner, self.args.codex_home, self.thread,
                self.args.tmp_dir, self.args.command_timeout,
            )
        except (OSError, subprocess.SubprocessError):
            submission = None
        if submission is None:
            self.cancel("queue command failed or delivery uncertain", reset=False)
            self.data["disabled"] = "uncertain submission"
        else:
            self.data["job"] = {
                "phase": "awaiting", "turn": job["turn"],
                "due": now + self.args.observe_timeout,
                "submission": submission,
            }
            LOG.info("Queued continue for %s (%s/%s); awaiting a new turn.",
                     self.thread, self.data["attempts"], self.args.max_retries)
        self.store.save(self.data)


def session_metadata(path):
    try:
        with path.open("rb") as stream:
            line = stream.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE:
            return None
        row = json.loads(line)
        payload = row.get("payload")
        if (
            row.get("type") == "session_meta"
            and isinstance(payload, dict)
            and isinstance(payload.get("id"), str)
            and UUID_PATTERN.fullmatch(payload["id"])
            and payload.get("source") == "cli"
        ):
            return payload
    except (OSError, ValueError, AttributeError):
        pass
    return None


def report_live_sessions(sessions, guards, previous=None):
    live = {}
    for thread in sorted(sessions):
        owner = guards.owner(thread)
        if owner is not None:
            live[thread] = owner
    snapshot = (len(sessions), live)
    if snapshot == previous:
        return snapshot
    LOG.info("Registered session files: %s; live interactive sessions: %s; "
             "inactive session files: %s.",
             len(sessions), len(live), len(sessions) - len(live))
    old_live = previous[1] if previous is not None else {}
    for thread, owner in live.items():
        if old_live.get(thread) != owner:
            LOG.info("Live session %s; PID %s.", thread, owner.pid)
    for thread in sorted(old_live.keys() - live.keys()):
        LOG.info("Session %s is no longer live.", thread)
    return snapshot


def positive_number(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("Must be a positive finite number.")
    return number


def positive_int(value):
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("Must be between 1 and 100.")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    scope = result.add_mutually_exclusive_group(required=True)
    scope.add_argument("--thread", type=thread_uuid, action="append",
                       help="Monitor a session UUID; repeat to monitor multiple sessions.")
    scope.add_argument("--all-cli", action="store_true",
                       help="Opt in to all current and future local CLI sessions.")
    result.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", "~/.tcodex")).expanduser())
    result.add_argument("--runtime-dir", type=Path,
                        default=PROJECT / "runtime/.auto-continue")
    result.add_argument("--tmp-dir", type=Path, default=DEFAULT_TMP)
    result.add_argument("--initial-delay", type=positive_number, default=60)
    result.add_argument("--max-delay", type=positive_number, default=300)
    result.add_argument("--max-retries", type=positive_int, default=5)
    result.add_argument("--poll-interval", type=positive_number, default=1)
    result.add_argument("--discover-interval", type=positive_number, default=10)
    result.add_argument("--command-timeout", type=positive_number, default=15)
    result.add_argument("--observe-timeout", type=positive_number, default=30)
    result.add_argument("--dry-run", action="store_true",
                        help="Log matching new failures without queueing messages.")
    result.add_argument("--once", action="store_true",
                        help="Read one batch and process due work, then exit.")
    return result


def main(argv=None):
    arg_parser = parser()
    args = arg_parser.parse_args(argv)
    for name in ("codex_home", "runtime_dir", "tmp_dir"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    if args.tmp_dir == Path("/tmp") or Path("/tmp") in args.tmp_dir.parents:
        arg_parser.error("--tmp-dir must not be under /tmp")
    if args.max_delay < args.initial_delay:
        arg_parser.error("--max-delay must not be less than --initial-delay")
    if not (args.codex_home / "sessions").is_dir():
        arg_parser.error("--codex-home must contain an existing sessions directory")
    if not Path("/proc/locks").exists():
        arg_parser.error("Linux /proc/locks is required for live-session checks")
    args.tmp_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(TMPDIR=str(args.tmp_dir), TMP=str(args.tmp_dir), TEMP=str(args.tmp_dir))
    # Dry runs must never consume the live watcher's checkpoints or retry budget.
    if args.dry_run:
        args.runtime_dir = args.runtime_dir / "dry-run"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    selected = set(args.thread or [])
    sessions = {}
    rejected = set()
    guards = Guards(args.codex_home)
    live_snapshot = None
    last_discovery = -float("inf")
    existing_paths = set((args.codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"))
    LOG.info(
        "%s home=%s scope=%s initial_delay=%ss max_delay=%ss max_retries=%s.",
        "Dry-run watcher" if args.dry_run else "Watcher",
        args.codex_home, "all CLI sessions" if args.all_cli else ",".join(sorted(selected)),
        args.initial_delay, args.max_delay, args.max_retries,
    )

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            now = time.time()
            if time.monotonic() - last_discovery >= args.discover_interval:
                for path in sorted((args.codex_home / "sessions").glob("*/*/*/rollout-*.jsonl")):
                    if not args.all_cli and not any(
                        path.name.endswith(thread + ".jsonl") for thread in selected
                    ):
                        continue
                    metadata = session_metadata(path)
                    if metadata is None:
                        continue
                    thread = metadata["id"]
                    if thread in sessions or thread in rejected:
                        continue
                    store = None
                    try:
                        store = Store(args.runtime_dir, args.codex_home, thread)
                        sessions[thread] = Session(
                            path, metadata, store, args, guards,
                            skip_existing=path in existing_paths,
                        )
                    except (OSError, ValueError):
                        if store is not None:
                            store.close()
                        rejected.add(thread)
                        LOG.error("Cannot watch %s; checkpoint invalid or another watcher owns it.",
                                  thread)
                last_discovery = time.monotonic()
                live_snapshot = report_live_sessions(sessions, guards, live_snapshot)
            for session in sessions.values():
                session.step(now)
            if selected and rejected:
                return 1
            if args.once:
                return 1 if rejected or (selected - sessions.keys()) else 0
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        LOG.info("Watcher stopped; no queued messages are removed.")
        return 0
    except (OSError, ValueError):
        LOG.error("Watcher stopped because checkpoint or runtime access failed.")
        return 1
    finally:
        for session in sessions.values():
            session.store.close()


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
