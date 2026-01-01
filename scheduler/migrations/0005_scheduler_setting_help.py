from __future__ import annotations

from django.db import migrations, models


def seed_setting_help(apps, schema_editor):
    SchedulerSettingHelp = apps.get_model("scheduler", "SchedulerSettingHelp")

    rows = [
        SchedulerSettingHelp(
            key="SCHEDULER_DEPLOYMENT",
            title="Deployment mode",
            description="実行環境モード。ローカル検証用とK8S想定で挙動を切り替えるために使います。",
            impact="Workerのログ取り扱い/表示など、一部の運用挙動が変わります。",
            editable=True,
            input_type="enum",
            enum_values_json=["local", "k8s"],
            is_secret=False,
        ),
        SchedulerSettingHelp(
            key="SCHEDULER_LOG_ARCHIVE_ENABLED",
            title="Archive job logs",
            description="ジョブログをS3互換ストレージ（MinIO等）へアップロードします。",
            impact="有効にするとアップロード処理が走り、log_ref がオブジェクトストレージ参照になります。",
            editable=True,
            input_type="bool",
            enum_values_json=[],
            is_secret=False,
        ),
        SchedulerSettingHelp(
            key="SCHEDULER_LOG_LOCAL_DELETE_AFTER_UPLOAD",
            title="Delete local log after upload",
            description="アップロード成功時にローカルログファイルを削除します。",
            impact="ローカルディスク使用量を抑えられますが、S3側が読めない場合に復旧できません。",
            editable=True,
            input_type="bool",
            enum_values_json=[],
            is_secret=False,
        ),
        SchedulerSettingHelp(
            key="SCHEDULER_GRPC_HOST",
            title="gRPC bind host",
            description="WorkerがgRPCサーバをbindするホスト（インターフェース）。",
            impact="誤るとLeaderがWorkerへ到達できません。通常は 0.0.0.0 または 127.0.0.1 を使います。",
            editable=True,
            input_type="text",
            enum_values_json=[],
            is_secret=False,
        ),
        SchedulerSettingHelp(
            key="SCHEDULER_GRPC_PORT_RANGE_START",
            title="gRPC port range start",
            description="Workerが自動選択するgRPCポート範囲の開始。",
            impact="同一ホストで複数Workerを立てる場合、衝突しない範囲を確保します。",
            editable=True,
            input_type="text",
            enum_values_json=[],
            is_secret=False,
        ),
        SchedulerSettingHelp(
            key="SCHEDULER_GRPC_PORT_RANGE_END",
            title="gRPC port range end",
            description="Workerが自動選択するgRPCポート範囲の終了。",
            impact="開始〜終了までで空きが無いとWorker起動に失敗します。",
            editable=True,
            input_type="text",
            enum_values_json=[],
            is_secret=False,
        ),
        SchedulerSettingHelp(
            key="SCHEDULER_EVENTS_API_TOKEN",
            title="Events ingest token",
            description="/api/events/ingest/ の認証トークン。",
            impact="漏洩すると外部からイベント投入されJobRun生成され得ます。必ず秘匿してローテーション可能にしてください。",
            editable=True,
            input_type="text",
            enum_values_json=[],
            is_secret=True,
        ),
    ]

    existing = set(SchedulerSettingHelp.objects.values_list("key", flat=True))
    SchedulerSettingHelp.objects.bulk_create([r for r in rows if r.key not in existing])


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0004_config_reload_request"),
    ]

    operations = [
        migrations.CreateModel(
            name="SchedulerSettingHelp",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=128, unique=True)),
                ("title", models.CharField(blank=True, max_length=200)),
                ("description", models.TextField(blank=True)),
                ("impact", models.TextField(blank=True)),
                ("editable", models.BooleanField(default=True)),
                (
                    "input_type",
                    models.CharField(
                        choices=[("text", "text"), ("bool", "bool"), ("enum", "enum")],
                        default="text",
                        max_length=16,
                    ),
                ),
                ("enum_values_json", models.JSONField(blank=True, default=list)),
                ("is_secret", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "scheduler_setting_help",
            },
        ),
        migrations.RunPython(seed_setting_help, migrations.RunPython.noop),
    ]
