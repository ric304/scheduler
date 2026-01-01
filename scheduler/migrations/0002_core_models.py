# Generated manually for M1 (core DB models)

from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("scheduler", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="JobDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                ("enabled", models.BooleanField(default=True)),
                ("type", models.CharField(choices=[("time", "time"), ("event", "event")], max_length=16)),
                ("command_name", models.CharField(max_length=200)),
                ("default_args_json", models.JSONField(blank=True, default=dict)),
                ("schedule", models.JSONField(blank=True, default=dict)),
                ("timeout_seconds", models.IntegerField(default=0)),
                ("max_retries", models.IntegerField(default=0)),
                ("retry_backoff_seconds", models.IntegerField(default=0)),
                (
                    "concurrency_policy",
                    models.CharField(
                        choices=[("forbid", "forbid"), ("allow", "allow"), ("replace", "replace")],
                        default="forbid",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "scheduler_job_definitions",
                "indexes": [
                    models.Index(fields=["enabled"], name="sched_jobdef_enabled"),
                    models.Index(fields=["type"], name="sched_jobdef_type"),
                ],
            },
        ),
        migrations.CreateModel(
            name="Event",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(max_length=128)),
                ("payload_json", models.JSONField(blank=True, default=dict)),
                ("dedupe_key", models.CharField(blank=True, max_length=256)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "scheduler_events",
                "indexes": [
                    models.Index(fields=["processed_at", "created_at"], name="sched_event_proc_created"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.UniqueConstraint(fields=("dedupe_key",), name="sched_event_unique_dedupe"),
        ),
        migrations.CreateModel(
            name="JobRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scheduled_for", models.DateTimeField(blank=True, null=True)),
                ("assigned_at", models.DateTimeField(blank=True, null=True)),
                ("assigned_worker_id", models.CharField(blank=True, max_length=128)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("PENDING", "PENDING"),
                            ("ASSIGNED", "ASSIGNED"),
                            ("RUNNING", "RUNNING"),
                            ("SUCCEEDED", "SUCCEEDED"),
                            ("FAILED", "FAILED"),
                            ("CANCELED", "CANCELED"),
                            ("TIMED_OUT", "TIMED_OUT"),
                            ("ORPHANED", "ORPHANED"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("attempt", models.IntegerField(default=0)),
                ("version", models.IntegerField(default=0)),
                ("leader_epoch", models.BigIntegerField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("exit_code", models.IntegerField(blank=True, null=True)),
                ("error_summary", models.TextField(blank=True)),
                ("log_ref", models.CharField(blank=True, max_length=512)),
                ("idempotency_key", models.CharField(blank=True, max_length=256)),
                (
                    "continuation_state",
                    models.CharField(
                        choices=[("NONE", "NONE"), ("CONFIRMING", "CONFIRMING")],
                        default="NONE",
                        max_length=16,
                    ),
                ),
                ("continuation_check_started_at", models.DateTimeField(blank=True, null=True)),
                ("continuation_check_deadline_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "job_definition",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="scheduler.jobdefinition"),
                ),
            ],
            options={
                "db_table": "scheduler_job_runs",
                "indexes": [
                    models.Index(fields=["state", "scheduled_for"], name="sched_jobrun_state_scheduled"),
                    models.Index(fields=["assigned_worker_id", "state"], name="sched_jobrun_worker_state"),
                    models.Index(fields=["created_at"], name="sched_jobrun_created_at"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="jobrun",
            constraint=models.UniqueConstraint(fields=("job_definition", "scheduled_for"), name="sched_jobrun_unique_schedule"),
        ),
    ]
