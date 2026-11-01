#!/usr/bin/env python3
"""Explicitly supervise registered TClaude sessions through native failure hooks."""

import argparse
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import sys
import time
import uuid

sys.dont_write_bytecode = True
from notify_queue import private_directory, private_file
from tclaude_retry import (
    DEFAULT_ROOT, DEFAULT_TMP, ensure_root, final_429, owner_info,
    socket_path, state, supported_owner, valid_id,
)

LOG = logging.getLogger("tclaude-watch")


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 300:
        raise argparse.ArgumentTypeError("Use a positive finite number up to 300.")
    return number


def session_id(value):
    try:
        if valid_id(value):
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Use a canonical session UUID.")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    scope = result.add_mutually_exclusive_group(required=True)
    scope.add_argument("--session", type=session_id, action="append")
    scope.add_argument("--all-registered", action="store_true")
    result.add_argument("--runtime-dir", type=Path, default=DEFAULT_ROOT)
    result.add_argument("--tmp-dir", type=Path, default=DEFAULT_TMP)
    result.add_argument("--initial-delay", type=positive, default=3)
    result.add_argument("--max-delay", type=positive, default=3)
    result.add_argument("--observe-timeout", type=positive, default=30)
    result.add_argument("--max-retries", type=int, default=5)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--once", action="store_true")
    return result


def failure_payload(request):
    return {
        "hook_event_name": "StopFailure", "error": "rate_limit",
        "session_id": request["session"], "transcript_path": request["transcript"],
    }


class Watcher:
    def __init__(self, args):
        self.args = args
        self.root = args.runtime_dir
        self.selected = set(args.session or [])
        self.selector = selectors.DefaultSelector()
        self.clients = {}
        self.snapshot = None
        self.dry_root = self.root / "dry-run"

    def reply(self, connection, answer=None):
        self.clients.pop(connection, None)
        try:
            self.selector.unregister(connection)
        except (KeyError, ValueError):
            pass
        try:
            connection.sendall(json.dumps(answer or {"deny": True}).encode() + b"\n")
        except OSError:
            pass
        connection.close()

    def register(self, connection, request, now):
        if not isinstance(request, dict) or not all(
            key in request for key in ("session", "failure", "prompt", "owner", "transcript")
        ):
            return False
        if not all(valid_id(request[key]) for key in ("session", "failure", "prompt")):
            return False
        if self.selected and request["session"] not in self.selected:
            return False
        owner = request["owner"]
        if not supported_owner(owner) or owner_info(owner.get("pid")) != owner:
            return False
        if final_429(failure_payload(request)) != request["failure"]:
            return False
        with state(self.root, request["session"]) as box:
            data = box[0]
            if not data or data.get("owner") != owner or data.get("prompt") != request["prompt"]:
                return False
            if data.get("disabled") or data.get("status") == "closed":
                return False
            if self.args.dry_run:
                ensure_root(self.dry_root)
                with state(self.dry_root, request["session"]) as dry:
                    if dry[0] is None:
                        dry[0] = {"version": 1, "session": request["session"], "seen": []}
                    if request["failure"] not in dry[0]["seen"]:
                        dry[0]["seen"] = (dry[0]["seen"] + [request["failure"]])[-256:]
                        LOG.info("DRY RUN: would continue session %s.", request["session"])
                return False
            if request["failure"] in data["seen"]:
                return False
            # A new failure proves that a prior native rewake was observed.
            if data.get("job") and data["job"]["phase"] == "dispatching":
                data.update(job=None, disabled=True)
                return False
            data["seen"] = (data["seen"] + [request["failure"]])[-256:]
            if data["attempts"] >= self.args.max_retries:
                data.update(job=None, disabled=True)
                LOG.warning("Session %s reached retry cap.", request["session"])
                return False
            delay = min(self.args.initial_delay * 2 ** data["attempts"], self.args.max_delay)
            data.update(status="failed", job={
                "phase": "cooldown", "failure": request["failure"],
                "due": now + delay, "transcript": request["transcript"],
                "observe_timeout": self.args.observe_timeout,
            })
        self.clients[connection]["request"] = request
        LOG.info("Session %s exhausted HTTP 429; scheduled in %ss.", request["session"], delay)
        return True

    def dispatch(self, connection, request, now):
        with state(self.root, request["session"]) as box:
            data = box[0]
            job = data.get("job") if data else None
            if not data or data.get("owner") != request["owner"] or data.get("prompt") != request["prompt"]:
                self.reply(connection)
                return
            if not job or job["phase"] != "cooldown" or job["failure"] != request["failure"]:
                self.reply(connection)
                return
            if owner_info(request["owner"]["pid"]) != request["owner"] or final_429(
                failure_payload(request)
            ) != request["failure"]:
                data.update(job=None)
                self.reply(connection)
                return
            if now < job["due"]:
                return
            data["attempts"] += 1
            job["phase"] = "dispatching"
            job["due"] = now + self.args.observe_timeout
            job["token"] = uuid.uuid4().hex
        # Reservation is fsynced before the hook can wake the original process.
        self.reply(connection, {"approve": request["failure"]})
        LOG.info("Approved native continuation for %s.", request["session"])

    def inventory(self, now):
        pending_failures = {entry["request"]["failure"] for entry in self.clients.values()
                            if entry.get("request")}
        live = []
        for path in sorted(self.root.glob("*.json")):
            try:
                sid = session_id(path.stem)
                if self.selected and sid not in self.selected:
                    continue
                with state(self.root, sid) as box:
                    data = box[0]
                    if not data:
                        continue
                    owner = data.get("owner")
                    active = isinstance(owner, dict) and owner_info(owner.get("pid")) == owner
                    if active and data.get("status") != "closed":
                        live.append(sid)
                        if not supported_owner(owner):
                            LOG.warning("Session %s runtime is not verified; no automatic continuation.", sid)
                    if self.args.dry_run:
                        continue
                    job = data.get("job")
                    if job and job["phase"] == "cooldown" and job["failure"] not in pending_failures:
                        data["job"] = None
                        continue
                    if job and job["phase"] in ("dispatching", "awaiting"):
                        if job["phase"] == "dispatching" and now >= job["due"]:
                            data.update(job=None, disabled=True)
                        elif not active or now >= job["due"]:
                            data.update(job=None, disabled=True)
            except (OSError, ValueError, KeyError, TypeError):
                LOG.warning("Invalid or unavailable session checkpoint; skipped.")
        snapshot = tuple(live)
        if snapshot != self.snapshot:
            LOG.info("Live registered sessions: %s.", len(live))
            for sid in live:
                LOG.info("Registered live session %s.", sid)
            self.snapshot = snapshot

    def serve(self, listener):
        self.selector.register(listener, selectors.EVENT_READ)
        last_inventory = 0
        try:
            while True:
                now = time.time()
                if now - last_inventory >= 1:
                    self.inventory(now)
                    last_inventory = now
                if self.args.once:
                    return
                for key, _mask in self.selector.select(0.1):
                    if key.fileobj is listener:
                        connection, _ = listener.accept()
                        peer = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        if peer[1] != os.getuid() or len(self.clients) >= 256:
                            connection.close()
                            continue
                        connection.setblocking(False)
                        self.clients[connection] = {"raw": bytearray(), "deadline": now + 5}
                        self.selector.register(connection, selectors.EVENT_READ)
                    else:
                        connection = key.fileobj
                        entry = self.clients[connection]
                        try:
                            raw = connection.recv(4096)
                            if not raw or entry.get("request"):
                                self.reply(connection)
                                continue
                            entry["raw"].extend(raw)
                            if len(entry["raw"]) > 8192:
                                self.reply(connection)
                            elif b"\n" in entry["raw"]:
                                request = json.loads(entry["raw"])
                                if not self.register(connection, request, now):
                                    self.reply(connection)
                        except (OSError, ValueError, KeyError, TypeError):
                            self.reply(connection)
                for connection, entry in list(self.clients.items()):
                    request = entry.get("request")
                    if request:
                        try:
                            self.dispatch(connection, request, time.time())
                        except (OSError, ValueError, KeyError, TypeError):
                            self.reply(connection)
                    elif now >= entry["deadline"]:
                        self.reply(connection)
        finally:
            for connection in list(self.clients):
                self.reply(connection)
            self.selector.close()


