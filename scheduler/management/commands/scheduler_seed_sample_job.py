from __future__ import annotations

from django.core.management.base import BaseCommand

from scheduler.models import JobDefinition


class Command(BaseCommand):
    help = "Create or update a single sample JobDefinition for local development."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            default="Sample: Every 5 minutes",
            help="JobDefinition.name to create/update",
        )
        parser.add_argument(
            "--enabled",
            action="store_true",
            default=True,
            help="Set enabled=true (default)",
        )
        parser.add_argument(
            "--disabled",
            action="store_true",
            default=False,
            help="Set enabled=false",
        )

    def handle(self, *args, **options):
        name: str = options["name"]
        enabled = bool(options["enabled"]) and not bool(options["disabled"])

        job_def, created = JobDefinition.objects.update_or_create(
            name=name,
            defaults={
                "enabled": enabled,
                "type": JobDefinition.JobType.TIME,
                "command_name": "scheduler_sample_job",
                "default_args_json": {},
                "schedule": {"kind": "every_n_minutes", "n": 5},
                "timeout_seconds": 60,
                "max_retries": 3,
                "retry_backoff_seconds": 5,
                "concurrency_policy": JobDefinition.ConcurrencyPolicy.FORBID,
            },
        )

        self.stdout.write(
            ("created" if created else "updated")
            + f" JobDefinition id={job_def.id} name={job_def.name!r} enabled={job_def.enabled}"
        )
