# 運用管理画面 設計（Django Adminを使わない）

作成日: 2025-12-28

## 0. 方針

- Django標準Adminは使用しない
- ただし **Djangoの認証（User/Group/Permission）** は再利用し、既存アプリへの組み込みを容易にする
- 見た目はカスタマイズ可能にする
  - テンプレートをDjango標準のテンプレート上書き機構で差し替え可能
  - CSSはCSS変数（design tokens）をベースにし、テーマ差し替えを容易にする
  - JSは最小限（MVPはサーバサイドレンダリング中心）

## 1. UI実装方式（推奨）

### 1.1 技術選択

- Django Templates + Class Based Views
- スタイル/コンポーネント: Bootstrap（標準機能を最大限利用）
- テーブル: DataTables（Paging/検索/ソート）
- 更新/メッセージ: Bootstrap Modalを利用
- 判断が不要な通知: Notyf（自動クローズ）

実装規約は [docs/ui-coding-rules.md](docs/ui-coding-rules.md) を正とする。

### 1.2 URL構成（例）

- `/ops/` ダッシュボード
- `/ops/jobs/` ジョブ定義一覧
- `/ops/jobs/<id>/` ジョブ定義詳細/編集
- `/ops/runs/` 実行履歴一覧
- `/ops/runs/<id>/` 実行履歴詳細
- `/ops/workers/` ワーカー一覧（Redis）
- `/ops/workers/<worker_id>/` ワーカー詳細
- `/ops/settings/` 閾値設定（SchedulerSettings）
- `/ops/audit/` 操作ログ

## 2. 画面要件

### 2.1 ダッシュボード

目的: 全体の状態を俯瞰

表示項目（例）:
- Leader情報（worker_id, node_id, epoch, last_seen）
- SubLeader情報（node別）
- Workerサマリ（総数、detach数、実行中数）
- JobRunサマリ（直近n件、失敗件数、遅延件数）

### 2.2 ジョブ定義一覧

- enabled/disabled
- type（time/event）
- schedule
- command_name
- concurrency_policy
- 操作:
  - 有効/無効切替
  - 手動実行（即時JobRunを作成して実行）

### 2.3 ジョブ定義編集

- scheduleは「安全に編集できるフォーム」を提供
- default_args_jsonはJSONエディタ（textarea + バリデーション）
- timeout, retry等

### 2.4 実行履歴一覧

- scheduled_for / assigned_at / started_at / finished_at
- state
- assigned_worker_id
- error_summary
- log_ref
- フィルタ（MVPでは最小限で可）

### 2.5 実行履歴詳細

- JobRunの時系列
- attempt、leader_epoch
- continuation_state（確認中かどうか）
- continuation_check_deadline_at（確認期限）
- 結果、エラー要約
- log_refリンク
- 操作:
  - Cancel（可能な状態のときのみ）
  - Reassign（ORPHANED等のとき）

log_ref の扱い:
- 直近期間はDB内ログ（または同等の高速参照）を表示
- 期間超過後は log_ref（S3互換オブジェクトストレージ等）へのリンク/参照情報を表示

### 2.6 ワーカー一覧（Redis）

- worker_id, node_id, role
- last_heartbeat
- load
- detached
- current_job_run_id

操作:
- 切り離し（detach）
- 切り離し解除（ただし「旧IDとして復帰」ではなく、新IDで参加する運用を推奨）
- Drain（新規割当停止）

### 2.7 ワーカー詳細

- gRPC endpoint
- heartbeat履歴はRedis上では持たない（必要なら別途メトリクス基盤）
- 直近のjob_run_id
- 操作:
  - Ping/GetStatus（画面からgRPC実行）
  - Cancel current job

### 2.8 閾値設定

- SchedulerSettingsの編集
- 変更は即時反映（Leader/Workerは設定を定期的にリロード）

追加（確定案）:
- 継続許容: リトライ回数/間隔（デフォルト: 3回, 0.3秒）
- ログ保持: DB保持日数（デフォルト: 7日）

### 2.9 操作ログ

- actor
- action
- target
- created_at
- detail_json

## 3. 権限設計

- `ops_view`: 参照のみ
- `ops_operator`: 切り離し/キャンセル/再割当など運用操作
- `ops_admin`: 設定/ジョブ定義変更

既存のDjango Group/Permissionに紐づけて運用できるようにする。

## 4. カスタマイズ設計

### 4.1 テンプレート

- `scheduler_ops/base.html` を基底にし、プロジェクト側でテンプレート上書き可能
- ブロック:
  - `title`
  - `nav`
  - `content`
  - `extra_css`
  - `extra_js`

### 4.2 スタイル（CSS変数）

MVPでは色をハードコードせず、以下のCSS変数でテーマ化する。

例:
- `--ops-bg`
- `--ops-fg`
- `--ops-muted`
- `--ops-accent`
- `--ops-border`
- `--ops-danger`
- `--ops-success`

プロジェクト側で `:root { ... }` を上書きできる。

## 5. 画面から実行される運用操作（サーバサイドAPI）

- POST `/ops/workers/<id>/detach` → Redisにdetachフラグ
- POST `/ops/workers/<id>/undock` → detach解除（ただし再参加は新ID推奨）
- POST `/ops/leader/degrade` → degradeフラグ
- POST `/ops/runs/<id>/cancel` → WorkerへgRPC Cancel + DB更新
- POST `/ops/runs/<id>/reassign` → DB状態更新 + 再割当対象へ

すべて AdminActionLog に記録する。

## 6. 表示データの取得

- RDB: JobDefinition / JobRun / Event / Settings / Audit
- Redis: Worker一覧、Leader/SubLeader状態
- gRPC: 任意で「今すぐPing」「今すぐStatus」

## 7. パフォーマンス

- Worker数が50程度まで:
  - Worker一覧はRedisのキー走査を避け、Worker登録時に `scheduler:workers:set` などの集合も併用すると安定
  - または `SCAN` を用いて段階的に取得
- JobRun一覧はRDBのインデックス設計で対応

## 8. 受け入れ基準（MVP）

- ダッシュボードでLeader/Worker/直近実行が確認できる
- ジョブ定義の作成/無効化/手動実行ができる
- 実行履歴の成功/失敗が追える
- ワーカーの切り離しができ、切り離し中は割当されない
- Leader降格ができ、SubLeaderが昇格して継続する（テスト環境で検証可能）

---

次工程では、この画面設計に対応する Django app のURL/View/Template 構成案（雛形）を追加し、既存プロジェクトに組み込む手順（設定項目一覧）まで落とし込む。
