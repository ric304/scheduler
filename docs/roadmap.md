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

- [x] M0: スキャフォールド（組み込み可能なDjango appの器）
- [x] M1: コア実行基盤（Worker/Leader/SubLeader + Redis + DB）
- [x] M2: gRPC制御面（mTLS）
- [x] M3: 時間起動（分単位）
- [x] M3': 時間起動（一般的な実行パターン対応）
- [ ] M4: イベント駆動
- [ ] M5: 切り離し/降格/継続許容（CONFIRMING）
- [x] M6: 運用UI（Bootstrap/DataTables/Modal/Notyf）
- [x] M6': 設定のDB化 + UI編集 + リロード指示（Leader→Worker gRPC）
- [x] M6'': 権限管理（3ロール: アプリ担当/運用管理者/スーパーユーザ）
- [ ] M7: 受け入れ試験・性能検証・運用監視
- [ ] M8: K8sデプロイ・運用手順（cert-manager含む）

※実際の順序は依存関係に従い、テスト効率のため K8s は **すべての機能が出揃ってから最後にまとめて対応**する。
  推奨: M1→M2→M3→M6→フェーズF（実ジョブ実行）→M5→M4→M3'→M6'→M6''→M7→M8

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

### フェーズD'（M3'）: 時間起動（一般的な実行パターン対応）

目的:
- 現状「N分おき」だけの制約を外し、一般的なジョブ実行パターンを登録できるようにする（ただしUIで安全に編集できる形式に限定）。

成果物:
- timeジョブのスケジュール表現を拡張し、次回実行時刻を計算できる
- 既存のJobRun生成/重複防止/割当の枠組みは維持

作業（案）:
- schedule JSONの形式を固定（例: `kind` + パラメータ）
  - `{"kind":"every_n_minutes","n":5}`
  - `{"kind":"hourly","minute":15}`
  - `{"kind":"daily","time":"02:30"}`
  - `{"kind":"weekdays","time":"09:00"}`
  - `{"kind":"weekly","weekday":0,"time":"09:00"}`（0=Mon等は仕様で固定）
- 互換: 既存の `{"every_n_minutes": N}` も当面は読み取れるようにする（移行のため）

完了条件:
- 上記いずれか2〜3パターンを登録し、想定通りにJobRunが生成される

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
- （追加）Workerログ参照の導線
  - K8s前提のMVPでは「kubectl logs / ログ基盤（Loki等）」を推奨し、運用UIはリンク/手順の提示まで
  - 将来拡張で「ログ（直近N行）をAPI経由で取得してUI表示」を検討

完了条件:
- JobDefinitionの作成/編集（モーダル）
- JobRunの一覧表示
- Worker一覧表示（Redis）

注: この段階のSettingsは「表示のみ」でも可（編集・反映はM6'で実装）。

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

### フェーズE'（M6'）: 設定のDB化 + UI編集 + リロード指示（Leader→Worker gRPC）

目的:
- これまで環境変数で渡している閾値/パラメータをDB（`SchedulerSetting`）に登録し、Worker起動時に取得する。
- 変更は運用UIから行い、「反映」操作でLeaderが各WorkerへgRPC指示して設定をリロードさせる。

成果物:
- DB上の設定（SchedulerSetting）を読み込み、Leader/Workerが動作に反映できる
- Ops UIにSettings編集画面（モーダル）を追加
- 反映ボタン: UI→Leaderに指示→Leaderが全WorkerへgRPCでReload/Refreshを送る

作業（案）:
- 設定の優先順位を固定する

---

### フェーズE''（M6''）: 権限管理（3ロール）

目的:
- 組織運用を前提に、Ops UI の閲覧/更新/危険操作をロールで制御する。

ロール要件:
- アプリケーション担当者: ジョブ登録（Jobs 作成/編集）、ログ確認（JobRuns/ログDL）
- 運用管理者: 上記 + 設定変更/反映、Worker 操作
- スーパーユーザ: 上記 + 設定ヘルプの編集、アクセストークン等の秘密情報の参照

設計方針:
- Django auth の Group をロールとして利用（3つの固定グループ、接頭: `schedule_ops_`）
- 事前セットアップ不要: グループが無い場合はアプリ起動時に自動作成する
- 画面/ API はロールでガードする（リンクの出し分けは補助、サーバ側を正とする）
- 秘密情報（token/password/secret）は「運用管理者は再設定はできるが既存値は見えない」を基本とし、スーパーユーザのみ参照可能

事前セットアップの扱い（重要）:
- Settingsのヘルプ（`SchedulerSettingHelp`）は、DBが空でも起動時に自動シードする（`loaddata` は任意）
- ただし「最初のログインユーザ」は必要なため、初回のみ `python manage.py createsuperuser` で作成する（以後はOps UIのUsersで追加/ロール付与可能）

完了条件:
- Jobs/JobRuns はアプリ担当者以上でアクセス可能
- Settings/Workers は運用管理者以上でアクセス可能
- 設定ヘルプ編集・秘密情報参照はスーパーユーザのみ
  - DB設定が存在するキーはDBを優先（環境変数はフォールバック）
  - DBに未登録のキーは環境変数（既存互換）
