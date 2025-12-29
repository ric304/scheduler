# DB整合性・索引 設計（JobRun/イベント/再割当）

作成日: 2025-12-28

## 0. 目的

ネットワーク分断・二重Leader・通信リトライが起きても、DB（RDB）を最終的な整合点として「同一JobRunの二重RUNNING」や「履歴の破壊」を防ぐ。

前提は [docs/architecture.md](docs/architecture.md) の epoch（フェンシング）とする。

## 1. JobRunの整合性（推奨ルール）

### 1.1 楽観ロック

- JobRunに `version`（int）を追加
- 更新は必ず `WHERE id=? AND version=? AND state=? ...` の条件つきにする
- 更新成功件数が0なら競合とみなし、呼び出し側で再読込/中断

### 1.2 状態遷移の許可表（MVP）

- PENDING -> ASSIGNED
- ASSIGNED -> RUNNING
- RUNNING -> SUCCEEDED / FAILED / TIMED_OUT
- ASSIGNED/RUNNING -> CANCELED
- ASSIGNED -> ORPHANED
- ORPHANED -> ASSIGNED（再割当）

「逆戻り」は禁止する。

補足（継続許容のための確認中フラグ）:

- JobRunの `state` は RUNNING のまま維持しつつ、別フィールドで `continuation_state` を持つ
  - `NONE` / `CONFIRMING`
- これにより「確認中→継続（解除）」で state の逆戻りを発生させず、再割当抑止のみを実現できる

### 1.3 epochの扱い

- `ASSIGNED -> RUNNING` のとき `leader_epoch` を書き込む
- `RUNNING -> 終了` は `leader_epoch` 一致を条件に含める
  - 古いLeader/分断側からの更新を拒否するため

### 1.4 assigned_worker_idの扱い

- `ASSIGNED -> RUNNING` は `assigned_worker_id == self.worker_id` を条件に含める
- 再割当では `assigned_worker_id` を新しいworker_idに更新し、attemptを増やす

## 2. 代表クエリとロック

### 2.1 予定時刻のJobRun作成（時間起動）

- 「同一ジョブ定義 + scheduled_for」の重複作成を避ける
- 例:
  - Unique制約: `(job_definition_id, scheduled_for)`
  - もしくは `idempotency_key = f"time:{job_definition_id}:{scheduled_for}"` を一意にする

### 2.2 割当（PENDING->ASSIGNED）

- 条件更新（楽観ロック）
- 併用: Redis `jobrun:lease:{job_run_id}`（短命）

### 2.3 開始（ASSIGNED->RUNNING）

- Workerが開始時に以下を同時に満たす更新を行う:
  - `state='ASSIGNED'`
  - `assigned_worker_id=self`
  - `version=expected`

失敗した場合は「開始権がない」と判断し、プロセス起動を行わない。

## 3. 索引（インデックス）設計

RDBをPostgreSQL/MySQLなどの一般的な構成と仮定する。

### 3.1 JobRun

- `(state, scheduled_for)`
  - Leaderが実行対象を探す
- `(assigned_worker_id, state)`
  - ワーカー詳細表示/孤児化検出
- `(job_definition_id, scheduled_for)` UNIQUE
  - 時間起動の重複作成防止
- `(created_at)`
  - 最近の履歴表示

### 3.2 Event

- `(processed_at, created_at)`
  - 未処理の取り出し
- `(dedupe_key)` UNIQUE（採用する場合）

## 4. 再割当の条件（MVP）

### 4.1 ASSIGNEDが進まない

- `assigned_at` から `reassign_after_seconds` を超過
- かつ、割当先Workerが detach/heartbeat失効

→ `state=ORPHANED` にして再割当

### 4.2 RUNNING中のワーカーが切り離し

- 原則は「継続許容ルール」に従う
- 原則は「継続許容ルール」に従う
- `continuation_state=CONFIRMING` の間は、Leaderは当該JobRunを再割当しない
  - Worker側で `continuation_check_deadline_at` を設定し、期限切れならWorkerが停止→DBを失敗系へ遷移
- 継続不可の場合:
  - Workerが自分で abort（停止）し、DBへ失敗/TIMED_OUT相当で記録
  - その後、Leaderが再実行（attempt増）

## 5. ログ保持（DB→ファイル退避）

- DBに保持する期間は `log_retention_days_db` で設定
- 退避対象:
  - JobRunに紐づくログ本文（持つ場合）
  - あるいは別テーブル（JobRunLog等）
- 退避後:
  - DB側は `log_ref` のみを残す（参照できることが重要）

## 6. マイグレーション方針（MVP）

- まずは最小のテーブル（JobDefinition, JobRun, Event, Settings, Audit）で開始
- ログ本文の保持は別テーブル化（肥大化とロック競合を避ける）
