from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scheduler", "0003_event_dedupe_nullable"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConfigReloadRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("requested_by", models.CharField(blank=True, max_length=150)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("PENDING", "PENDING"), ("APPLIED", "APPLIED"), ("FAILED", "FAILED")],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("leader_worker_id", models.CharField(blank=True, max_length=128)),
                ("leader_epoch", models.BigIntegerField(blank=True, null=True)),
                ("result_json", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "db_table": "scheduler_config_reload_requests",
            },
        ),
        migrations.AddIndex(
            model_name="configreloadrequest",
            index=models.Index(fields=["status", "requested_at"], name="sched_reload_status_req"),
        ),
        migrations.AddIndex(
            model_name="configreloadrequest",
            index=models.Index(fields=["requested_at"], name="sched_reload_requested_at"),
        ),
    ]
