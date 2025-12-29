# gRPC API 設計（Worker制御）

作成日: 2025-12-28

## 0. 目的

Leader/SubLeader が Worker に対して、生存確認・状態取得・ジョブ開始/停止・drain を行うための gRPC API を定義する。

本設計は [docs/architecture.md](docs/architecture.md) の「epoch（フェンシング）」を前提とする。

## 1. 接続モデル

- **各 Worker プロセスが gRPC サーバとして待ち受ける**
- Leader/SubLeader は Redis の `grpc_host/grpc_port` を参照して Worker に接続する
- 単一ノード構成では `localhost` 通信になり得る

## 2. 認証/暗号（mTLS）

### 2.1 前提

- gRPC は **mTLS を必須**とする
- 認証単位は「Workerプロセス」または「ノード」

### 2.2 証明書の運用（案）

本基盤導入時に「社内CAを立てる/運用する」ハードルが高いケースを想定し、以下の2案を用意する。

#### 案A（デフォルト）: 独自CAなし（自己署名 + 相互ピン留め）

- 各ノード（またはWorker）が自己署名証明書（サーバ/クライアント兼用）を生成する
- 各プロセスは「接続先として許可する証明書（またはフィンガープリント）」のリストを保持する
  - 配布経路はアプリ設定（env/設定ファイル/Secret管理）など、運用に合わせる
- TLSハンドシェイク時に、相手提示の証明書が許可リストに含まれない場合は接続を拒否する

ポイント:
- CAを用意しなくてもmTLS相当の相互認証が可能（ただし証明書の配布・更新を自前で行う必要がある）
- スケール（30〜50台）を想定すると「証明書配布の自動化（Secret管理）」が重要

### 2.3 自己署名（ピン留め）証明書の自動更新

自己署名（相互ピン留め）を運用に乗せる場合、実質的に必要なのは次の2点。

1. **新しい証明書の配布**（各Pod/各ノードへ）
2. **プロセスが証明書を再読み込み**（無停止または短停止で）

この設計書では、環境別に「現実的に回る」案を提示する。

### 2.3.1 運用パターン別の推奨（要件: 混在なし）

本プロジェクトでは、以下3パターンを想定する。

1. K8s + Mesh（推奨）
2. K8s + 標準機能（Meshなし）
3. コンテナ環境（Docker / AWS ECS など）

#### パターン1: K8s + Mesh（推奨）

**結論**: 証明書の自動更新はメッシュに任せ、アプリは原則“証明書更新を意識しない”。

- Istio/Linkerd/App Mesh などのメッシュにより、サイドカー間でmTLSを実施
- メッシュが証明書発行/更新を自動で行う

メリット:
- 証明書ローテーションをアプリが意識しなくてよい
- “自己署名の配布/ピン留めリスト更新”といった運用をアプリから切り離せる

注意:
- この場合の「mTLS必須」は、アプリ直のmTLSではなく「通信経路としてmTLS必須」と読み替える
- どうしてもアプリ直mTLSが必要なら、パターン2の方式を併用する（運用は重くなる）

#### パターン2: K8s + 標準機能（Meshなし）

**結論**: `cert-manager + Secret更新 + ローリング再起動（またはホットリロード）` が現実的。

- cert-managerで `Secret(tls.crt/tls.key)` を周期更新
  - 自己署名で回す場合は `selfSigned` Issuer を利用
  - （オプション）クラスタ内だけの軽量CAを持てるなら `CA` Issuer も可
- PodはSecretをvolumeでマウントし、更新に追随する

更新の反映方法（選択）:
- **簡単（推奨MVP）**: Secret更新後にDeploymentをローリング再起動
  - Workerが複数いる前提なら、全体として無停止にしやすい
- **無停止**: アプリがファイル更新を検知してTLSコンテキストを差し替える（2.3.2参照）

##### パターン2を本設計のMVPとする

本プロジェクトでは、まず **「K8s + 標準機能（Meshなし） + cert-manager + ローリング再起動」** をMVPの前提とする。

理由:
- Mesh導入が不要
- 証明書ローテーションはcert-managerで自動化できる
- 再起動はK8s標準のローリング更新で実現でき、Worker複数なら停止影響を最小化できる

##### 最小構成のYAML例（概念）

自己署名Issuer + 証明書Secret（例）:

```yaml
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: scheduler-selfsigned
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: scheduler-grpc-tls
spec:
  # デフォルト: 共有Secret（全Podで同一）
  secretName: scheduler-grpc-tls
  duration: 2160h        # 90d（例）
  renewBefore: 360h      # 15d前に更新（例）
  commonName: scheduler-grpc
  dnsNames:
    - scheduler-grpc
  issuerRef:
    name: scheduler-selfsigned
```

Deploymentへのマウント（例）:

