#!/usr/bin/env python3
"""TClaude lifecycle registry and fail-closed native rewake bridge."""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time
import uuid

sys.dont_write_bytecode = True
from notify_queue import private_directory, private_file

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT / "runtime/.tclaude-auto-continue"
DEFAULT_TMP = PROJECT.parents[1] / "tmp"
MAX_INPUT = 1024 * 1024
MAX_TAIL = 16 * 1024 * 1024
FEEDBACK = "Continue the original user task after the exhausted HTTP 429. Preserve all existing permissions and constraints."
VERIFIED_RUNTIME = "claude-code-2.1.295"


def supported_owner(owner):
    return isinstance(owner, dict) and Path(owner.get("executable", "")).parent.name == VERIFIED_RUNTIME


def valid_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def owner_info(pid):
    if type(pid) is not int or pid <= 1:
        return None
    try:
        directory = Path("/proc") / str(pid)
        if directory.stat().st_uid != os.getuid():
            return None
        executable = os.readlink(directory / "exe")
        if Path(executable).name != "claude":
            return None
        if not os.readlink(directory / "fd/0").startswith("/dev/pts/"):
            return None
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start": fields[19], "executable": executable}
    except (OSError, ValueError, IndexError):
        return None


def ancestor_owner():
    pid = os.getppid()
    for _ in range(12):
        owner = owner_info(pid)
        if owner:
            return owner
        try:
            pid = int((Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if pid <= 1:
            return None
    return None


def terminal_record(path, session):
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - MAX_TAIL))
            if size > MAX_TAIL:
                stream.readline()
            raw = stream.read(MAX_TAIL)
        if not raw.endswith(b"\n"):
            return None
        for line in reversed(raw.splitlines()):
            row = json.loads(line)
            if not isinstance(row, dict):
                return None
            if row.get("type") not in ("user", "assistant"):
                continue
            if row.get("sessionId", row.get("session_id")) != session:
                return None
            return row if row.get("type") == "assistant" else None
    except (OSError, ValueError):
        pass
    return None


def final_429(payload):
    if payload.get("hook_event_name") != "StopFailure" or payload.get("error") != "rate_limit":
        return None
    row = terminal_record(payload.get("transcript_path"), payload.get("session_id"))
    if row is None or row.get("isApiErrorMessage") is not True:
        return None
    if type(row.get("apiErrorStatus")) is not int or row["apiErrorStatus"] != 429:
        return None
    failure = row.get("uuid")
    return failure if isinstance(failure, str) and valid_id(failure) else None


def root_path():
    return Path(os.environ.get("TCLAUDE_NOTIFICATION_RUNTIME", DEFAULT_ROOT)).resolve()


def socket_path(root, tmp):
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:20]
    return tmp / ("tclaude-watch-" + str(os.getuid())) / (digest + ".sock")


def ensure_root(root):
    private_directory(root.parent)
    private_directory(root)