def main():
    arg_parser = parser()
    args = arg_parser.parse_args()
    args.runtime_dir = args.runtime_dir.expanduser().resolve()
    args.tmp_dir = args.tmp_dir.expanduser().resolve()
    if args.tmp_dir == Path("/tmp") or Path("/tmp") in args.tmp_dir.parents:
        arg_parser.error("--tmp-dir must not be under /tmp")
    if args.max_delay < args.initial_delay or not 1 <= args.max_retries <= 100:
        arg_parser.error("Invalid delay or retry limit.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_root(args.runtime_dir)
    args.tmp_dir.mkdir(parents=True, exist_ok=True)
    address = socket_path(args.runtime_dir, args.tmp_dir)
    private_directory(address.parent)
    lock_fd = private_file(address.parent / (address.stem + ".lock"), os.O_CREAT | os.O_RDWR)
    listener = socket.socket(socket.AF_UNIX)
    owns_lock = False

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owns_lock = True
        if address.exists():
            address.unlink()
        listener.bind(str(address))
        os.chmod(address, 0o600)
        listener.listen(64)
        LOG.info("TClaude watcher%s listening; only newly submitted failures are eligible.",
                 " (dry-run)" if args.dry_run else "")
        Watcher(args).serve(listener)
        return 0
    except KeyboardInterrupt:
        LOG.info("Watcher stopped; pending hooks will not continue.")
        return 0
    except (OSError, ValueError):
        LOG.error("Watcher unavailable; another watcher may own this runtime.")
        return 1
    finally:
        listener.close()
        if owns_lock:
            address.unlink(missing_ok=True)
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
