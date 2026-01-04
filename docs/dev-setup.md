# 開発環境 構築手順（Windows + WSL / ローカル / K8S）

作成日: 2025-12-28（更新: 2026-01-02）

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
- ローカル（WSL + docker compose + 複数 `scheduler_worker`）でもフェーズF（実ジョブ実行）まで進められます。
- 一方で「K8s環境での挙動確認」「CI/CD（GitOps）」「Secrets/TLSマウント」を早期に検証したい場合は、後述の **VM×3（Ubuntu 24.02 LTS）+ k3s** 手順でK8s開発環境を用意できます。

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

## 8. K8S開発環境（Ubuntu 24.02 LTS VM×3 / k3s）

この章は「K8s環境でSchedulerを動かして検証する」ための最小構成です。

想定:
- VM1: control plane（例: `k8s-master`）
- VM2: worker（例: `k8s-worker`）
- VM3: worker（例: `k8s-worker2`）
- いずれも Ubuntu 24.02 LTS
- 3台は相互に疎通可能（固定IP推奨）

注意:
- これはあくまで「K8s上のアプリケーション検証」のための開発環境です。
- K8s自体の可用性（マルチマスター等）は考慮しません（シングルマスター構成）。

### 8.1 VM共通の下準備

両VMで実行:

1) OS更新
- `sudo apt-get update && sudo apt-get -y upgrade`

2) Swap無効化（K8s要件）
- `sudo swapoff -a`
- `/etc/fstab` の swap 行をコメントアウト（永続化）

3) 依存
- `sudo apt-get install -y curl ca-certificates gnupg lsb-release`

4) ホスト名/名前解決（任意だが推奨）
- 例: `/etc/hosts` に相互の `k8s-master` / `k8s-worker` を追加

### 8.2 k3sのインストール

#### 8.2.1 VM1（control plane）

VM1で実行（例: `k8s-master`）:

注: この手順では Ingress と LoadBalancer を別途入れるため、k3s標準の Traefik / ServiceLB を無効化します。

- `curl -sfL https://get.k3s.io | sh -s - server --write-kubeconfig-mode 644 --disable traefik --disable servicelb`

推奨: 安定版を明示的に指定してインストールします（`INSTALL_K3S_VERSION` を使用）。

```bash
# 例: 安定の既知バージョンを指定
export INSTALL_K3S_VERSION="v1.30.7+k3s1"
curl -sfL https://get.k3s.io | sh -s - server --write-kubeconfig-mode 644 --disable traefik --disable servicelb
```

トラブルシューティング（ダウンロード失敗や 404 が出る場合）:

- IPv6 経由で接続が不安定な環境では IPv4 を強制して試してください。

```bash
curl -4 -sfL https://get.k3s.io | sh -s - server --write-kubeconfig-mode 644 --disable traefik --disable servicelb
```

- インストーラが GitHub リリースを取得できない（ネットワーク制限やプロキシ等）場合は、バイナリを手動で配置してインストーラのダウンロード工程をスキップできます。

```bash
# 1) 使いたいバージョンを明示してバイナリを配置（例）
export K3S_VER="v1.30.7+k3s1"
curl -fL --retry 5 -o /usr/local/bin/k3s "https://github.com/k3s-io/k3s/releases/download/${K3S_VER}/k3s"
chmod +x /usr/local/bin/k3s

# 2) インストーラにダウンロードをスキップさせてセットアップ
export INSTALL_K3S_SKIP_DOWNLOAD=true
curl -sfL https://get.k3s.io | sh -s - server --write-kubeconfig-mode 644 --disable traefik --disable servicelb
```

確認:
- `sudo kubectl get nodes`

#### 8.2.2 VM2（worker）

VM1で token を取得:
- `sudo cat /var/lib/rancher/k3s/server/node-token`

VM2で実行（`<MASTER_IP>` と `<TOKEN>` を置換）:

- `curl -sfL https://get.k3s.io | K3S_URL=https://<MASTER_IP>:6443 K3S_TOKEN=<TOKEN> sh -s - agent`

#### 8.2.3 VM3（worker2）

VM3（`k8s-worker2`）もVM2と同様にjoinします（`<MASTER_IP>` と `<TOKEN>` を置換）:

- `curl -sfL https://get.k3s.io | K3S_URL=https://<MASTER_IP>:6443 K3S_TOKEN=<TOKEN> sh -s - agent`

またはマスターと同じバージョンを明示して agent を入れる場合:

```bash
export INSTALL_K3S_VERSION="v1.30.7+k3s1"
K3S_URL=https://<MASTER_IP>:6443 K3S_TOKEN=<TOKEN> curl -sfL https://get.k3s.io | sh -s - agent
```

確認（VM1で）:
- `sudo kubectl get nodes -o wide`

