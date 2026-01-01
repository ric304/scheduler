# 開発環境 構築手順（Windows + WSL / ローカル）

作成日: 2025-12-28

この手順は、設計書ベースのジョブ実行基盤を **ローカル開発で動かす** ための環境構築を整理したものです。

想定:
- OS: Windows + WSL2（例: Ubuntu）
- 開発の実行場所:
  - 推奨: VS Code の Remote - WSL で、WSL側でPython/Django/Redis/PostgreSQL を動かす
  - 代替: Windows側でPython、依存サービスはWSL側（ポートフォワード）でも可
- Python: 3.13（例: `python -m venv venv`）
- 依存サービス: Redis + RDB（開発ではSQLiteでも開始可能だが、推奨はPostgreSQL）
- gRPC: mTLS（開発は長期自己署名証明書を許容）

補足（K8sについて）:
- テスト効率のため、当面は **K8s環境（kind/minikube等）の構築は後回し** にします。
  - ローカル（WSL + docker compose + 複数 `scheduler_worker`）でフェーズF（実ジョブ実行）まで進められます。
  - K8sマニフェスト/証明書更新（M7）は後段でまとめて整備します。

---

## 1. 前提ソフト

- Windows: WSL2（Ubuntu等）
- WSL: Python 3.13, Git
- WSL: Docker Engine（Docker Desktopは不要）
- Windows: VS Code

### 1.1 WSL2（Ubuntu）の準備（概要）

- WSL2を有効化し、Ubuntuをインストール
- VS Code で Remote - WSL を使用して、WSL上で作業することを推奨

### 1.2 VS Code（Remote - WSL）セットアップ（推奨）

VS CodeはWindows側にインストールし、実作業はWSL上で行います。

1) Windows側に VS Code をインストール

2) VS Code 拡張をインストール（Windows側）

- Remote - WSL（拡張ID: `ms-vscode-remote.remote-wsl`）

3) WSL上のフォルダを VS Code で開く

- WSLターミナルでリポジトリへ移動し、`code .`
  - 例: `cd /mnt/c/vscode/Scheduler && code .`

4) Pythonインタープリタを venv に合わせる

- コマンドパレット: `Python: Select Interpreter`
- `./venv/bin/python`（WSL側）を選択

注: Remote - WSLでは、拡張は「Windows側」と「WSL側」で別々に入ります。
Python系の拡張は、WSLで開いたワークスペース側にもインストールしてください。

### 1.3 WSLへ Docker Engine を入れる（Docker Desktopなし）

WSLのUbuntu上で実施。

1) 依存パッケージ

- `sudo apt-get update`
- `sudo apt-get install -y ca-certificates curl gnupg`

2) Dockerリポジトリ設定（例）

- `sudo install -m 0755 -d /etc/apt/keyrings`
- `curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg`
- `echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null`

3) Dockerインストール

- `sudo apt-get update`
- `sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`

4) 自分をdockerグループへ（ログインし直す）

- `sudo usermod -aG docker $USER`

5) 動作確認

- `docker version`
- `docker ps`

### 1.4 推奨 VS Code 拡張（WSL側にも入れる）

必須（推奨）:
- Python（拡張ID: `ms-python.python`）
- Pylance（拡張ID: `ms-python.vscode-pylance`）320.
- Ruff（拡張ID: `charliermarsh.ruff`）

あると便利:
- Docker（拡張ID: `ms-azuretools.vscode-docker`）
- YAML（拡張ID: `redhat.vscode-yaml`）
- EditorConfig（拡張ID: `editorconfig.editorconfig`）

注: このリポジトリには推奨拡張の一覧として `.vscode/extensions.json` を同梱しています。
VS Code の「Recommended」からまとめてインストールできます。

### 1.5 推奨 VS Code 設定（任意）

注: このリポジトリには、最小限の開発設定として `.vscode/settings.json` を同梱しています。
好みに合わせて調整してください。

---

## 2. Python 仮想環境

推奨: WSL側（例: `/mnt/c/vscode/Scheduler` または WSLホーム配下）で実行。

