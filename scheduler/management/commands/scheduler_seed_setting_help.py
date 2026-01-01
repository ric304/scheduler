from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from scheduler.conf import list_all_scheduler_setting_keys
from scheduler.models import SchedulerSettingHelp


def _is_secret_key(key: str) -> bool:
    u = str(key or "").upper()
    return ("SECRET" in u) or ("TOKEN" in u) or ("PASSWORD" in u)


class Command(BaseCommand):
    help = "Ensure SchedulerSettingHelp rows exist for all SCHEDULER_* keys."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only show what would be created.",
        )
        parser.add_argument(
            "--apply-defaults",
            action="store_true",
            help="Upsert bundled help/constraints/examples for known keys.",
        )

    def handle(self, *args, **options):
        keys = list_all_scheduler_setting_keys(fresh=True)
        existing = set(SchedulerSettingHelp.objects.values_list("key", flat=True))
        missing = [k for k in keys if k not in existing]

        if options.get("dry_run"):
            self.stdout.write(json.dumps({"missing": missing, "count": len(missing)}, ensure_ascii=False))
            return

        if options.get("apply_defaults"):
            updated = 0
            created = 0
            for k, spec in DEFAULT_HELP.items():
                if not k.startswith("SCHEDULER_"):
                    continue
                if k in {"SCHEDULER_NODE_ID"}:
                    continue
                defaults = {
                    "title": str(spec.get("title") or ""),
                    "description": str(spec.get("description") or ""),
                    "impact": str(spec.get("impact") or ""),
                    "editable": bool(spec.get("editable", True)),
                    "input_type": str(spec.get("input_type") or "text"),
                    "enum_values_json": list(spec.get("enum_values") or []),
                    "constraints_json": dict(spec.get("constraints") or {}),
                    "examples_json": list(spec.get("examples") or []),
                    "is_secret": _is_secret_key(k) or bool(spec.get("is_secret", False)),
                }
                _, was_created = SchedulerSettingHelp.objects.update_or_create(key=k, defaults=defaults)
                created += 1 if was_created else 0
                updated += 0 if was_created else 1
            self.stdout.write(self.style.SUCCESS(f"help_defaults: created={created} updated={updated}"))

        # Create any remaining missing keys as blank placeholders.
        existing = set(SchedulerSettingHelp.objects.values_list("key", flat=True))
        missing = [k for k in keys if k not in existing]
        rows = []
        for k in missing:
            rows.append(
                SchedulerSettingHelp(
                    key=k,
                    title="",
                    description="",
                    impact="",
                    editable=True,
                    input_type=SchedulerSettingHelp.InputType.TEXT,
                    enum_values_json=[],
                    constraints_json={},
                    examples_json=[],
                    is_secret=_is_secret_key(k),
                )
            )

        if rows:
            SchedulerSettingHelp.objects.bulk_create(rows)
        self.stdout.write(self.style.SUCCESS(f"created={len(rows)}"))


