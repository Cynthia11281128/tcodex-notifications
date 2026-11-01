#!/usr/bin/env python3
"""Advisory TClaude completion and AskUserQuestion hooks."""

import argparse
import json
import sys
import uuid

sys.dont_write_bytecode = True
from notify_queue import NotificationQueue, validate_channel

MAX_INPUT = 1024 * 1024


def normalize(payload):
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    session = payload.get("session_id")
    if not isinstance(session, str) or not 0 < len(session) <= 1024:
        return None
    kind = payload.get("hook_event_name")
    if kind == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion":
        call = payload.get("tool_use_id")
        questions = payload.get("tool_input", {}).get("questions") if isinstance(
            payload.get("tool_input"), dict
        ) else None
        if not isinstance(call, str) or not 0 < len(call) <= 1024:
            return None
        if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
            return None
        if any(not isinstance(q, dict) or any(
            not isinstance(q.get(field), str) or not q[field].strip()
            for field in ("header", "question")
        ) for q in questions):
            return None
        return {
            "type": "user-input-requested", "thread-id": "tclaude:" + session,
            "turn-id": "tclaude", "tool-use-id": call,
        }
    if kind not in ("Stop", "StopFailure"):
        return None
    prompt = payload.get("prompt_id")
    if not isinstance(prompt, str) or not 0 < len(prompt) <= 1024:
        return None
    return {
        "type": "agent-turn-complete", "thread-id": "tclaude:" + session,
        "turn-id": prompt,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=validate_channel, default="default")
    parser.add_argument("--test", choices=("complete", "input"))
    args = parser.parse_args()
    queue = None
    try:
        if args.test:
            event = {
                "type": "agent-turn-complete" if args.test == "complete" else "user-input-requested",
                "thread-id": "tclaude:test", "turn-id": uuid.uuid4().hex,
                "tool-use-id": uuid.uuid4().hex,
            }
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                return 0
            event = normalize(json.loads(raw))
        if event is not None:
            queue = NotificationQueue(args.channel)
            queue.enqueue(event)
        if args.test:
            print("TClaude test event queued.")
        return 0
    except (OSError, ValueError, RuntimeError):
        print("TClaude notification could not be queued.", file=sys.stderr)
        return 1 if args.test else 0
    finally:
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
