from __future__ import annotations

from django.core.management.base import BaseCommand

from scheduler.models import JobDefinition


class Command(BaseCommand):
    help = "Create or update a sample JobDefinition that generates CPU/IO load for testing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            default="Sample: CPU+IO Load (Every 5 minutes)",
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
                "command_name": "scheduler_sample_resource_job",
                "default_args_json": {
                    "cpu_seconds": 5,
                    "io_write_mb": 50,
                    "io_read_mb": 50,
                    "chunk_kb": 256,
                },
                "schedule": {"kind": "every_n_minutes", "n": 5},
                "timeout_seconds": 600,
                "max_retries": 1,
                "retry_backoff_seconds": 5,
                "concurrency_policy": JobDefinition.ConcurrencyPolicy.FORBID,
            },
        )

        self.stdout.write(
            ("created" if created else "updated")
            + f" JobDefinition id={job_def.id} name={job_def.name!r} enabled={job_def.enabled}"
        )