@contextlib.contextmanager
def state(root, session):
    if not valid_id(session):
        raise ValueError("Invalid session.")
    ensure_root(root)
    fd = private_file(root / (session + ".lock"), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = root / (session + ".json")
        try:
            read_fd = private_file(path, os.O_RDONLY)
        except FileNotFoundError:
            data = None
        else:
            with os.fdopen(read_fd) as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or data.get("session") != session or data.get("version") != 1:
                raise ValueError("Invalid registry.")
            if "owner" in data:
                if (
                    not isinstance(data["owner"], dict)
                    or type(data["owner"].get("pid")) is not int
                    or not isinstance(data["owner"].get("start"), str)
                    or not isinstance(data["owner"].get("executable"), str)
                    or type(data.get("attempts")) is not int or not 0 <= data["attempts"] <= 100
                    or type(data.get("disabled")) is not bool
                    or not isinstance(data.get("seen"), list)
                    or not all(valid_id(item) for item in data["seen"])
                    or data.get("status") not in ("idle", "active", "failed", "closed")
                ):
                    raise ValueError("Invalid registry fields.")
                job = data.get("job")
                if job is not None and (
                    not isinstance(job, dict)
                    or job.get("phase") not in ("cooldown", "dispatching", "awaiting")
                    or not valid_id(job.get("failure"))
                    or type(job.get("due")) not in (int, float)
                    or not 0 < job["due"] < float("inf")
                    or not isinstance(job.get("transcript"), str)
                    or type(job.get("observe_timeout")) not in (int, float)
                    or not 0 < job["observe_timeout"] <= 300
                ):
                    raise ValueError("Invalid dispatch checkpoint.")
        box = [data]
        yield box
        if box[0] is not None:
            pending = root / (".pending-" + uuid.uuid4().hex)
            try:
                write_fd = private_file(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(write_fd, "w") as stream:
                    json.dump(box[0], stream)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(pending, path)
                directory_fd = os.open(root, os.O_DIRECTORY | os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                pending.unlink(missing_ok=True)
    finally:
        os.close(fd)


def lifecycle(payload, root, owner):
    session = payload["session_id"]
    kind = payload["hook_event_name"]
    prompt = payload.get("prompt_id")
    with state(root, session) as box:
        data = box[0]
        if kind == "SessionStart":
            box[0] = {
                "version": 1, "session": session, "owner": owner, "generation": uuid.uuid4().hex,
                "prompt": None, "status": "idle", "attempts": 0, "seen": [],
                "disabled": False, "job": None, "updated": time.time(),
            }
            return
        if data is None or data["owner"] != owner:
            return
        data["updated"] = time.time()
        if kind == "UserPromptSubmit":
            if payload.get("source") not in (None, "user"):
                return
            if not valid_id(prompt):
                data.update(job=None, disabled=True)
                return
            job = data.get("job")
            text = payload.get("prompt")
            if (
                job and job["phase"] == "awaiting"
                and isinstance(job.get("token"), str)
                and isinstance(text, str)
                and FEEDBACK + " [tclaude-retry:" + job["token"] + "]" in text
            ):
                data.update(prompt=prompt, status="active", job=None)
                return
            data.update(prompt=prompt, status="active", attempts=0, disabled=False, job=None)
        elif kind == "SessionEnd":
            data.update(status="closed", job=None)
        elif kind == "Stop":
            data.update(status="idle", attempts=0, disabled=False, job=None)


def failure_hook(payload, root, tmp, owner):
    session = payload["session_id"]
    failure = final_429(payload)
    with state(root, session) as box:
        seen = set(box[0]["seen"]) if box[0] and box[0].get("owner") == owner else set()
    deadline = time.monotonic() + 3
    while payload.get("error") == "rate_limit" and (failure is None or failure in seen) and time.monotonic() < deadline:
        if owner_info(owner["pid"]) != owner:
            return 0
        time.sleep(0.05)
        failure = final_429(payload)
    if failure in seen:
        return 0
    if failure is None:
        with state(root, session) as box:
            if box[0] is not None and box[0]["owner"] == owner:
                box[0].update(status="idle", job=None, attempts=0)
        return 0
    prompt = payload.get("prompt_id")
    if not isinstance(prompt, str) or not valid_id(prompt):
        return 0
    request = {
        "session": session, "failure": failure, "prompt": prompt, "owner": owner,
        "transcript": payload.get("transcript_path"),
    }
    # No listening watcher means no failure registration and no historical retry.
    address = socket_path(root, tmp)
    try:
        info = address.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return 0
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3500)
            connection.connect(str(address))
            peer = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer[1] != os.getuid():
                return 0
            connection.sendall(json.dumps(request).encode() + b"\n")
            raw = bytearray()
            while b"\n" not in raw and len(raw) <= 4096:
                chunk = connection.recv(4096)
                if not chunk:
                    return 0
                raw.extend(chunk)
            response = json.loads(raw)
            if response != {"approve": failure}:
                return 0
            with state(root, session) as box:
                data = box[0]
                if data is None or data["owner"] != owner or data["prompt"] != prompt:
                    return 0
                job = data.get("job")
                if not job or job.get("failure") != failure or job.get("phase") != "dispatching":
                    return 0
                if owner_info(owner["pid"]) != owner or final_429(payload) != failure:
                    data.update(job=None, disabled=True)
                    return 0
                data["job"]["phase"] = "awaiting"
                data["job"]["due"] = time.time() + job["observe_timeout"]
            print(FEEDBACK + " [tclaude-retry:" + job["token"] + "]", file=sys.stderr, flush=True)
            return 2
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("agent_id") or not valid_id(payload.get("session_id")):
            return 0
        owner = ancestor_owner()
        if owner is None:
            return 0
        if payload.get("hook_event_name") == "StopFailure" and not supported_owner(owner):
            return 0
        root = root_path()
        tmp = Path(os.environ.get("TMPDIR", DEFAULT_TMP)).resolve()
        if tmp == Path("/tmp") or Path("/tmp") in tmp.parents:
            return 0
        if payload.get("hook_event_name") == "StopFailure":
            return failure_hook(payload, root, tmp, owner)
        lifecycle(payload, root, owner)
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
