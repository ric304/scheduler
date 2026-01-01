from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "A sample long-running job for local development (sleeps > 60 seconds)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sleep-seconds",
            type=int,
            default=None,
            help="Override sleep duration in seconds (default: 65).",
        )
        parser.add_argument(
            "--progress-interval-seconds",
            type=int,
            default=10,
            help="Progress log interval in seconds (default: 10).",
        )

    def handle(self, *args, **options):
        sleep_seconds = options.get("sleep_seconds")
        if sleep_seconds is None:
            sleep_seconds = 65

        # Allow override via the scheduler runtime environment variable.
        raw = os.environ.get("SCHEDULER_ARGS_JSON", "")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "sleep_seconds" in parsed:
                    sleep_seconds = int(parsed.get("sleep_seconds") or sleep_seconds)
            except Exception:
                pass

        progress_interval = int(options.get("progress_interval_seconds") or 10)
        progress_interval = max(1, progress_interval)

        started = datetime.now(timezone.utc)
        self.stdout.write(
            f"scheduler_sample_long_job start utc={started.isoformat()} sleep_seconds={sleep_seconds}"
        )

        deadline = time.time() + max(0, int(sleep_seconds))
        next_log = time.time() + progress_interval
        while True:
            now = time.time()
            if now >= deadline:
                break
            if now >= next_log:
                remaining = max(0, int(deadline - now))
                self.stdout.write(f"scheduler_sample_long_job progress remaining_seconds={remaining}")
                next_log = now + progress_interval
            time.sleep(0.5)

        finished = datetime.now(timezone.utc)
        self.stdout.write(f"scheduler_sample_long_job done utc={finished.isoformat()}")