DEFAULT_HELP: dict[str, dict] = {
    "SCHEDULER_MIN_ONLINE_WORKERS": {
        "title": "Minimum online workers",
        "description": "Redis heartbeatで判定した『オンラインworker数』の最低値。スケールアウト/インの運用に合わせて調整します。",
        "impact": "オンラインworkerがこの値未満になると Prometheus Alert（SchedulerNoWorkersOnline）が発火します。意図的にscale-to-zeroする場合は 0 に設定してください。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [0, 1, 2],
    },
    "SCHEDULER_ALERT_ROLE_CHANGE_ENABLED": {
        "title": "Alert: worker role change enabled",
        "description": "Leader/SubLeader の役割が切り替わったことを検知して警告アラートを出すかどうか。",
        "impact": "有効な場合、role change が発生すると Prometheus Alert（SchedulerWorkerRoleChanged）が発火します。",
        "input_type": "bool",
        "examples": [True, False],
    },
    "SCHEDULER_ALERT_HIGH_LOAD_ENABLED": {
        "title": "Alert: high load enabled",
        "description": "高負荷Worker（RUNNING/ASSIGNED job数が閾値超え）を警告アラートとして出すかどうか。",
        "impact": "有効な場合、Prometheus Alert（SchedulerWorkerHighLoad）が発火します。",
        "input_type": "bool",
        "examples": [True, False],
    },
    "SCHEDULER_WORKER_HIGH_LOAD_THRESHOLD": {
        "title": "High load threshold (jobs)",
        "description": "高負荷判定の閾値（worker単位の RUNNING+ASSIGNED job数）。",
        "impact": "小さすぎると頻繁に警告が出ます。スケール/運用ポリシーに合わせて調整してください。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [5, 10, 20],
    },
    "SCHEDULER_ALERT_WEBHOOK_TOKEN": {
        "title": "Alert webhook token",
        "description": "Alertmanager -> Django webhook 通知の簡易トークン（URLに含めて認証します）。",
        "impact": "トークンが漏洩すると外部から通知を偽装される可能性があります。",
        "input_type": "text",
        "examples": ["dev"],
        "is_secret": True,
    },
    "SCHEDULER_NOTIFY_SLACK_WEBHOOK_URL": {
        "title": "Slack webhook URL",
        "description": "通知先Slack Incoming Webhook URL。未設定の場合はSlack通知しません。",
        "impact": "URLにトークンが含まれます。取り扱い注意。",
        "input_type": "text",
        "examples": ["https://hooks.slack.com/services/..."],
        "is_secret": True,
    },
    "SCHEDULER_NOTIFY_TEAMS_WEBHOOK_URL": {
        "title": "Teams webhook URL",
        "description": "通知先Microsoft Teams Incoming Webhook URL。未設定の場合はTeams通知しません。",
        "impact": "URLにトークンが含まれます。取り扱い注意。",
        "input_type": "text",
        "examples": ["https://.../IncomingWebhook/..."],
        "is_secret": True,
    },
    "SCHEDULER_NOTIFY_EMAIL_TO": {
        "title": "Notify email to",
        "description": "通知先メールアドレス（カンマ区切り可）。未設定の場合はメール通知しません。",
        "impact": "SMTP設定が必要です。",
        "input_type": "text",
        "examples": ["ops@example.com"],
    },
    "SCHEDULER_NOTIFY_EMAIL_FROM": {
        "title": "Notify email from",
        "description": "通知送信元メールアドレス。",
        "impact": "SMTPの要件に合わせて設定してください。",
        "input_type": "text",
        "examples": ["scheduler@example.com"],
    },
    "SCHEDULER_NOTIFY_SMTP_HOST": {
        "title": "SMTP host",
        "description": "SMTPサーバのホスト名。",
        "impact": "未設定の場合、メール通知は無効になります。",
        "input_type": "text",
        "examples": ["smtp.example.com"],
    },
    "SCHEDULER_NOTIFY_SMTP_PORT": {
        "title": "SMTP port",
        "description": "SMTPサーバのポート。",
        "impact": "通常は 587(TLS) / 25 / 465(SSL) など。",
        "input_type": "text",
        "constraints": {"min": 1, "max": 65535},
        "examples": [587],
    },
    "SCHEDULER_NOTIFY_SMTP_USER": {
        "title": "SMTP user",
        "description": "SMTP認証ユーザ名。",
        "impact": "未設定の場合、認証なしで送信します（サーバ要件による）。",
        "input_type": "text",
        "examples": ["user"],
    },
    "SCHEDULER_NOTIFY_SMTP_PASSWORD": {
        "title": "SMTP password",
        "description": "SMTP認証パスワード。",
        "impact": "漏洩すると不正送信リスクがあります。",
        "input_type": "text",
        "examples": ["change-me"],
        "is_secret": True,
    },
    "SCHEDULER_NOTIFY_SMTP_USE_TLS": {
        "title": "SMTP use STARTTLS",
        "description": "SMTPでSTARTTLSを使うかどうか。",
        "impact": "環境に合わせて設定してください。",
        "input_type": "bool",
        "examples": [True, False],
    },
    "SCHEDULER_PROMETHEUS_URL": {
        "title": "Prometheus base URL",
        "description": "Ops DashboardがPrometheus HTTP APIを使ってジョブ性能情報を取得するためのURL（例: http://localhost:9090）。",
        "impact": "未設定の場合、Ops Dashboardのメトリクス表示は無効になります。",
        "input_type": "text",
        "examples": ["http://localhost:9090"],
    },
    "SCHEDULER_ALERTMANAGER_URL": {
        "title": "Alertmanager base URL",
        "description": "Ops DashboardからアラートのSilence（無効化）を行うためのAlertmanager URL（例: http://localhost:9093）。",
        "impact": "未設定の場合、ダッシュボードからのSilence操作は無効になります。",
        "input_type": "text",
        "examples": ["http://localhost:9093"],
    },
    "SCHEDULER_METRICS_TOKEN": {
        "title": "Metrics token",
        "description": "`/metrics` の保護用トークン。設定した場合、HTTPヘッダ `X-Scheduler-Token` が一致しないと 401 になります。",
        "impact": "Prometheus側からこのヘッダを付与できない環境では、設定するとscrapeできなくなります。",
        "input_type": "text",
        "examples": ["change-me"],
        "is_secret": True,
    },
    "SCHEDULER_REDIS_URL": {
        "title": "Redis URL",
        "description": "Leader選出・heartbeat・worker registry に使うRedis接続URL。",
        "impact": "変更すると別クラスタとして扱われ、既存workerと疎通しなくなります。",
        "input_type": "text",
        "examples": ["redis://localhost:6379/0"],
    },
    "SCHEDULER_GRPC_HOST": {
        "title": "gRPC bind host",
        "description": "WorkerがgRPCサーバをbindするホスト（インターフェース）。",
        "impact": "誤るとLeaderがWorkerへ到達できません。",
        "input_type": "text",
        "examples": ["127.0.0.1", "0.0.0.0"],
    },
    "SCHEDULER_GRPC_PORT_RANGE_START": {
        "title": "gRPC port range start",
        "description": "Workerが自動選択するgRPCポート範囲の開始。",
        "impact": "同一ホストで複数Workerを立てる場合、衝突しない範囲を確保します。",
        "input_type": "text",
        "constraints": {"min": 1, "max": 65535},
        "examples": [50051],
    },
    "SCHEDULER_GRPC_PORT_RANGE_END": {
        "title": "gRPC port range end",
        "description": "Workerが自動選択するgRPCポート範囲の終了。",
        "impact": "開始〜終了までで空きが無いとWorker起動に失敗します。",
        "input_type": "text",
        "constraints": {"min": 1, "max": 65535},
        "examples": [50150],
    },
    "SCHEDULER_TLS_CERT_FILE": {
        "title": "TLS cert file",
        "description": "gRPC(mTLS)で使用するサーバ証明書（PEM）のパス。",
        "impact": "誤るとgRPCサーバ起動に失敗するか、mTLSが成立しません。",
        "input_type": "text",
        "examples": ["/mnt/c/vscode/Scheduler/dev-certs/tls.crt"],
    },
    "SCHEDULER_TLS_KEY_FILE": {
        "title": "TLS key file",
        "description": "gRPC(mTLS)で使用する秘密鍵（PEM）のパス。",
        "impact": "漏洩するとなりすまし/復号リスクがあります。アクセス権を最小化してください。",
        "input_type": "text",
        "examples": ["/mnt/c/vscode/Scheduler/dev-certs/tls.key"],
    },
    "SCHEDULER_ASSIGN_AHEAD_SECONDS": {
        "title": "Assign ahead seconds",
        "description": "Leaderが時刻ジョブのJobRunを『どれだけ先まで』作成/割当するか。",
        "impact": "大きすぎるとDBに未来のJobRunが多く作られます。小さすぎると割当がギリギリになります。",
        "input_type": "text",
        "constraints": {"min": 1},
        "examples": [60],
    },
    "SCHEDULER_SKIP_LATE_RUNS_AFTER_SECONDS": {
        "title": "Skip late runs after seconds",
        "description": "遅延したASSIGNED実行をスキップする閾値（秒）。",
        "impact": "ダウン後の大量バックログ実行を防げますが、業務上重要なジョブがスキップされ得ます。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [300],
    },
    "SCHEDULER_REASSIGN_ASSIGNED_AFTER_SECONDS": {
        "title": "Reassign grace seconds",
        "description": "ASSIGNEDが開始されない場合に、LeaderがORPHANEDへ落として再割当する猶予（秒）。",
        "impact": "小さすぎると揺れ（無駄な再割当）が増えます。大きすぎると復旧が遅れます。",
        "input_type": "text",
        "constraints": {"min": 1},
        "examples": [10],
    },
    "SCHEDULER_CONTINUATION_CONFIRM_SECONDS": {
        "title": "Continuation confirm seconds",
        "description": "RUNNING中にworkerが消えたとき、CONFIRMING状態で待つ時間（秒）。",
        "impact": "短いと二重実行リスクを下げますが、実行継続の余地が減ります。",
        "input_type": "text",
        "constraints": {"min": 1},
        "examples": [30],
    },
    "SCHEDULER_ASSIGN_WEIGHT_LEADER": {
        "title": "Assign weight (leader)",
        "description": "割当バランスの重み（LEADER）。大きいほど割当が増えます。",
        "impact": "重みの比率でLeader/Worker/SubLeaderの負荷配分が変わります。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [1],
    },
    "SCHEDULER_ASSIGN_WEIGHT_SUBLEADER": {
        "title": "Assign weight (subleader)",
        "description": "割当バランスの重み（SUBLEADER）。",
        "impact": "値を上げるとサブリーダへ割当が寄ります。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [2],
    },
    "SCHEDULER_ASSIGN_WEIGHT_WORKER": {
        "title": "Assign weight (worker)",
        "description": "割当バランスの重み（WORKER）。",
        "impact": "値を上げると通常workerへ割当が寄ります。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [3],
    },
    "SCHEDULER_ASSIGN_RUNNING_LOAD_WEIGHT": {
        "title": "Running load weight",
        "description": "RUNNINGジョブが割当計算に与える負荷係数。",
        "impact": "大きいほど『実行中が多いworker』への新規割当が減ります。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [2],
    },
    "SCHEDULER_REBALANCE_ASSIGNED_ENABLED": {
        "title": "Rebalance assigned enabled",
        "description": "ASSIGNED（未開始）を保守的に再配分する機能のON/OFF。",
        "impact": "有効だと、偏りを減らせますが、再割当による揺れが増えることがあります。",
        "input_type": "bool",
        "examples": [True],
    },
    "SCHEDULER_REBALANCE_ASSIGNED_MIN_FUTURE_SECONDS": {
        "title": "Rebalance min future seconds",
        "description": "scheduled_for が現在よりこれ以上未来のASSIGNEDのみ再配分対象にする閾値（秒）。",
        "impact": "小さすぎると直前の実行が揺れます。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [30],
    },
    "SCHEDULER_REBALANCE_ASSIGNED_MAX_PER_TICK": {
        "title": "Rebalance max per tick",
        "description": "1 tick あたりに再配分するASSIGNEDの最大件数。",
        "impact": "大きいと回復は速いがDB更新が増えます。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [50],
    },
    "SCHEDULER_REBALANCE_ASSIGNED_COOLDOWN_SECONDS": {
        "title": "Rebalance cooldown seconds",
        "description": "再配分処理のクールダウン（秒）。",
        "impact": "小さすぎると頻繁に再配分が走り、揺れ/負荷が増えます。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [5],
    },
    "SCHEDULER_LOG_ARCHIVE_ENABLED": {
        "title": "Archive job logs",
        "description": "ジョブログをS3互換ストレージ（MinIO等）へアップロードします。",
        "impact": "有効にするとアップロード処理が走り、log_ref がオブジェクトストレージ参照になります。",
        "input_type": "bool",
        "examples": [True],
    },
    "SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL": {
        "title": "S3 endpoint URL",
        "description": "S3互換エンドポイント（MinIO等）のURL。",
        "impact": "誤るとアップロードに失敗します。",
        "input_type": "text",
        "examples": ["http://127.0.0.1:9000"],
    },
    "SCHEDULER_LOG_ARCHIVE_S3_REGION": {
        "title": "S3 region",
        "description": "S3互換のリージョン名（環境により不要/任意）。",
        "impact": "誤ると署名不一致で失敗する環境があります。",
        "input_type": "text",
        "examples": ["us-east-1"],
    },
    "SCHEDULER_LOG_ARCHIVE_BUCKET": {
        "title": "S3 bucket",
        "description": "ログ格納バケット名。",
        "impact": "存在しない/権限が無いとアップロード失敗します。",
        "input_type": "text",
        "examples": ["scheduler-logs"],
    },
    "SCHEDULER_LOG_ARCHIVE_PREFIX": {
        "title": "S3 object prefix",
        "description": "バケット内のプレフィックス（任意）。",
        "impact": "変更すると保存先パスが変わり、参照URLも変わります。",
        "input_type": "text",
        "examples": ["job-logs/"],
    },
    "SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL": {
        "title": "Public base URL",
        "description": "UIがlog_refをURL表示/取得するためのベースURL（allowlist対象）。",
        "impact": "誤るとUIでログが読めません。URLを公開する構成の場合は漏洩リスクに注意。",
        "input_type": "text",
        "examples": ["http://127.0.0.1:9000"],
    },
    "SCHEDULER_LOG_ARCHIVE_ACCESS_KEY_ID": {
        "title": "S3 access key id",
        "description": "S3互換のアクセスキーID。",
        "impact": "誤ると認証失敗します。",
        "input_type": "text",
        "examples": ["minioadmin"],
    },
    "SCHEDULER_LOG_ARCHIVE_SECRET_ACCESS_KEY": {
        "title": "S3 secret access key",
        "description": "S3互換のシークレットアクセスキー。",
        "impact": "漏洩するとログ読み取り/改ざんに繋がります。必ず秘匿。",
        "input_type": "text",
        "is_secret": True,
        "examples": ["minioadmin"],
    },
    "SCHEDULER_LOG_LOCAL_DELETE_AFTER_UPLOAD": {
        "title": "Delete local log after upload",
        "description": "アップロード成功時にローカルログファイルを削除します。",
        "impact": "ローカルディスク使用量を抑えられますが、S3側が読めない場合に復旧できません。",
        "input_type": "bool",
        "examples": [True],
    },
    "SCHEDULER_LOG_LOCAL_RETENTION_HOURS": {
        "title": "Local log retention hours",
        "description": "ローカルログを削除するまでの保持時間（時間）。0で無効。",
        "impact": "短くしすぎると調査時にローカルログが残りません。",
        "input_type": "text",
        "constraints": {"min": 0},
        "examples": [168],
    },
    "SCHEDULER_EVENTS_API_TOKEN": {
        "title": "Events ingest token",
        "description": "/api/events/ingest/ の認証トークン。",
        "impact": "漏洩すると外部からイベント投入されJobRun生成され得ます。必ず秘匿してローテーション可能にしてください。",
        "input_type": "text",
        "is_secret": True,
        "examples": ["changeme"],
    },
    "SCHEDULER_DEPLOYMENT": {
        "title": "Deployment mode",
        "description": "実行環境モード。ローカル検証用とK8S想定で挙動を切り替えるために使います。",
        "impact": "Workerのログ取り扱い/表示など、一部の運用挙動が変わります。",
        "input_type": "enum",
        "enum_values": ["local", "k8s"],
        "examples": ["local"],
    },
}
