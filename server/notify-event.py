#!/usr/bin/env python3
"""Codex notify hook: persist a minimal event, without failing the task."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

sys.dont_write_bytecode = True
from notify_queue import DEFAULT_CHANNEL, NotificationQueue, validate_channel


def is_primary_cli_thread(thread, home=None):
    if not isinstance(thread, str) or not 0 < len(thread) <= 1024:
        return False
    home = Path(home or os.environ.get("CODEX_HOME") or "~/.tcodex").expanduser().resolve()
    uri = (home / "state_5.sqlite").as_uri() + "?mode=ro"
    for attempt in range(3):
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as conn:
                row = conn.execute(
                    "SELECT source FROM threads WHERE id=?", (thread,)
                ).fetchone()
            # Title and recap tasks are ephemeral; subagents have a different source.
            return row is not None and row[0] == "cli"
        except sqlite3.Error:
            if attempt == 2:
                raise RuntimeError("Codex session state unavailable.") from None
            time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, type=validate_channel)
    parser.add_argument("--test", action="store_true", help="Write a synthetic test event.")
    parser.add_argument("event", nargs="?", help="Single JSON argument supplied by Codex.")
    args = parser.parse_args()
    queue = None
    try:
        if args.test:
            payload = {
                "type": "agent-turn-complete",
                "thread-id": "notification-test",
                "turn-id": uuid.uuid4().hex,
            }
        else:
            payload = json.loads(args.event or "{}")
        if not isinstance(payload, dict) or payload.get("type") != "agent-turn-complete":
            return 0
        if not args.test:
            turn = payload.get("turn-id")
            if not isinstance(turn, str) or not 0 < len(turn) <= 1024:
                return 0
            if not is_primary_cli_thread(payload.get("thread-id")):
                return 0
        queue = NotificationQueue(args.channel)
        inserted = queue.enqueue(payload)
        if args.test:
            print("Test event queued." if inserted else "Test event already queued.")
        return 0
    except (OSError, ValueError, RuntimeError):
        # Do not log payloads, credentials, or raw exception text.
        print(
            "TCodex notification could not be queued; check CODEX_HOME/state_5.sqlite "
            "and the workspace runtime.",
            file=sys.stderr,
        )
        return 1 if args.test else 0
    finally:
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