1) venv作成

- `python -m venv venv`

2) venv有効化（bash）

- `source venv/bin/activate`

3) pip更新

- `python -m pip install -U pip`

---

## 3. 依存サービスの起動（Redis / PostgreSQL）

### 3.1 WSL上のDockerで起動（推奨）

推奨: リポジトリ同梱の `docker-compose.yml` を使用（WSL上で実行）

起動:
- `docker compose up -d`

確認:
- `docker compose ps`

### 3.1.1 オブジェクトストレージ（MinIO）

`docker-compose.yml` には開発用のMinIO（S3互換）を含みます。

- S3 API: `http://127.0.0.1:9000`
- Console: `http://127.0.0.1:9001`（user/pass は `minioadmin` / `minioadmin`）
- バケット: `scheduler-logs`（起動時に自動作成、匿名ダウンロードを許可）

ジョブログを実行後にMinIOへ退避して `log_ref` をURL化する場合は、環境変数を有効化します:

- `SCHEDULER_LOG_ARCHIVE_ENABLED=1`
- `SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL=http://127.0.0.1:9000`
- `SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL=http://127.0.0.1:9000`
- `SCHEDULER_LOG_ARCHIVE_BUCKET=scheduler-logs`
- `SCHEDULER_LOG_ARCHIVE_ACCESS_KEY_ID=minioadmin`
- `SCHEDULER_LOG_ARCHIVE_SECRET_ACCESS_KEY=minioadmin`

ローカルログの扱い（Worker側）:

- アップロード成功時にローカルログを削除する: `SCHEDULER_LOG_LOCAL_DELETE_AFTER_UPLOAD=1`
- 指定時間（hours）を超えたローカルログを削除する: `SCHEDULER_LOG_LOCAL_RETENTION_HOURS=168`
  - `0` の場合は無効（削除しない）

停止:
- `docker compose down`

データも削除（初期化）:
- `docker compose down -v`

（参考）個別に `docker run` する場合:

- Redis
  - `docker run --name scheduler-redis -p 6379:6379 -d redis:7-alpine`

- PostgreSQL
  - `docker run --name scheduler-postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=scheduler -p 5432:5432 -d postgres:16-alpine`

確認:
- Redis: `docker logs scheduler-redis`
- Postgres: `docker logs scheduler-postgres`

停止/削除:
- `docker stop scheduler-redis scheduler-postgres`
- `docker rm scheduler-redis scheduler-postgres`

### 3.2 Dockerを使わない場合

- WSL上で Redis/PostgreSQL を `apt` でインストールして起動してもよい
- ただし環境差分が出やすいため、開発はDockerを推奨

---

## 4. Django プロジェクト側の準備（組み込み形）

本リポジトリは現時点で設計書中心のため、実装着手後は以下の形になります。

- `scheduler`（reusable app）を既存Djangoプロジェクトへ追加
- `INSTALLED_APPS` に `scheduler` と `scheduler_ops`（運用UI）を追加

開発開始時点で最低限必要になる設定（案）:

- Redis接続
  - `SCHEDULER_REDIS_URL=redis://localhost:6379/0`
- DB接続（PostgreSQL推奨）
  - `DATABASE_URL=postgres://postgres:postgres@localhost:5432/scheduler`
- gRPC bind
  - `SCHEDULER_GRPC_HOST=127.0.0.1`
  - ポートは `SCHEDULER_GRPC_PORT_RANGE_START/END` から自動選択（複数worker起動を想定）
- TLSファイル（設計で固定）
  - `SCHEDULER_TLS_CERT_FILE=/etc/scheduler/tls/tls.crt`
  - `SCHEDULER_TLS_KEY_FILE=/etc/scheduler/tls/tls.key`

注: Windowsローカルでは `/etc/...` が存在しないため、**開発ではWindowsパスに差し替える**運用にします。
例:
- `SCHEDULER_TLS_CERT_FILE=C:\vscode\Scheduler\dev-certs\tls.crt`
- `SCHEDULER_TLS_KEY_FILE=C:\vscode\Scheduler\dev-certs\tls.key`

