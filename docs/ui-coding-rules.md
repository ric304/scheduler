# 運用管理画面 コーディング規約（Bootstrap / DataTables / Modal / Notyf）

作成日: 2025-12-28

## 0. 目的

運用管理画面（Django Adminを使わない）の実装において、UIの一貫性・実装速度・保守性を高めるためのコーディング規約を定める。

本規約は [docs/operations-ui.md](docs/operations-ui.md) を具体実装に落とす際の指針とする。

## 1. 採用ライブラリ（原則）

- CSS/コンポーネント: Bootstrap（標準機能を最大限活用）
- テーブル: DataTables（Paging/検索/ソートを提供）
- モーダル: Bootstrap Modal
- トースト通知: Notyf（操作判断が不要な通知は自動クローズ）

## 2. UIの基本方針

- 独自UIコンポーネントは作らない（Bootstrapで表現できる範囲に寄せる）
- 画面遷移を増やさず、編集/作成/確認は原則モーダルで完結させる
- 一覧はDataTablesで統一し、表の機能（検索/ソート/ページング）はDataTablesに寄せる

## 3. テンプレート構成（Django Templates）

### 3.1 ベーステンプレート

- `scheduler_ops/base.html`
  - Bootstrap / DataTables / Notyf の読込を集約する
  - ページ固有のJS/CSSは block で差し込む

必須ブロック:
- `title`
- `content`
- `extra_css`
- `extra_js`

### 3.2 パーシャル（部品化）

- モーダルは `scheduler_ops/partials/modal.html` のように共通化し、本文だけ差し替える
- テーブル行は partial 化して差し替えやすくする（将来の部分更新に備える）

## 4. Bootstrap の利用ルール

- レイアウト: `container` / `container-fluid`, `row`, `col-*` を使用
- フォーム: `form-control`, `form-select`, `input-group` を使用
- バリデーション: `is-invalid` / `invalid-feedback` を使用
- ボタン: `btn`, `btn-primary`, `btn-outline-*`, `btn-danger` を使用
- バッジ: `badge` をステータス表示に使用

禁止:
- 独自の複雑なCSSでBootstrapを置き換える

カスタマイズ方針:
- 見た目調整は「Bootstrap変数（Sass）」または「上書きCSS」で行い、テンプレートに散らさない

## 5. DataTables の利用ルール

### 5.1 対象

- 一覧（Jobs/JobRuns/Workers/Audit 等）は必ずDataTablesで実装

### 5.2 必須機能

- Paging
- 検索（全体検索）
- ソート

### 5.3 推奨設定

- デフォルトはクライアントサイド（件数が増える場合はサーバサイドへ移行可能な形に）
- 列の定義をJS側で明示し、HTML側はできるだけ素朴に保つ

### 5.4 サーバサイド処理（将来）

- JobRunが増える場合は DataTables server-side processing を採用
  - Django view で `draw/start/length/search/order` を受け取って返す

## 6. モーダル運用（更新画面/メッセージ画面）

### 6.1 使い分け

- 作成/編集: モーダルフォーム
- 確認: モーダル（OK/Cancel）
- メッセージ: 重要（判断必要）ならモーダル、判断不要ならNotyf

### 6.2 モーダルの原則

- 1モーダル = 1操作（編集/切り離し/降格/キャンセル等）
- 送信は原則POST
- 成功時は
  - 一覧表を更新（ページ再読込 or DataTablesの再描画）
  - 判断不要の通知はNotyf success で自動クローズ

### 6.3 一覧→更新（モーダル）→一覧 のUX要件（追加）

目的: モーダルで更新後に一覧へ戻った際、検索/ページ/並び順などが初期化されないようにする。

要件:
- 更新後、**ページ全体のリロードはしない**（初期状態に戻るのを防ぐ）
- DataTablesの状態（検索/ページング/ソート/フィルタ）を維持したまま、更新対象のデータだけを反映する

実装指針（推奨）:
- DataTablesの `stateSave` を有効化し、状態を維持する
- 更新成功時は、次のいずれかでテーブルデータを差分更新する
  - 対象行のデータを `row().data(...)` で差し替えて `draw(false)`（ページ位置を維持）
  - サーバサイド方式の場合は `ajax.reload(null, false)`（ページ位置を維持）

禁止:
- 更新後に `location.reload()` で一覧を再読込して初期化する

備考:
- どの行を更新するかを特定できるよう、各行に一意なキー（例: JobDefinition.id / JobRun.id / worker_id）を保持する

## 7. Notyf（自動で閉じる通知）

### 7.1 対象

- 成功（保存完了/切り離し指示送信/再割当開始 等）
- 失敗でも「ユーザーが追加判断不要」なもの（単純なバリデーション失敗はフォームに表示）

### 7.2 ルール

- Notyfは自動クローズ（durationを設定）
- 例外: 重大障害（操作継続が危険）や、ユーザー判断が必要な場合はモーダルで表示

## 8. アクションのUIルール（運用操作）

- 危険操作（切り離し/降格/キャンセル）は、必ず確認モーダルを挟む
- 取り消し可能な操作は、その旨を文言に含める
- 操作後は必ず監査ログへ記録される前提でUI表示する（いつ誰が行ったかは別画面で追える）

## 9. エラーハンドリング

- フォーム入力エラー: モーダル内のBootstrap validationで表示
- 通信/サーバエラー:
  - 判断不要ならNotyf error
  - リトライ/判断が必要ならモーダルで詳細表示

## 10. 静的ファイルの方針

- Bootstrap/DataTables/Notyfの読込は以下のどちらかに統一
  - Django static に同梱（閉域/運用を優先）

MVPでは「CDNでも可」だが、本番/閉域を想定するなら static 同梱を推奨。

## 10.1 設定値（SCHEDULER_*）の扱い

- 運用/表示に関わる設定値（例: `SCHEDULER_PROMETHEUS_URL`）は、原則 **Ops の Settings メニューで編集**し、DB（`SchedulerSetting`）の上書きを使う。
- UI機能追加のために `.env` / 環境変数の追加を必須にしない（開発者ごとの設定差で画面が再現できなくなるため）。
- 新しい設定キーを追加する場合は、Settings 画面に出るよう `SchedulerSettingHelp`（help seed）へ定義を追加する。
- 例外: `_NON_OVERRIDABLE_KEYS`（例: `SCHEDULER_NODE_ID`）のように「プロセス/環境に結びつく値」はDBで上書きしない。

## 11. チェックリスト（PR時）

- 一覧はDataTablesでPaging/検索/ソートが有効
- 更新/メッセージ系はモーダルで完結
- 判断不要の通知はNotyfで自動クローズ
- 危険操作は確認モーダルあり
- Bootstrapの標準クラスを使い、独自CSSが肥大化していない
- バックログ化した項目がある場合、[docs/roadmap.md](docs/roadmap.md) に追記済み
