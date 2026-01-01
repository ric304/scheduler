from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0006_setting_help_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobrun",
            name="resource_cpu_seconds_total",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="jobrun",
            name="resource_peak_rss_bytes",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="jobrun",
            name="resource_io_read_bytes",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="jobrun",
            name="resource_io_write_bytes",
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