- gRPCにリロード用RPCを追加（例: `ReloadConfig`）
  - Workerは受け取り次第、DBから最新設定を再読み込みしてRuntimeに反映
  - 失敗したworkerはUIに集計表示（MVP: 成功/失敗のみ）

完了条件:
- Ops UIから閾値を変更し、反映操作で実際にLeader/Workerの挙動が変わる

---

### フェーズI（M8）: K8sデプロイ/運用手順（MVP）

成果物:
- マニフェスト（Deployment/Service/ConfigMap/Secret/Issuer/Certificate）
- 証明書更新とローリング更新の運用（CronJob + RBAC）

完了条件:
- K8s上で複数Workerが動き、UIから状態確認できる

---

### フェーズJ（M8）: 検証・監視

（注）K8s対応は最後にまとめて行うため、検証・監視（M7）を先に実施してよい。

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

## 5. バックログ（記録ルール）

ルール:
- 「後でやる（バックログ化）」と判断した項目は、必ずこのセクションへ追記して管理する。

候補:
- ジョブ統計に基づく負荷分散
  - 例: 実行時間の移動平均/EWMA、失敗率、タイムアウト率、直近のスループット
  - 用途: 遅い/不安定なWorkerへの割当を自動で抑制、重いジョブを分散
  - 位置づけ: M8（検証・監視）と合わせて設計・実装（先に入れる場合は要件を明確化）
- JobDefinitionのノード指定（affinity）
  - 例: node_id 指定 / ラベル指定 / ワーカープール指定
  - 用途: 特定ノード上の依存（ローカルデバイス/データ）や、専用ワーカーへの割当
  - 位置づけ: 負荷分散・リバランスと一体で設計（UI/DB/API/割当ロジック）

## 6. 直近のTODO（方向修正）

方針（2026-01）:
- 設定はDB側で一元管理し、Ops UI（Settings）から変更→Applyで反映できることを正とする。
- RedisはSchedulerの一部（必須コンポーネント）として扱い、デフォルトはSPOFを排除した構成を指向する。
- 検証環境は「最低限3ノード（シングルマスター + worker×2）」で、Schedulerが提供する可用性/分断耐性の検証ができることを目標とする。
  - ただし、これはあくまでK8s上のアプリケーション検証のためであり、K8s自体の可用性（マルチマスター等）は考慮しない。
- 一方で、ホストDjangoアプリとして検討すべき事項（DB基盤やK8s運用設計）はScheduler AddOnのスコープ外とする。

直近TODO（上から順に着手）:
- 1) 「DB一元管理」の境界を確定
  - DBに持つ: 閾値/動作パラメータ（tick間隔、TTL、再割当猶予、ログ保持、リトライ等）
  - 要方針決定: 接続情報/認証情報（例: Redisパスワード、外部URL、トークン）をDBに保持するか（Secrets機構に委譲するか）
- 2) 設定キーの分類と編集ガードを整備
  - `SchedulerSettingHelp` の `is_secret/editable/constraints` を正として、Ops UIとAPIの両方で一貫した入力制約を適用
  - `SCHEDULER_REDIS_URL` 等の“URLに資格情報が混入し得るキー”のマスク方針を確定（誤って平文表示されない）

進捗（完了マーク）:
- [x] 2) `SCHEDULER_REDIS_URL` を `is_secret=true`（非Superuserはマスク）
- [x] 2) `ACCESS_KEY` 系を再チェックし `is_secret=true`（非Superuserはマスク）
- [x] 2) Superuserは平文表示可能（既存仕様を維持）
- 3) Apply反映の成功条件・可観測性を明確化
  - `ConfigReloadRequest` の結果（成功/失敗/対象ワーカー）をOps UIで確認可能にし、失敗時の再実行手順を定義
  - Leader不在/到達不能時の扱いを仕様化
    - UIは「失敗」として明示し、しばらくして再試行する旨をメッセージ表示（例: Notyf error + 再実行ガイド）
    - 連続失敗時のオペレーション（Leader復旧→再度Apply、等）を手順化
- 4) Redis高可用“デフォルト”の設計を確定
  - 採用方式: Sentinel replication をデフォルト（将来拡張でCluster）
  - アプリ側の接続戦略: Sentinelでmaster自動解決
  - フェイルオーバ時の接続断/再接続・タイムアウトの標準値を設定化（DBから変更可能に）
    - 例: connect timeout / socket timeout / retry on timeout / healthcheck interval 等
    - 反映方法: `SchedulerSettingHelp` + Ops UI（Settings）から変更できることを正とする
- 5) 「3ノード検証」でどこまでHAを成立させるかを明文化
  - 3ノード（master + worker×2）での検証は「アプリ挙動（Scheduler/Redis）の確認」が目的で、K8sのHAは対象外（シングルマスター）
  - Redis Sentinelのクォーラム/自動フェイルオーバ要件を整理し、必要ノード数・構成（Sentinel配置）を明記
- 6) ドキュメントの役割分離（AddOnスコープの明確化）
  - DB/K8sはホストDjangoアプリの設計事項として、Scheduler側ドキュメントでは「検証のための最小構成」までに留める
  - 依存コンポーネント（Redis等）は“Scheduler同梱/必須”としてセットアップ導線を一本化

