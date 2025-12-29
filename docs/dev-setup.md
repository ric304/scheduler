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

---

## 1. 前提ソフト

- Windows: WSL2（Ubuntu等）
- WSL: Python 3.13, Git
- WSL: Docker Engine（Docker Desktopは不要）

### 1.1 WSL2（Ubuntu）の準備（概要）

- WSL2を有効化し、Ubuntuをインストール
- VS Code で Remote - WSL を使用して、WSL上で作業することを推奨

### 1.2 WSLへ Docker Engine を入れる（Docker Desktopなし）

WSLのUbuntu上で実施。

1) 依存パッケージ

- `sudo apt-get update`
- `sudo apt-get install -y ca-certificates curl gnupg`

2) Dockerリポジトリ設定（例）

- `sudo install -m 0755 -d /etc/apt/keyrings`
- `curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg`

3) Dockerインストール

- `sudo apt-get update`
- `sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`

4) 自分をdockerグループへ（ログインし直す）

- `sudo usermod -aG docker $USER`

5) 動作確認

- `docker version`
- `docker ps`

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
  - `SCHEDULER_GRPC_PORT=50051`
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