```yaml
volumes:
  - name: grpc-tls
    secret:
      # デフォルト: 共有Secret（全Podで同一）
      secretName: scheduler-grpc-tls
containers:
  - name: scheduler-worker
    volumeMounts:
      - name: grpc-tls
        mountPath: /etc/scheduler/tls
        readOnly: true
    env:
      - name: SCHEDULER_TLS_CERT_FILE
        value: /etc/scheduler/tls/tls.crt
      - name: SCHEDULER_TLS_KEY_FILE
        value: /etc/scheduler/tls/tls.key
```

##### パス/Secret名の規約（確定）

- TLSファイルのデフォルトパスは上記の通り、`/etc/scheduler/tls/tls.crt` と `/etc/scheduler/tls/tls.key` を採用する
- Secretは **全Podで同一をデフォルト**（`scheduler-grpc-tls`）とする
- 将来、Podごとに証明書を分けたい場合は「デプロイ設定でSecret名を差し替える」形で対応する
  - アプリ側は `SCHEDULER_TLS_CERT_FILE` / `SCHEDULER_TLS_KEY_FILE` のみ参照し、Secret名そのものは意識しない

注（K8sの制約）:
- `volumes[].secret.secretName` は実行時に環境変数で動的に切り替えられないため、Podごとに変える場合は Helm/Kustomize 等のデプロイテンプレートでSecret名を生成・注入する

例（HelmのvaluesでSecret名を切替）:

```yaml
# values.yaml
tls:
  secretName: scheduler-grpc-tls  # デフォルト（共有）
```

```yaml
# deployment.yaml（概念）
secretName: {{ .Values.tls.secretName }}
```

例（PodごとにSecretを分ける発想）:
- StatefulSetで `scheduler-worker-0-tls` / `scheduler-worker-1-tls` のようにSecretを別名で用意し、テンプレートで割り当てる

##### 自動ローリング再起動（K8s標準リソースのみ）

ホットリロードを実装しないMVPでは、証明書更新後にPod再起動が必要になる。
追加コントローラを入れずに自動化したい場合、以下のような「CronJob + RBAC」で `kubectl rollout restart` 相当の操作を行う。

方針:
- 証明書更新はcert-managerが行う
- 再起動は定期的に実行（例: 1日1回）し、更新取りこぼしを防ぐ
  - 更新の“変更検知”まで標準機能だけで厳密にやるのは難しいため、MVPは「定期再起動」で割り切る

例（概念）:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: scheduler-rollout
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: scheduler-rollout
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: scheduler-rollout
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: scheduler-rollout
subjects:
  - kind: ServiceAccount
    name: scheduler-rollout
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: scheduler-rollout-restart
spec:
  schedule: "0 3 * * *" # 毎日3時（例）
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: scheduler-rollout
          restartPolicy: Never
          containers:
            - name: kubectl
              image: bitnami/kubectl:latest
              command:
                - /bin/sh
                - -c
                - |
                  kubectl -n $NAMESPACE patch deployment scheduler-worker \
                    -p '{"spec":{"template":{"metadata":{"annotations":{"scheduler/restartedAt":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)'""}}}}}'
              env:
                - name: NAMESPACE
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.namespace
```

注:
- これは「Secret更新を検知して即時再起動」ではなく「更新を取りこぼさないための定期再起動」
- より厳密にやるなら、次段階でホットリロード（2.3.2）または専用コントローラ導入を検討する

#### パターン3: コンテナ環境（Docker / AWS ECS など）

**結論**: “自己署名 + ピン留め”を維持しつつ、更新は「Secret配布 + タスク/コンテナのローリング再配置」で回すのが現実的。

配布の置き場（例）:
- AWS ECS: AWS Secrets Manager / SSM Parameter Store
- Docker: composeのsecrets、またはボリューム、またはSecret管理基盤

更新の反映方法:
- 新しい証明書（と許可するフィンガープリント/証明書リスト）をSecretへ投入
- サービスをローリング更新（ECSなら新しいタスクが起動し、古いタスクが順次停止）

補足:
- 「コンテナ単体で完全自動更新 + 無停止」を自己署名だけで実現するのは難易度が高い
- Workerが複数で動く前提なら、ローリング更新で運用上の停止をほぼ回避できる

### 2.3.2 アプリ側要件（証明書ホットリロード）

自動更新を「再起動なし」で成立させるには、サーバ/クライアント双方でTLS設定の差し替えが必要。

- サーバ側: 証明書ファイル（またはSVID）更新を検知して、新しいTLSコンテキストを適用
- クライアント側: 接続確立時に最新の証明書/検証設定を使う

MVPの割り切り（簡単にする場合）:
- 更新時はPod/プロセスをローリング再起動する（短停止を許容）
  - Worker数が複数なら全体としては無停止運用になりやすい

### 2.3.3 開発環境の扱い（長期証明書）

- 開発/検証では「実質無期限（長期）」の自己署名証明書を使う運用を許容する
- 本番相当では短命 + 自動更新（K1/K2/K3 or N1/N2）を推奨する

#### 案B（オプション）: 社内CA/PKIを利用

- CA（社内CA/PKI）を用意し、各ノードにクライアント/サーバ証明書を配布
- SAN（Subject Alternative Name）に `node_id` やホスト名を含め、最低限のなりすまし防止を行う
- 証明書ローテーションを想定（期限短め + 自動更新）

## 3. API一覧（サービス）

### 3.1 WorkerService

- `Ping`
- `GetStatus`
- `StartJob`
- `CancelJob`
- `Drain`
- `ConfirmContinuation`（切り離し中の継続判定）

## 4. メッセージ（フィールド方針）

### 4.1 共通

- `worker_id`: Redisで採番されたワーカーID
- `node_id`: ノード識別子
- `leader_epoch`: フェンシングトークン。Leaderがロック獲得ごとにINCRした値。
  - Workerは古いepochの指示を拒否する
- `job_run_id`: RDBのJobRunのID

## 5. proto相当の定義（擬似）

※実装言語に依存しないよう、ここでは擬似protoとして示す。

```proto
syntax = "proto3";

