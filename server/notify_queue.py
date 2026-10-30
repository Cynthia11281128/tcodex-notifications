"""Bounded atomic-file outbox; writes stay inside the workspace."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"
DEFAULT_CHANNEL = "default"
RETENTION_SECONDS = 24 * 60 * 60
MAX_EVENTS = 1000
EVENT_NAME = re.compile(r"^([0-9]{20})-([0-9]{12})-([0-9a-f]{64})\.json$")


def validate_channel(channel):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", channel):
        raise ValueError("Channel must be 1-64 letters, digits, underscores or hyphens.")
    return channel


def private_directory(path):
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Runtime directories must be owned by this user with mode 0700.")


def private_file(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("Runtime files must be owned by this user with mode 0600.")
    return fd


class NotificationQueue:
    def __init__(self, channel=DEFAULT_CHANNEL, runtime_dir=RUNTIME_DIR):
        validate_channel(channel)
        runtime_dir = Path(runtime_dir)
        private_directory(runtime_dir)
        self.path = runtime_dir / channel
        private_directory(self.path)
        self.lock_fd = private_file(self.path / ".lock", os.O_CREAT | os.O_RDWR)

    def close(self):
        os.close(self.lock_fd)

    @contextlib.contextmanager
    def locked(self, exclusive=False):
        deadline = time.monotonic() + 10
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(self.lock_fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Notification queue is busy.")
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def event_files(self):
        return sorted(path for path in self.path.iterdir() if EVENT_NAME.fullmatch(path.name))

    def watermark(self, files=None):
        try:
            fd = private_file(self.path / ".cursor", os.O_RDONLY)
        except FileNotFoundError:
            cursor = 0
        else:
            with os.fdopen(fd) as stream:
                raw = stream.read(32).strip()
            if not re.fullmatch(r"[0-9]{1,18}", raw):
                raise ValueError("Invalid notification cursor.")
            cursor = int(raw)
        files = self.event_files() if files is None else files
        return max(cursor, int(files[-1].name[:20]) if files else 0)

    def high_water(self):
        with self.locked():
            return self.watermark()

    def atomic_write(self, path, text):
        temp = self.path / (".pending-" + uuid.uuid4().hex)
        try:
            fd = private_file(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def prune_files(self, now, files, limit=MAX_EVENTS):
        kept = []
        for index, path in enumerate(files):
            created = int(EVENT_NAME.fullmatch(path.name).group(2))
            if created < now - RETENTION_SECONDS or index < len(files) - limit:
                path.unlink()
            else:
                kept.append(path)
        return kept

    def prune(self, now=None):
        now = int(time.time()) if now is None else now
        with self.locked(exclusive=True):
            files = self.event_files()
            self.atomic_write(self.path / ".cursor", str(self.watermark(files)))
            self.prune_files(now, files)

    def enqueue(self, payload, now=None):
        if not isinstance(payload, dict):
            return False
        kind = payload.get("type")
        if kind not in ("agent-turn-complete", "user-input-requested"):
            return False
        thread = payload.get("thread-id")
        turn = payload.get("turn-id")
        if kind == "user-input-requested":
            call = payload.get("tool-use-id")
            if not all(isinstance(value, str) and 0 < len(value) <= 1024 for value in (thread, turn, call)):
                return False
            key = hashlib.sha256(json.dumps(
                [kind, thread, turn, call], ensure_ascii=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
        elif isinstance(thread, str) and thread and isinstance(turn, str) and turn:
            # Preserve completion keys so upgrading does not replay existing events.
            key = hashlib.sha256((thread + "\0" + turn).encode("utf-8")).hexdigest()
        else:
            key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        if not isinstance(thread, str) or not thread:
            thread = uuid.uuid4().hex
        session = hashlib.sha256(thread.encode("utf-8")).hexdigest()[:12]
        now = int(time.time()) if now is None else now
        with self.locked(exclusive=True):
            files = self.event_files()
            cursor = self.watermark(files)
            if any(
                path.name.endswith("-" + key + ".json")
                and int(EVENT_NAME.fullmatch(path.name).group(2)) >= now - RETENTION_SECONDS
                for path in files
            ):
                return False
            self.prune_files(now, files, MAX_EVENTS - 1)
            event_id = cursor + 1
            if event_id > 999999999999999999:
                raise ValueError("Notification sequence exhausted.")
            target = self.path / ("{:020d}-{:012d}-{}.json".format(event_id, now, key))
            row = [event_id, now, session]
            if kind == "user-input-requested":
                row.append(kind)
            self.atomic_write(target, json.dumps(row) + "\n")
            return True

    def read_after(self, cursor, now=None):
        now = int(time.time()) if now is None else now
        records = []
        with self.locked():
            for path in self.event_files():
                expected_id = int(path.name[:20])
                if expected_id <= cursor:
                    continue
                fd = private_file(path, os.O_RDONLY)
                with os.fdopen(fd) as stream:
                    row = json.loads(stream.read(1024))
                if (
                    not isinstance(row, list)
                    or len(row) not in (3, 4)
                    or (len(row) == 4 and row[3] != "user-input-requested")
                    or type(row[0]) is not int
                    or row[0] != expected_id
                    or type(row[1]) is not int
                    or row[1] != int(EVENT_NAME.fullmatch(path.name).group(2))
                    or not isinstance(row[2], str)
                    or not re.fullmatch(r"[0-9a-f]{12}", row[2])
                ):
                    raise ValueError("Invalid notification record.")
                if row[1] >= now - RETENTION_SECONDS:
                    records.append(tuple(row))
                if len(records) >= 100:
                    break
        return records
