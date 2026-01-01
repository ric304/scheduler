from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import redis


def _k_leader_lock() -> str:
    return "scheduler:leader:lock"


def _k_subleader_lock() -> str:
    return "scheduler:subleader:lock"


def _k_leader_epoch() -> str:
    return "scheduler:leader:epoch"


def _k_worker_heartbeat(worker_id: str) -> str:
    return f"scheduler:worker:{worker_id}:heartbeat"


def _k_worker_info(worker_id: str) -> str:
    return f"scheduler:worker:{worker_id}:info"


@dataclass(frozen=True)
class WorkerInfo:
    worker_id: str
    node_id: str
    grpc_host: str
    grpc_port: int
    last_seen: float
    heartbeat_ttl_seconds: int
    is_leader: bool
    is_subleader: bool


@dataclass(frozen=True)
class ClusterLeadership:
    leader_worker_id: Optional[str]
    cluster_epoch: int


def get_cluster_leadership(redis_url: str) -> ClusterLeadership:
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    leader_worker_id = r.get(_k_leader_lock())
    raw_epoch = r.get(_k_leader_epoch())
    cluster_epoch = int(raw_epoch) if raw_epoch else 0
    return ClusterLeadership(leader_worker_id=leader_worker_id, cluster_epoch=cluster_epoch)


def list_workers(redis_url: str) -> list[WorkerInfo]:
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    leader_worker_id = r.get(_k_leader_lock())
    subleader_worker_id = r.get(_k_subleader_lock())

    workers: list[WorkerInfo] = []
    for key in r.scan_iter(match="scheduler:worker:*:info"):
        data = r.hgetall(key)
        worker_id = data.get("worker_id")
        node_id = data.get("node_id", "")
        grpc_host = data.get("grpc_host", "")
        raw_grpc_port = data.get("grpc_port")
        raw_last_seen = data.get("last_seen")

        if not worker_id or not raw_last_seen:
            continue

        try:
            last_seen = float(raw_last_seen)
        except ValueError:
            continue

        grpc_port = 0
        if raw_grpc_port:
            try:
                grpc_port = int(raw_grpc_port)
            except ValueError:
                grpc_port = 0

        ttl = r.ttl(_k_worker_heartbeat(worker_id))
        heartbeat_ttl_seconds = int(ttl) if ttl is not None and ttl > 0 else 0

        workers.append(
            WorkerInfo(
                worker_id=worker_id,
                node_id=node_id,
                grpc_host=grpc_host,
                grpc_port=grpc_port,
                last_seen=last_seen,
                heartbeat_ttl_seconds=heartbeat_ttl_seconds,
                is_leader=(leader_worker_id == worker_id),
                is_subleader=(subleader_worker_id == worker_id),
            )
        )

    workers.sort(key=lambda w: w.last_seen, reverse=True)
    return workers


@dataclass(frozen=True)
class CoordinationSettings:
    heartbeat_ttl_seconds: int = 15
    leader_lock_ttl_seconds: int = 10
    subleader_lock_ttl_seconds: int = 10


@dataclass(frozen=True)
class TickStatus:
    is_leader: bool
    is_subleader: bool
    leader_epoch: Optional[int]
    leader_worker_id: Optional[str]
    subleader_worker_id: Optional[str]
    cluster_epoch: int


_LUA_RENEW_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
  return 0
end
"""


_LUA_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"""