### 8.3 kubectl を手元PC（WSL）から叩く（任意）

運用UI/デバッグをローカルから見たい場合、`kubeconfig` を手元へ持ってきます。

VM1の kubeconfig:
- `/etc/rancher/k3s/k3s.yaml`

ポイント:
- `server: https://127.0.0.1:6443` を `https://<MASTER_IP>:6443` に変更
- 手元で `kubectl` を使う場合は `~/.kube/config` に配置し `KUBECONFIG` を設定

### 8.4 Ingress / LoadBalancer / TLS

VM環境ではクラウドLBが無いので、以下を入れておくと検証が楽です。

事前: VM1に Helm をインストール（例）
- `curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash`

1) Ingress（推奨: ingress-nginx）
- `helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx`
- `helm repo update`
- `helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace`

2) LoadBalancer（推奨: MetalLB）
- `helm repo add metallb https://metallb.github.io/metallb`
- `helm repo update`
- `helm upgrade --install metallb metallb/metallb -n metallb-system --create-namespace`

MetalLBはアドレスプール設定が必要です。
例（VMネットワークの未使用IP範囲を指定）:

```yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: pool
  namespace: metallb-system
spec:
  addresses:
    - 192.168.3.240-192.168.3.250
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: l2
  namespace: metallb-system
spec: {}
```

適用:
- `kubectl apply -f metallb-pool.yaml`

3) TLS管理（任意、推奨: cert-manager）
- `helm repo add jetstack https://charts.jetstack.io`
- `helm repo update`
- `helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true`

### 8.5 監視（Prometheus / Alertmanager）

本リポジトリのOps UIは Prometheus/Alertmanager のURLをSettingsから参照できます。

推奨（OSS）:
- kube-prometheus-stack（Prometheus + Alertmanager + Grafana）

例:
- `helm repo add prometheus-community https://prometheus-community.github.io/helm-charts`
- `helm repo update`
- `helm upgrade --install monitoring prometheus-community/kube-prometheus-stack -n monitoring --create-namespace`


GrafanaのAdminユーザ/パスワード:
 kubectl --namespace monitoring get secrets monitoring-grafana -o jsonpath="{.data.admin-password}" | base64 -d ; echo

 NAME                                      TYPE           CLUSTER-IP      EXTERNAL-IP     PORT(S)                      
monitoring-grafana                        LoadBalancer   10.43.45.196    192.168.3.241   80:31842/TCP                    
monitoring-kube-prometheus-alertmanager   LoadBalancer   10.43.87.90     192.168.3.242   9093:32659/TCP,8080:31057/TCP   
monitoring-kube-prometheus-prometheus     LoadBalancer   10.43.77.229    192.168.3.243   9090:30359/TCP,8080:31077/TCP  

上記３つのSVCをClusterIPからLoadBalancerに変更しているため、`EXTERNAL-IP` に変更し、外部よりアクセス可能に設定する。

### 8.6 依存サービス（Redis / PostgreSQL / MinIO）

ここでは「クラスタ内（K8s上）に Redis / PostgreSQL / MinIO をデプロイする」詳細手順を示します。
事前: クラスタに StorageClass（例: `local-path` / `standard`）があることを確認してください。k3s 環境では `local-path` が既定の場合が多いです。

1) 名前空間と Helm リポジトリの追加

```bash
kubectl create namespace scheduler-infra
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
```

2) Redis（冗長構成: Sentinel レプリケーション） — k8s-master と k8s-worker に分散配置する例

まずノードにラベルを付けて分散を観察しやすくします（任意）:

```bash
kubectl label node k8s-master redis-role=preferred
kubectl label node k8s-worker redis-role=preferred
```

`redis-values.yaml` のサンプル（Bitnami chart の replication + sentinel を使用）:

```yaml
architecture: replication
auth:
  password: "redispassword"
global:
  storageClass: local-path
master:
  persistence:
    enabled: true
    size: 2Gi
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchExpressions:
              - key: app.kubernetes.io/name
                operator: In
                values:
                  - redis
          topologyKey: kubernetes.io/hostname
replica:
  replicaCount: 2
  persistence:
    enabled: true
    size: 2Gi
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchExpressions:
              - key: app.kubernetes.io/name
                operator: In
                values:
                  - redis
          topologyKey: kubernetes.io/hostname
sentinel:
  enabled: true
  replicaCount: 3
```

デプロイ:

```bash
helm upgrade --install scheduler-redis bitnami/redis -n scheduler-infra -f redis-values.yaml --wait
```

確認:

```bash
kubectl get pods -n scheduler-infra -l app.kubernetes.io/name=redis -o wide
kubectl get svc -n scheduler-infra
# Sentinel 情報確認 (sentinel Pod 名を指定)
kubectl exec -n scheduler-infra -it <sentinel-pod> -- redis-cli -p 26379 sentinel masters
# マスター/レプリカ情報確認 (任意の redis Pod 名を指定)
kubectl exec -n scheduler-infra -it <redis-pod> -- redis-cli -a redispassword info replication
```

接続と利用上の注意:
- Scheduler アプリは `SCHEDULER_REDIS_URL` をそのまま `redis.Redis.from_url(...)` に渡す実装です（Sentinel の「master 自動解決」を直接は行いません）。
- Bitnami Redis（replication + sentinel）を使う場合、アプリの接続先は **master Service** を指定してください: `redis://scheduler-redis-master.scheduler-infra.svc.cluster.local:6379/0`
  - フェイルオーバ時は master Pod が切り替わる想定です（切り替え中は接続が一時的に切れる可能性があります）。
- Sentinel（26379）に直接つないで master を解決したい場合は、アプリ側に Sentinel 設定を追加する実装変更が必要です。
- 本番ではパスワードを `Secret` にし、PVC サイズとリソース制限を要検討してください。

3) PostgreSQL（開発向け：単一 Primary）

```bash
helm install scheduler-postgres bitnami/postgresql -n scheduler-infra \
  --set auth.username=postgres \
  --set auth.password=postgres \
  --set auth.database=scheduler \
  --set primary.persistence.size=5Gi \
  --set global.storageClass=local-path
```

- 接続文字列（クラスタ内）: `postgresql://postgres:postgres@scheduler-postgres-postgresql.scheduler-infra.svc.cluster.local:5432/scheduler`

4) MinIO（S3 互換ストレージ）

```bash
helm install scheduler-minio bitnami/minio -n scheduler-infra \
  --set auth.rootUser=minioadmin \
  --set auth.rootPassword=minioadmin \
  --set persistence.size=10Gi \
  --set global.storageClass=local-path
```

- 内部エンドポイント: `http://scheduler-minio.scheduler-infra.svc.cluster.local:9000`
- ローカルからコンソールを開くにはポートフォワード:

```bash
kubectl port-forward -n scheduler-infra svc/scheduler-minio 9000:9000
# open http://127.0.0.1:9000 (user=minioadmin, pass=minioadmin)
```

5) 接続情報を Kubernetes Secret に登録（例）

```bash
kubectl -n scheduler-infra create secret generic scheduler-db-credentials \
  --from-literal=DATABASE_URL='postgresql://postgres:postgres@scheduler-postgres-postgresql.scheduler-infra.svc.cluster.local:5432/scheduler'

kubectl -n scheduler-infra create secret generic scheduler-redis-credentials \
  --from-literal=REDIS_URL='redis://scheduler-redis-master.scheduler-infra.svc.cluster.local:6379/0' \
  --from-literal=REDIS_PASSWORD='redispassword'

kubectl -n scheduler-infra create secret generic scheduler-minio-credentials \
  --from-literal=MINIO_ENDPOINT='http://scheduler-minio.scheduler-infra.svc.cluster.local:9000' \
  --from-literal=MINIO_ACCESS_KEY='minioadmin' \
  --from-literal=MINIO_SECRET_KEY='minioadmin'
```

6) 動作確認

```bash
kubectl get pods -n scheduler-infra
kubectl get svc -n scheduler-infra
kubectl logs -n scheduler-infra deployment/scheduler-postgres-postgresql
```

7) 注意点 / ベストプラクティス

- StorageClass: 開発環境では `local-path` や `hostpath` を使うことが多いです。クラウド環境ではクラウド-provided StorageClass を使ってください。
- 資格情報: 開発では平文でもよいですが、本番/ステージングでは `SealedSecrets` や External Secrets を使って安全に管理してください。
- 再現性: `values.yaml` を作り `helm upgrade --install -f values.yaml` でデプロイする運用が望ましいです。
- バックアップとリストア: Postgres のバックアップ方針（定期スナップショット、pgBackRest 等）を事前に決めておいてください。

この手順でクラスタ内に Redis / PostgreSQL / MinIO を用意できます。`SCHEDULER` アプリケーション側は上記のサービス名/シークレットを利用して接続設定を行ってください。

### 8.7 Scheduler のデプロイ（概要）

ポイント:
- `SCHEDULER_DEPLOYMENT=k8s` を設定（K8s API healthを有効化）
- gRPC mTLS の証明書を `Secret` として `/etc/scheduler/tls` にマウント（設計どおり）

例: TLS Secret 作成（手元の `dev-certs/` を使う場合）

- `kubectl create namespace scheduler`
- `kubectl -n scheduler create secret generic scheduler-grpc-tls --from-file=tls.crt=dev-certs/tls.crt --from-file=tls.key=dev-certs/tls.key`

Deployment/StatefulSetの volumeMount（概念）:

