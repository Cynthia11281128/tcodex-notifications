#!/usr/bin/env python3
"""TCodex notify hook: display a local macOS notification."""

import argparse
import json
import subprocess
import sys

sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Send a local test notification.")
    parser.add_argument("event", nargs="?", help="Single JSON argument supplied by TCodex.")
    args = parser.parse_args()
    try:
        payload = {"type": "agent-turn-complete"} if args.test else json.loads(args.event or "{}")
        if not isinstance(payload, dict) or payload.get("type") != "agent-turn-complete":
            return 0
        if sys.platform != "darwin":
            raise OSError("macOS is required")
        message = "TCodex local notification test" if args.test else "TCodex turn ended (local)"
        subprocess.run(
            [
                "osascript",
                "-e", "on run argv",
                "-e", 'display notification (item 1 of argv) with title "TCodex" sound name "Glass"',
                "-e", "end run",
                message,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
        if args.test:
            print("Local notification request sent. This does not confirm macOS displayed it.")
        return 0
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        print("TCodex local notification failed; check macOS and osascript.", file=sys.stderr)
        return 1 if args.test else 0


if __name__ == "__main__":
    raise SystemExit(main())
