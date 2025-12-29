# 開発ロードマップ（ジョブ実行制御基盤）

作成日: 2025-12-28

本ロードマップは以下ドキュメントを前提とする。

- [docs/architecture.md](docs/architecture.md)
- [docs/db-consistency.md](docs/db-consistency.md)
- [docs/grpc-api.md](docs/grpc-api.md)
- [docs/operations-ui.md](docs/operations-ui.md)
- [docs/ui-coding-rules.md](docs/ui-coding-rules.md)

## 0. 前提（MVPの運用）

- 環境: K8s（Meshなし）
- mTLS: `cert-manager` によるSecret更新 + Deploymentローリング更新で追随
- Secret: 全Pod共通をデフォルト（必要ならデプロイ設定でPodごとに差し替え）
- UI: Bootstrap + DataTables + Modal + Notyf
  - 一覧→モーダル更新→一覧はリロードせず状態維持（DataTables stateSave + 差分更新）

## 1. マイルストーン一覧

- M0: スキャフォールド（組み込み可能なDjango appの器）
- M1: コア実行基盤（Worker/Leader/SubLeader + Redis + DB）
- M2: gRPC制御面（mTLS）
- M3: 時間起動（分単位）
- M4: イベント駆動
- M5: 切り離し/降格/継続許容（CONFIRMING）
- M6: 運用UI（Bootstrap/DataTables/Modal/Notyf）
- M7: K8sデプロイ・運用手順（cert-manager含む）
- M8: 受け入れ試験・性能検証・運用監視

※実際の順序は依存関係に従い、M1→M2→M3→M6→M5→M4→M7→M8 の流れを推奨。

## 2. フェーズ別詳細

### フェーズA（M0）: スキャフォールド

成果物:
- Django reusable app（例: `scheduler`）の基本構成
- 設定項目（Redis URL、node_id、gRPC port、TLSファイルパスなど）の読み込み枠

作業:
- `scheduler` アプリ雛形
- `settings.py` に追加する項目の仕様化
- マイグレーションの土台

完了条件:
- 既存Djangoプロジェクトに `INSTALLED_APPS` 追加で組み込み可能

---

### フェーズB（M1）: コア実行基盤（DB/Redis/ロール選出）

成果物:
- DBモデル: JobDefinition / JobRun / Event / SchedulerSettings / AdminActionLog
- Redisキー運用: worker登録・heartbeat・leader lock・epoch・detach
- Workerプロセス（Management Command）
  - worker_id採番、heartbeat更新
  - leaderロック獲得/喪失
  - subleaderロック獲得

作業:
- DBスキーマ（索引・ユニーク制約含む）を実装
- Redisアクセス層（キー規約・TTL更新）
- Leader tick（DBを見るだけの空実装でも可）

完了条件:
- K8s/ローカルでWorkerを複数起動し、Leaderが1つだけ選出され続ける
- epochがLeader切替で更新される

---

### フェーズC（M2）: gRPC制御面（mTLS）

成果物:
- Worker gRPCサーバ（Ping/GetStatus/StartJob/CancelJob/Drain/ConfirmContinuation）
- mTLS設定（証明書ファイル読み込み）

作業:
- gRPCハンドラの実装（まずはPing/GetStatus）
- mTLS: `/etc/scheduler/tls/tls.crt` と `/etc/scheduler/tls/tls.key` を利用
- リトライ/タイムアウトの基準値をSettingsに対応

完了条件:
- Leaderが全WorkerへPingでき、状態を取得できる

---

### フェーズD（M3）: 時間起動（分単位）

成果物:
- JobDefinition（type=time）のスケジュール解釈
- JobRunの生成（重複防止）
- assign_ahead_seconds による事前割当

作業:
- スケジュール表現を「UI安全形式」に固定し、次回実行時刻を計算
- JobRun作成の一意性（(job_definition_id, scheduled_for) UNIQUE）
- 割当アルゴリズム（load最小）

完了条件:
- 分単位でJobRunが生成され、割当される（実行はまだダミーでも可）

---

### フェーズE（M6先行推奨）: 運用UI（最小）

成果物:
- Bootstrapベースの画面
- 一覧はDataTables（Paging/検索/ソート）
- 更新/操作はモーダル
- 成功通知はNotyf（自動クローズ）
- 一覧→更新→一覧でDataTables状態維持 + 差分更新

作業:
- 認証/権限（Django authを再利用）
- `/ops/` 配下のURLとテンプレート基盤
- Jobs/JobRuns/Workers/Settings/Audit の最低限一覧

完了条件:
- JobDefinitionの作成/編集（モーダル）
- JobRunの一覧表示
- Worker一覧表示（Redis）

---

### フェーズF（M3→M2完了後）: 実ジョブ実行（StartJob）

成果物:
- Workerが `python manage.py <command>` をサブプロセス実行
- JobRunの状態遷移（ASSIGNED→RUNNING→SUCCEEDED/FAILED/TIMED_OUT）
- タイムアウト/キャンセル

作業:
- JobRunの楽観ロック（version）または同等の競合制御
- CancelJobでプロセス停止
- log_ref/要約の保存

完了条件:
- 指定コマンドが指定引数で実行され、履歴に残る

---

### フェーズG（M5）: 切り離し/降格/継続許容（CONFIRMING）

成果物:
- Worker切り離し（detach）
- Leader降格/昇格（SubLeader）
- 継続許容フロー
  - JobRunに `continuation_state=CONFIRMING` と期限
  - 確認中は再割当抑止

作業:
- detach判定（gRPC失敗 + heartbeat TTL）
- 再割当条件（ASSIGNED停滞、ORPHANED化）
- ConfirmContinuationの判定ロジック

完了条件:
- わざとWorkerを落としても、ジョブが再割当される
- 確認中は再割当されない
- Leader停止時にSubLeaderが昇格して継続する

---

### フェーズH（M4）: イベント駆動

成果物:
- Eventモデル投入→JobRun生成→実行
- dedupe_key/idempotencyの運用

完了条件:
- API等からイベント投入し、指定ジョブが走る

---

### フェーズI（M7）: K8sデプロイ/運用手順（MVP）

成果物:
- マニフェスト（Deployment/Service/ConfigMap/Secret/Issuer/Certificate）
- 証明書更新とローリング更新の運用（CronJob + RBAC）

完了条件:
- K8s上で複数Workerが動き、UIから状態確認できる

---

### フェーズJ（M8）: 検証・監視

検証項目:
- split brain想定（ネットワーク分断/Redis failover）での挙動
- 3〜5 worker → 30〜50 worker 相当（負荷・DB/Redis）
- UI操作（切り離し/降格/キャンセル）の監査ログ

完了条件:
- 受け入れ基準（運用で使える最低限）を満たす

## 3. MVPの受け入れ基準（短縮版）

- Worker起動でLeaderが1つに収束し、Worker状態がRedisで見える
- 分単位スケジュールのJobが割当・実行できる
- Worker停止でジョブが再割当される
- 継続確認中（CONFIRMING）は再割当されない
- 運用UIでJobs/JobRuns/Workers/Settingsが確認でき、操作はモーダルで完結
- 一覧はDataTablesで検索/ソート/ページングが使える（更新後も状態維持）

## 4. リスクと先回り（重要）

- split brainはゼロにはできない
  - epoch + DB遷移条件 + 冪等設計で収束させる
- DB負荷（JobRun大量）
  - インデックス/アーカイブ/サーバサイドDataTablesに移行
- 証明書更新の反映
  - MVPはローリング再起動で割り切り、必要に応じてホットリロードへ拡張
