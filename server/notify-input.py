#!/usr/bin/env python3
"""Advisory PreToolUse reminder for request_user_input; JSON arrives on stdin."""

import argparse
import json
import sys
import uuid

sys.dont_write_bytecode = True
from notify_queue import DEFAULT_CHANNEL, NotificationQueue, validate_channel

MAX_INPUT_BYTES = 1024 * 1024


def input_event(payload):
    if (
        not isinstance(payload, dict)
        or payload.get("hook_event_name") != "PreToolUse"
        or payload.get("tool_name") != "request_user_input"
    ):
        return None
    tool_input = payload.get("tool_input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        return None
    for question in questions:
        if not isinstance(question, dict) or not all(
            isinstance(question.get(field), str) and question[field].strip()
            for field in ("id", "header", "question")
        ):
            return None
    identifiers = [payload.get(field) for field in ("session_id", "turn_id", "tool_use_id")]
    if not all(isinstance(value, str) and 0 < len(value) <= 1024 for value in identifiers):
        return None
    return dict(zip(
        ("type", "thread-id", "turn-id", "tool-use-id"),
        ("user-input-requested", *identifiers),
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, type=validate_channel)
    parser.add_argument("--test", action="store_true", help="Write a synthetic question reminder.")
    args = parser.parse_args()
    queue = None
    try:
        if args.test:
            event = {
                "type": "user-input-requested",
                "thread-id": "notification-test",
                "turn-id": uuid.uuid4().hex,
                "tool-use-id": uuid.uuid4().hex,
            }
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                return 0
            event = input_event(json.loads(raw))
            if event is None:
                return 0
        queue = NotificationQueue(args.channel)
        inserted = queue.enqueue(event)
        if args.test:
            print("Test question reminder queued." if inserted else "Test question reminder already queued.")
        return 0
    except (OSError, ValueError, RuntimeError):
        print("TCodex question reminder could not be queued; check the workspace runtime.", file=sys.stderr)
        return 1 if args.test else 0
    finally:
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