class RedisCoordinator:
    def __init__(
        self,
        *,
        redis_url: str,
        worker_id: str,
        node_id: str,
        grpc_host: str,
        grpc_port: int,
        settings: CoordinationSettings,
    ):
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._worker_id = worker_id
        self._node_id = node_id
        self._grpc_host = grpc_host
        self._grpc_port = grpc_port
        self._settings = settings

        self._is_leader = False
        self._leader_epoch: Optional[int] = None

        self._is_subleader = False

        self._renew_lock = self._redis.register_script(_LUA_RENEW_LOCK)
        self._release_lock = self._redis.register_script(_LUA_RELEASE_LOCK)

    def tick(self, *, now: float) -> TickStatus:
        # Heartbeat
        self._redis.set(
            _k_worker_heartbeat(self._worker_id),
            str(now),
            ex=self._settings.heartbeat_ttl_seconds,
        )
        self._redis.hset(
            _k_worker_info(self._worker_id),
            mapping={
                "worker_id": self._worker_id,
                "node_id": self._node_id,
                "grpc_host": self._grpc_host,
                "grpc_port": str(self._grpc_port),
                "last_seen": str(now),
            },
        )
        self._redis.expire(_k_worker_info(self._worker_id), self._settings.heartbeat_ttl_seconds)

        leader_lock_key = _k_leader_lock()
        subleader_lock_key = _k_subleader_lock()

        # If this process restarted with the same worker_id, it may already own locks.
        current_leader = self._redis.get(leader_lock_key)
        if not self._is_leader and current_leader == self._worker_id:
            self._is_leader = True
            raw_epoch = self._redis.get(_k_leader_epoch())
            self._leader_epoch = int(raw_epoch) if raw_epoch else None

        current_subleader = self._redis.get(subleader_lock_key)
        if not self._is_leader and not self._is_subleader and current_subleader == self._worker_id:
            self._is_subleader = True

        # Leader maintenance / acquisition
        if self._is_leader:
            renewed = int(
                self._renew_lock(
                    keys=[leader_lock_key],
                    args=[self._worker_id, str(self._settings.leader_lock_ttl_seconds * 1000)],
                )
            )
            if renewed <= 0:
                # Lost leadership
                self._is_leader = False
                self._leader_epoch = None
        else:
            # SubLeader maintenance / acquisition (only when not leader)
            if self._is_subleader:
                renewed = int(
                    self._renew_lock(
                        keys=[subleader_lock_key],
                        args=[self._worker_id, str(self._settings.subleader_lock_ttl_seconds * 1000)],
                    )
                )
                if renewed <= 0:
                    self._is_subleader = False
            else:
                acquired = self._redis.set(
                    subleader_lock_key,
                    self._worker_id,
                    nx=True,
                    ex=self._settings.subleader_lock_ttl_seconds,
                )
                if acquired:
                    self._is_subleader = True

            # Only SubLeader tries to acquire leadership aggressively.
            if self._is_subleader and not current_leader:
                acquired = self._redis.set(
                    leader_lock_key,
                    self._worker_id,
                    nx=True,
                    ex=self._settings.leader_lock_ttl_seconds,
                )
                if acquired:
                    self._is_leader = True
                    self._leader_epoch = int(self._redis.incr(_k_leader_epoch()))
                    # Once promoted, release subleader role.
                    self._is_subleader = False
                    try:
                        self._release_lock(keys=[subleader_lock_key], args=[self._worker_id])
                    except Exception:
                        pass

        leader_worker_id = self._redis.get(leader_lock_key)
        subleader_worker_id = self._redis.get(subleader_lock_key)
        leader_epoch = self._leader_epoch if self._is_leader else None

        raw_cluster_epoch = self._redis.get(_k_leader_epoch())
        cluster_epoch = int(raw_cluster_epoch) if raw_cluster_epoch else 0

        return TickStatus(
            is_leader=self._is_leader,
            is_subleader=self._is_subleader,
            leader_epoch=leader_epoch,
            leader_worker_id=leader_worker_id,
            subleader_worker_id=subleader_worker_id,
            cluster_epoch=cluster_epoch,
        )

    def shutdown(self) -> None:
        if self._is_leader:
            try:
                self._release_lock(keys=[_k_leader_lock()], args=[self._worker_id])
            finally:
                self._is_leader = False
                self._leader_epoch = None

        if self._is_subleader:
            try:
                self._release_lock(keys=[_k_subleader_lock()], args=[self._worker_id])
            finally:
                self._is_subleader = False
