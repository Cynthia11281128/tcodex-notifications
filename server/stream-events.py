#!/usr/bin/env python3
"""SSH stdout protocol: READY <cursor>, then EVENT or INPUT <id> <time> <session>."""

import argparse
import sys
import time

sys.dont_write_bytecode = True
from notify_queue import DEFAULT_CHANNEL, NotificationQueue, validate_channel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, type=validate_channel)
    parser.add_argument("--after", type=int, help="Reconnect cursor; default skips old events.")
    parser.add_argument("--once", action="store_true", help="Read one batch and exit.")
    args = parser.parse_args()
    if args.after is not None and args.after < 0:
        parser.error("--after must not be negative")
    queue = None
    try:
        queue = NotificationQueue(args.channel)
        queue.prune()
        watermark = queue.high_water()
        cursor = watermark if args.after is None else min(args.after, watermark)
        print("READY\t{}".format(cursor), flush=True)
        last_prune = time.monotonic()
        while True:
            for row in queue.read_after(cursor):
                event_id, created, session = row[:3]
                kind = "INPUT" if len(row) == 4 else "EVENT"
                print("{}\t{}\t{}\t{}".format(kind, event_id, created, session), flush=True)
                cursor = event_id
            if args.once:
                return 0
            if time.monotonic() - last_prune >= 60:
                queue.prune()
                last_prune = time.monotonic()
            time.sleep(1)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    except (OSError, ValueError):
        print("Notification stream unavailable; check the workspace runtime.", file=sys.stderr)
        return 1
    finally:
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
