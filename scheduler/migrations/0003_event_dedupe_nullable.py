# Generated manually to relax Event.dedupe_key uniqueness

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scheduler", "0002_core_models"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="event",
            name="sched_event_unique_dedupe",
        ),
        migrations.AlterField(
            model_name="event",
            name="dedupe_key",
            field=models.CharField(blank=True, max_length=256, null=True),
        ),
    ]