package scheduler.v1;

service WorkerService {
  rpc Ping(PingRequest) returns (PingResponse);
  rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);

  rpc StartJob(StartJobRequest) returns (StartJobResponse);
  rpc CancelJob(CancelJobRequest) returns (CancelJobResponse);

  rpc Drain(DrainRequest) returns (DrainResponse);

  rpc ConfirmContinuation(ConfirmContinuationRequest) returns (ConfirmContinuationResponse);
}

message PingRequest {
  string caller_role = 1; // "leader" or "subleader" (監査用)
  int64 leader_epoch = 2;
}
message PingResponse {
  string worker_id = 1;
  string node_id = 2;
  int64 observed_leader_epoch = 3;
  int64 now_unix_ms = 4;
}

message GetStatusRequest {
  string caller_role = 1;
  int64 leader_epoch = 2;
}
message GetStatusResponse {
  string worker_id = 1;
  string node_id = 2;
  string role = 3;          // leader/subleader/worker
  bool detached = 4;
  bool draining = 5;
  int32 load = 6;
  string current_job_run_id = 7;
  int64 observed_leader_epoch = 8;
  int64 last_heartbeat_unix_ms = 9;
}

message StartJobRequest {
  int64 leader_epoch = 1;
  string job_run_id = 2;

  string command_name = 3;
  string args_json = 4; // Django command 引数

  int32 timeout_seconds = 5;
  int32 attempt = 6;
}
message StartJobResponse {
  enum Result {
    RESULT_UNSPECIFIED = 0;
    ACCEPTED = 1;
    REJECTED_OLD_EPOCH = 2;
    REJECTED_DETACHED = 3;
    REJECTED_DRAINING = 4;
    REJECTED_ALREADY_RUNNING = 5;
    REJECTED_INVALID = 6;
  }
  Result result = 1;
  string message = 2;
}

message CancelJobRequest {
  int64 leader_epoch = 1;
  string job_run_id = 2;
  string reason = 3;
}
message CancelJobResponse {
  enum Result {
    RESULT_UNSPECIFIED = 0;
    ACCEPTED = 1;
    REJECTED_OLD_EPOCH = 2;
    NOT_FOUND = 3;
    ALREADY_FINISHED = 4;
  }
  Result result = 1;
  string message = 2;
}

message DrainRequest {
  int64 leader_epoch = 1;
  bool enable = 2; // true: drain on, false: drain off
}
message DrainResponse {
  bool draining = 1;
}

message ConfirmContinuationRequest {
  // Workerが「切り離し中に継続してよいか」を問い合わせる
  // 方向: Worker -> (Leader or SubLeader)
  // 実装上は「Leader/SubLeaderもWorkerプロセスである」ため、Leader/SubLeaderのgRPCエンドポイントへ
  // 同じ WorkerService を呼び出す形で成立させる。
  int64 leader_epoch = 1;
  string worker_id = 2;
  string job_run_id = 3;
}
message ConfirmContinuationResponse {
  enum Decision {
    DECISION_UNSPECIFIED = 0;
    ALLOW_CONTINUE = 1;
    MUST_ABORT = 2;
  }
  Decision decision = 1;
  string message = 2;
}
```

## 6. タイムアウト/リトライ指針

- Ping/GetStatus
  - deadline: 200ms〜500ms（環境で調整）
  - リトライ回数は短め（例: 2〜3回）
- StartJob/CancelJob
  - deadline: 1s〜数秒
  - 同一job_run_idへの重複送信が起きても、Workerは冪等に扱えるようにする

## 7. エラー設計

- アプリケーションレベルの拒否（epoch古い等）は Response の `result` で返す
- 通信失敗（UNAVAILABLE/DEADLINE_EXCEEDED）は呼び出し側で扱い、切り離し判定へ利用する

## 8. 未決事項（実装時に確定）

- `ConfirmContinuation` の判定材料
  - Leader/SubLeader側で「そのJobRunが当該workerに割り当たっているか」「epochが妥当か」「再割当/キャンセルされていないか」を確認して返す
  - 返却が `MUST_ABORT` の場合、Workerはジョブを停止し、DBへ失敗系で記録する
