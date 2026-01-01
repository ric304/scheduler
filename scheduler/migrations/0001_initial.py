# Generated manually for M0 scaffold

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="SchedulerSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=128, unique=True)),
                ("value_json", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "scheduler_settings"},
        ),
        migrations.CreateModel(
            name="AdminActionLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("actor", models.CharField(blank=True, max_length=150)),
                ("action", models.CharField(max_length=128)),
                ("target", models.CharField(blank=True, max_length=256)),
                ("payload_json", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "scheduler_admin_action_logs",
                "indexes": [
                    models.Index(fields=["created_at"], name="sched_audit_created_at"),
                    models.Index(fields=["action"], name="sched_audit_action"),
                ],
            },
        ),
    ]
