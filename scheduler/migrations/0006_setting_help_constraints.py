from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0005_scheduler_setting_help"),
    ]

    operations = [
        migrations.AddField(
            model_name="schedulersettinghelp",
            name="constraints_json",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="schedulersettinghelp",
            name="examples_json",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
