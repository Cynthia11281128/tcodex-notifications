#!/usr/bin/env python3
"""Codex notify hook: persist a minimal event, without failing the task."""

import argparse
import json
import sys
import uuid

sys.dont_write_bytecode = True
from notify_queue import DEFAULT_CHANNEL, NotificationQueue, validate_channel


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
        queue = NotificationQueue(args.channel)
        inserted = queue.enqueue(payload)
        if args.test:
            print("Test event queued." if inserted else "Test event already queued.")
        return 0
    except (OSError, ValueError, RuntimeError):
        # Do not log payloads, credentials, or raw exception text.
        print("TCodex notification could not be queued; check the workspace runtime.", file=sys.stderr)
        return 1 if args.test else 0
    finally:
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