推奨（WSLで実行する場合）:
- `SCHEDULER_TLS_CERT_FILE=/mnt/c/vscode/Scheduler/dev-certs/tls.crt`
- `SCHEDULER_TLS_KEY_FILE=/mnt/c/vscode/Scheduler/dev-certs/tls.key`

（本番K8sではSecretを `/etc/scheduler/tls` にマウントする前提）

---

## 4.1 設定ヘルプ（fixtures）

運用UIの Settings で表示する「意味/影響/制限（min/max等）」はDBに格納します。

初期セットアップ（新規DB）では fixtures をロードします:

- `python manage.py loaddata scheduler_setting_help`

注: 既に `SchedulerSettingHelp` が作成済みのDBに対して `loaddata` すると、キー重複で失敗する場合があります。

既にDBがあり、空のヘルプ行が存在する場合は、同梱のデフォルトを上書き適用できます:

- `python manage.py scheduler_seed_setting_help --apply-defaults`

## 5. イベント投入（M4）の動作確認

ローカルで `POST /api/events/ingest/` を叩いて、イベント→JobRun生成までを確認するためのスクリプトです。

- curl（WSL想定）: [scripts/ingest_event.sh](../scripts/ingest_event.sh)
- Python: [scripts/ingest_event.py](../scripts/ingest_event.py)

---

## 5. 開発用の自己署名証明書（長期）

設計では開発環境は「実質無期限（長期）」証明書運用を許容します。

### 5.1 OpenSSL が使える場合（推奨）

1) フォルダ作成
- `mkdir -p dev-certs`

2) 鍵/証明書作成（例: 10年）

- `openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes -keyout dev-certs/tls.key -out dev-certs/tls.crt -subj "/CN=scheduler-grpc"`

3) 環境変数に反映
- WSLで実行する場合（例）
  - `SCHEDULER_TLS_CERT_FILE=/mnt/c/vscode/Scheduler/dev-certs/tls.crt`
  - `SCHEDULER_TLS_KEY_FILE=/mnt/c/vscode/Scheduler/dev-certs/tls.key`
- Windowsで実行する場合（例）
  - `SCHEDULER_TLS_CERT_FILE=C:\vscode\Scheduler\dev-certs\tls.crt`
  - `SCHEDULER_TLS_KEY_FILE=C:\vscode\Scheduler\dev-certs\tls.key`

### 5.2 OpenSSLが無い場合

- 代替として `mkcert` 等の利用も検討可能
- あるいは（短期的には）mTLSを無効化してlocalhost限定で動作確認 → 後でmTLSを有効化
  - ※ただし要件上、最終的にはmTLS前提に戻す

---

## 6. パッケージ（実装開始後）

実装が入ると、最低限以下が必要になります。

- Django
- Redisクライアント
- gRPC（サーバ/クライアント）
- （推奨）設定管理: `python-dotenv` など

このリポジトリに `requirements.txt` / `pyproject.toml` が追加されたら、ここにインストール手順を追記します。

---

## 7. ローカル起動（実装開始後）

実装完了後に想定される起動例です。

- Worker起動（複数起動）
  - `python manage.py scheduler_worker --grpc-port 50051`
  - `python manage.py scheduler_worker --grpc-port 50052`

- 運用UI起動
  - `python manage.py runserver`
  - `http://127.0.0.1:8000/ops/`

---

## 8. つまずきやすい点

- Windowsでの証明書パス
  - K8s前提の `/etc/scheduler/tls/...` はローカルでは使えないので、環境変数で差し替える
- Redis/Postgresの疎通
  - ポート競合（6379/5432）に注意
- gRPCポート
  - Workerを複数立てる場合、portをずらす

---

## 9. 次のアクション

- 実装着手（M0）として Django app 雛形と依存関係ファイル（requirements/pyproject）を追加
- それに合わせて本手順を「コマンドがそのまま動く」形に更新
