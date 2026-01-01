#!/usr/bin/env python3
"""Post an event to /api/events/ingest/.

Examples:
  python scripts/ingest_event.py \
    --url http://localhost:8000/api/events/ingest/ \
    --event-type demo.user.signup \
    --payload '{"user_id":123}' \
    --dedupe-key demo-123 \
    --token "$SCHEDULER_EVENTS_API_TOKEN"

If --token is omitted, the API requires an authenticated Django session.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--event-type", required=True)
    parser.add_argument("--payload", default="{}")
    parser.add_argument("--dedupe-key", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    try:
        payload_json = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"Invalid --payload JSON: {e}", file=sys.stderr)
        return 2

    body = json.dumps(
        {
            "event_type": args.event_type,
            "payload_json": payload_json,
            "dedupe_key": args.dedupe_key,
        }
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-Scheduler-Token"] = args.token

    req = urllib.request.Request(args.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read()
            sys.stdout.buffer.write(resp_body)
            if resp_body and not resp_body.endswith(b"\n"):
                sys.stdout.write("\n")
            return 0
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}\n{detail}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Request failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
