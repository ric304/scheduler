# Copilot instructions (Scheduler repo)

このファイルは **docs/ の設計・手順を正**として、Copilot がこのリポジトリで作業する際に「環境を間違えない」「UI実装をブレさせない」「整合性/安全性を壊さない」ための指示です。

対象ドキュメント（必読）:
- docs/dev-setup.md
- docs/operations-ui.md
- docs/ui-coding-rules.md
- docs/architecture.md
- docs/db-consistency.md
- docs/grpc-api.md
- docs/roadmap.md

---

## 0. 最優先ルール（間違い防止）

- **開発はWSL前提**（docs/dev-setup.md）。VS Code は Remote - WSL で開く。
- **コマンドはWSL側で実行**する（`/mnt/c/...` 配下のリポジトリを WSL で開いて作業）。
- Windows の `python manage.py ...` で動作確認しない（venv/依存関係がズレて失敗しやすい）。
- 変更前に docs/ を読み、実装が設計に反していないか確認してから着手する。

---

## 1. 開発環境（WSL）

前提（docs/dev-setup.md）:
- OS: Windows + WSL2
- Python: 3.13
- 推奨: WSL側で venv を作り、その Python を VS Code の interpreter に設定

作業ルール:
- venv: ディレクトリ名は `venv` を基本（例: `python -m venv venv`、`source venv/bin/activate`）
- 依存サービス: WSL上の Docker で起動（`docker compose up -d`）
- 以降の `python`/`pip`/`manage.py` は **venv有効化済みのWSL** で実行する

TLSパス（docs/dev-setup.md / docs/grpc-api.md）:
- デフォルト（K8s想定）: `/etc/scheduler/tls/tls.crt` と `/etc/scheduler/tls/tls.key`
- 開発（WSL）: 
  - `SCHEDULER_TLS_CERT_FILE=/mnt/c/vscode/Scheduler/dev-certs/tls.crt`
  - `SCHEDULER_TLS_KEY_FILE=/mnt/c/vscode/Scheduler/dev-certs/tls.key`

---

## 2. Ops UI 実装ルール（最重要）

Ops UI の方針（docs/operations-ui.md / docs/ui-coding-rules.md）:
- Django Adminは使わない（ただし Django の auth は利用）
- UIは **Bootstrap + DataTables + Modal + Notyf** を原則とする
- 画面遷移を増やさず、作成/編集/確認は **原則モーダルで完結**

禁止事項（docs/ui-coding-rules.md）:
- 独自の複雑なUIコンポーネントを作らない（Bootstrapで表現する）
- **更新後に `location.reload()` で一覧を再読み込みしない**
  - DataTables の検索/ページ/ソート状態が消えるため

推奨実装（docs/ui-coding-rules.md）:
- 一覧は DataTables を必ず使う（paging/検索/ソート）
- `stateSave: true` を有効化
- 更新成功時は
  - `row().data(...)` + `draw(false)` などで差分更新、または
  - `ajax.reload(null, false)`（server-side時）

危険操作（削除/切り離し/降格/キャンセル等）:
- **必ず確認モーダル**を挟む
- サーバ側APIは **POST** のみ
- 実施後は **AdminActionLog に必ず記録**する（docs/operations-ui.md）

API呼び出し:
- CSRF を付ける（`X-CSRFToken`）
- フォーム入力エラー: モーダル内で Bootstrap validation（`is-invalid` + `invalid-feedback`）
- 通信/サーバエラー: Notyf error（判断不要なもの）

設定（SCHEDULER_*）の扱い（docs/ui-coding-rules.md）:
- Ops Settings で編集できる形を優先し、UI機能のために env 追加を必須にしない
- 新しい設定キーを追加する場合は `SchedulerSettingHelp` の定義も追加する

---

## 3. 権限・ロール

権限設計（docs/operations-ui.md / docs/roadmap.md）:
- Django Group をロールとして利用（3ロール）
- 画面/ API は **サーバ側でロールガード**する（リンク出し分けは補助）

ロール（実装の固定グループ名）:
- `schedule_ops_app_operator`
- `schedule_ops_ops_admin`
- `schedule_ops_superuser`

実装ルール:
- View は必ず適切な role check を通す（例: app operator / ops admin / superuser）
- 「自分自身の削除」など、ロックアウトに繋がる操作はサーバ側で拒否する

---

## 4. DB整合性・状態遷移（壊さない）

整合性方針（docs/architecture.md / docs/db-consistency.md）:
- ネットワーク分断・二重Leaderを前提に、RDBを最終整合点とする
- epoch（フェンシング）を前提とし、古いepochからの更新を拒否できる設計にする

状態遷移（MVP、docs/db-consistency.md）:
- PENDING -> ASSIGNED
- ASSIGNED -> RUNNING
- RUNNING -> SUCCEEDED / FAILED / TIMED_OUT
- ASSIGNED/RUNNING -> CANCELED
- ASSIGNED -> ORPHANED
- ORPHANED -> ASSIGNED（再割当）
- **逆戻りは禁止**

更新実装の原則:
- （推奨）楽観ロック `version` を想定し、`WHERE id=? AND version=? AND state=? ...` の条件付き更新にする
- `RUNNING -> 終了` は `leader_epoch` 一致も条件に含める
- `ASSIGNED -> RUNNING` は `assigned_worker_id == self` を条件に含める

---

## 5. gRPC / mTLS

gRPC接続モデル（docs/grpc-api.md）:
- 各 Worker プロセスが gRPC サーバとして待ち受け
- Leader/SubLeader が Redis の `grpc_host/grpc_port` から接続

mTLS（必須、docs/grpc-api.md）:
- 証明書パスのデフォルトは `/etc/scheduler/tls/...`
- 開発環境では docs/dev-setup.md の例に従いパスを差し替える

---

## 6. 進め方（Copilot運用）

- 変更は最小・目的限定（周辺のリファクタはしない）
- 既存のパターン（View/Template/JS）を優先し、docs と実装に差がある場合は
  - まず docs を確認
  - その上で **このリポジトリの実装パターンに合わせる**（破壊的変更を避ける）
- 可能なら WSL の venv で `python manage.py check` を実行してから完了とする

---

## 7. よくある間違い（禁止）

- Windows 側の Python で `manage.py` を実行して「Djangoが無い」となる（WSL venv で実行する）
- Ops UI の更新後に `location.reload()` して DataTables 状態が消える
- 危険操作で確認モーダルが無い / GETで副作用を起こす
- 権限制御をテンプレの出し分けだけに頼る（必ずサーバ側を正とする）
