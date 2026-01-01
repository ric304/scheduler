from __future__ import annotations

from datetime import datetime, timezone

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "A tiny sample job for local development (prints current UTC timestamp)."

    def handle(self, *args, **options):
        self.stdout.write(f"scheduler_sample_job utc={datetime.now(timezone.utc).isoformat()}")