```yaml
volumeMounts:
  - name: tls
    mountPath: /etc/scheduler/tls
    readOnly: true
volumes:
  - name: tls
    secret:
      secretName: scheduler-grpc-tls
```

---

## 9. CI/CD（GitHub Actions + GHCR + Argo CD / GitOps）

この章は「GitHubをリポジトリ」「無料/OSS」「K8sへ自動反映」を前提にした最小構成です。

構成:
- CI: GitHub Actions（無料枠）
- Image Registry: GHCR（GitHub Container Registry）
- CD: Argo CD（OSS、GitOps）

### 9.1 CI（GitHub Actions）概要

やること:
- PR: `python -m compileall` / `python manage.py check`（必要ならテスト追加）
- main: Docker build → GHCRへpush

メモ:
- GHCR push は `GITHUB_TOKEN` で可能（workflowに `permissions: packages: write` が必要）

### 9.2 CD（Argo CD）概要

やること:
- Argo CDをクラスタへ導入
- 監視対象リポジトリ（GitHub）に `manifests/`（または `helm/`）を置く
- Argo CD Application で `main` を追従し、自動sync

導入（例）:
- `kubectl create namespace argocd`
- `kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml`

アクセス（例）:
- 開発用途は port-forward でOK
  - `kubectl -n argocd port-forward svc/argocd-server 8080:80`

### 9.3 Secrets の扱い（OSS）

選択肢:
- シンプル: Kubernetes Secret を手動作成（開発のみ）
- GitOps向け: Sealed Secrets（OSS）
  - Gitに暗号化されたSecretをコミットできる

---

## 10. 初期設定（K8S向けの推奨値: 環境変数 + Ops Settings）

ここでは「まず動く」ための初期値をまとめます。

### 10.1 Django/アプリの環境変数（Deploymentのenv）

最低限（例）:
- `DATABASE_URL=postgresql://postgres:postgres@scheduler-postgres-postgresql.scheduler-infra.svc.cluster.local:5432/scheduler`
- `SCHEDULER_REDIS_URL=redis://scheduler-redis-master.scheduler-infra.svc.cluster.local:6379/0`
- `SCHEDULER_DEPLOYMENT=k8s`

TLS（Secretを `/etc/scheduler/tls` にマウントする前提）:
- `SCHEDULER_TLS_CERT_FILE=/etc/scheduler/tls/tls.crt`
- `SCHEDULER_TLS_KEY_FILE=/etc/scheduler/tls/tls.key`

ログアーカイブ（MinIOをK8s内で使う例）:
- `SCHEDULER_LOG_ARCHIVE_ENABLED=1`
- `SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL=http://scheduler-minio.scheduler-infra.svc.cluster.local:9000`
- `SCHEDULER_LOG_ARCHIVE_BUCKET=scheduler-logs`
- `SCHEDULER_LOG_ARCHIVE_ACCESS_KEY_ID=minioadmin`
- `SCHEDULER_LOG_ARCHIVE_SECRET_ACCESS_KEY=minioadmin`

重要:
- `SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL` は **未設定でOK**（同一URL運用の場合）
  - 未設定の場合、Ops UIは `SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL` を参照URLとして利用します。
  - 内部URLと外部URLを分けたい場合のみ、`SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL` に外部から到達できるURLを設定してください。

### 10.2 Ops UI Settings（/ops/settings/ で投入する値）

SettingsはDBに保存され、運用中に変更可能です（ConfigReloadRequestで反映）。

K8sでの推奨（例）:
- `SCHEDULER_DEPLOYMENT` : `k8s`
- `SCHEDULER_PROMETHEUS_URL` : `http://monitoring-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090`
- `SCHEDULER_ALERTMANAGER_URL` : `http://monitoring-kube-prometheus-alertmanager.monitoring.svc.cluster.local:9093`

（任意）バックログ暴走を抑える:
- `SCHEDULER_SKIP_LATE_RUNS_AFTER_SECONDS` : `300`（例: 5分）

（任意）Leader ping の負荷調整:
- `SCHEDULER_LEADER_PING_BATCH_SIZE` : `50`（環境に合わせる）

ログ参照（外部公開URLが必要なときだけ）:
- `SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL` : `https://minio.example.local` など

---

## 11. つまずきやすい点

- Windowsでの証明書パス
  - K8s前提の `/etc/scheduler/tls/...` はローカルでは使えないので、環境変数で差し替える
- Redis/Postgresの疎通
  - ポート競合（6379/5432）に注意
- gRPCポート
  - Workerを複数立てる場合、portをずらす

---

## 12. 次のアクション

- 実装着手（M0）として Django app 雛形と依存関係ファイル（requirements/pyproject）を追加
- それに合わせて本手順を「コマンドがそのまま動く」形に更新
