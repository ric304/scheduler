from __future__ import annotations

import json
import random
import signal
import threading
import time
import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.db import transaction
from django.utils import timezone

from scheduler.conf import get_scheduler_config, reload_scheduler_settings_cache
from scheduler.grpc import worker_pb2
from scheduler.grpc.ports import PortRange, find_available_tcp_port
from scheduler.grpc.runtime import (
    WorkerRuntimeState,
    ping_worker,
    reload_config_on_worker,
    start_job_on_worker,
    start_worker_grpc_server,
)
from scheduler.grpc.runtime import get_status_worker
from scheduler.leader_tick import run_leader_tick_snapshot
from scheduler.models import ConfigReloadRequest, JobRun
from scheduler.redis_coordination import (
    CoordinationSettings,
    RedisCoordinator,
    TickStatus,
    list_workers,
)


class Command(BaseCommand):
    help = "Run a scheduler worker process (M1: heartbeat + leader election + epoch)."

    def add_arguments(self, parser):
        parser.add_argument("--worker-id", default="", help="Explicit worker_id (defaults to random UUID)")
        parser.add_argument(
            "--interval-seconds",
            type=float,
            default=1.0,
            help="Main loop interval (seconds)",
        )
        parser.add_argument(
            "--heartbeat-ttl-seconds",
            type=int,
            default=15,
            help="Heartbeat TTL (seconds)",
        )
        parser.add_argument(
            "--leader-lock-ttl-seconds",
            type=int,
            default=10,
            help="Leader lock TTL (seconds)",
        )
        parser.add_argument(
            "--run-seconds",
            type=int,
            default=0,
            help="If >0, stop after N seconds (useful for local tests)",
        )
        parser.add_argument(
            "--grpc-host",
            default="",
            help="Override gRPC bind host (defaults to SCHEDULER_GRPC_HOST)",
        )
        parser.add_argument(
            "--grpc-port",
            type=int,
            default=None,
            help="Override gRPC bind port. If omitted or 0, auto-select within configured range.",
        )
        parser.add_argument(
            "--grpc-port-range-start",
            type=int,
            default=None,
            help="Override gRPC auto-port range start (defaults to settings).",
        )
        parser.add_argument(
            "--grpc-port-range-end",
            type=int,
            default=None,
            help="Override gRPC auto-port range end (defaults to settings).",
        )

    def handle(self, *args, **options):
        cfg = get_scheduler_config()

        grpc_host = options.get("grpc_host") or cfg.grpc_host

        # Port selection:
        # - If --grpc-port is provided and >0: use it.
        # - If omitted or 0: choose a free port within settings range.
        raw_grpc_port = options.get("grpc_port")
        if raw_grpc_port is not None and int(raw_grpc_port) > 0:
            grpc_port = int(raw_grpc_port)
        else:
            range_start = options.get("grpc_port_range_start")
            range_end = options.get("grpc_port_range_end")
            port_range = PortRange(
                start=int(range_start) if range_start is not None else int(cfg.grpc_port_range_start),
                end=int(range_end) if range_end is not None else int(cfg.grpc_port_range_end),
            )
            try:
                grpc_port = find_available_tcp_port(host=grpc_host, port_range=port_range)
            except Exception as e:
                raise CommandError(
                    f"No available gRPC port in range {port_range.start}-{port_range.end}. ({type(e).__name__}: {e})"
                )

        worker_id = options.get("worker_id") or ""
        if not worker_id:
            # For local development convenience, pick a short numeric id if possible.
            # Avoid collisions with currently active Redis workers.
            existing = {w.worker_id for w in list_workers(cfg.redis_url)}
            chosen = ""
            for _ in range(200):
                cand = f"{random.randint(0, 999):03d}"
                if cand not in existing:
                    chosen = cand
                    break
            worker_id = chosen or uuid.uuid4().hex

        settings = CoordinationSettings(
            heartbeat_ttl_seconds=int(options["heartbeat_ttl_seconds"]),
            leader_lock_ttl_seconds=int(options["leader_lock_ttl_seconds"]),
            subleader_lock_ttl_seconds=int(options["leader_lock_ttl_seconds"]),
        )

        stop_requested = False

        def _request_stop(*_args):
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)

        interval_seconds = float(options["interval_seconds"])
        run_seconds = int(options["run_seconds"])
        deadline = time.time() + run_seconds if run_seconds > 0 else None

        coordinator = RedisCoordinator(
            redis_url=cfg.redis_url,
            worker_id=worker_id,
            node_id=cfg.node_id,
            grpc_host=grpc_host,
            grpc_port=grpc_port,
            settings=settings,
        )

        self.stdout.write("scheduler worker (M1)")
        self.stdout.write(f"worker_id={worker_id}")
        self.stdout.write(f"node_id={cfg.node_id}")
        self.stdout.write(f"redis_url={cfg.redis_url}")
        self.stdout.write(f"grpc={grpc_host}:{grpc_port}")

        # gRPC server (M2)
        runtime_lock = threading.Lock()
        runtime_state = WorkerRuntimeState(worker_id=worker_id, node_id=cfg.node_id)
        try:
            grpc_server = start_worker_grpc_server(
                host=grpc_host,
                port=grpc_port,
                state=runtime_state,
                lock=runtime_lock,
                tls_cert_file=cfg.tls_cert_file,
                tls_key_file=cfg.tls_key_file,
            )
        except RuntimeError as e:
            raise CommandError(
                f"Failed to start gRPC server on {grpc_host}:{grpc_port}. "
                f"The port may already be in use. Try a different --grpc-port. ({e})"
            )
        self.stdout.write("grpc_server=started")
        last_leader_tick_at = 0.0
        last_leader_ping_at = 0.0
        last_leader_ping_summary_at = 0.0
        last_leader_dispatch_at = 0.0
        last_leader_reconcile_at = 0.0
        last_leader_reload_at = 0.0

        # Pipeline knobs (keep the main loop responsive).
        leader_ping_batch_size = 2
        leader_dispatch_rpc_budget = 5
        leader_dispatch_time_budget_seconds = 0.3
        leader_reconcile_worker_batch_size = 2
        leader_reconcile_jobrun_batch_size = 50

        ping_cursor = 0
        reconcile_cursor = 0

        # Coordination tick must remain cheap and frequent (docs/architecture.md).
        # Leader work (DB / RPC) can be heavy; run tick in a dedicated thread so
        # heartbeat / lock TTLs don't expire and cause role flapping.
        status_lock = threading.Lock()
        latest_status: TickStatus | None = None

        coordination_interval_seconds = min(1.0, max(0.1, float(interval_seconds)))

        def _coordination_loop():
            nonlocal latest_status
            local_last_role = None
            local_last_epoch = None
            local_last_cluster_epoch = None

            last_tick_error_log_at = 0.0

            while True:
                if stop_requested:
                    break
                if deadline is not None and time.time() >= deadline:
                    break

                try:
                    status = coordinator.tick(now=time.time())
                except Exception as e:
                    now_ts = time.time()
                    # Avoid spamming logs if Redis is temporarily unavailable.
                    if (now_ts - last_tick_error_log_at) >= 5.0:
                        self.stdout.write(f"coordination_tick error={type(e).__name__}")
                        last_tick_error_log_at = now_ts
                    time.sleep(min(0.5, coordination_interval_seconds))
                    continue
                with status_lock:
                    latest_status = status

                if status.is_leader:
                    role = "LEADER"
                elif status.is_subleader:
                    role = "SUBLEADER"
                else:
                    role = "WORKER"

                with runtime_lock:
                    if status.is_leader:
                        runtime_state.role = "leader"
                    elif status.is_subleader:
                        runtime_state.role = "subleader"
                    else:
                        runtime_state.role = "worker"
                    runtime_state.cluster_epoch = int(status.cluster_epoch)
                    runtime_state.leader_epoch = int(status.leader_epoch or 0)
                    runtime_state.leader_worker_id = status.leader_worker_id or ""
                    runtime_state.last_heartbeat_unix_ms = int(time.time() * 1000)

                if (
                    role != local_last_role
                    or status.leader_epoch != local_last_epoch
                    or status.cluster_epoch != local_last_cluster_epoch
                ):
                    self.stdout.write(
                        " ".join(
                            [
                                f"role={role}",
                                f"cluster_epoch={status.cluster_epoch}",
                                f"leader_epoch={status.leader_epoch}",
                                f"leader_worker_id={status.leader_worker_id}",
                                f"subleader_worker_id={status.subleader_worker_id}",
                            ]
                        )
                    )
                    local_last_role = role
                    local_last_epoch = status.leader_epoch
                    local_last_cluster_epoch = status.cluster_epoch

                time.sleep(coordination_interval_seconds)

        coordination_thread = threading.Thread(target=_coordination_loop, name="coordination", daemon=True)
        coordination_thread.start()

        def _mark_skipped_if_assigned(*, job_run_id: int, reason: str) -> bool:
            now_dt = timezone.now()
            with transaction.atomic():
                try:
                    jr = JobRun.objects.select_for_update().get(id=job_run_id)
                except JobRun.DoesNotExist:
                    return False

                if jr.state != JobRun.State.ASSIGNED:
                    return False
                if jr.started_at is not None:
                    return False

                jr.state = JobRun.State.SKIPPED
                jr.finished_at = now_dt
                jr.error_summary = (jr.error_summary + "\n" if jr.error_summary else "") + f"skipped: {reason}"
                jr.version = int(jr.version) + 1
                jr.save(update_fields=["state", "finished_at", "error_summary", "version", "updated_at"])
                return True

        def _mark_confirming_if_running(*, job_run_id: int, reason: str, confirm_seconds: int) -> bool:
            now_dt = timezone.now()
            confirm_seconds = max(1, int(confirm_seconds or 0))
            with transaction.atomic():
                try:
                    jr = JobRun.objects.select_for_update().get(id=job_run_id)
                except JobRun.DoesNotExist:
                    return False

                if jr.state != JobRun.State.RUNNING:
                    return False
                if jr.continuation_state == JobRun.ContinuationState.CONFIRMING:
                    return False

                jr.continuation_state = JobRun.ContinuationState.CONFIRMING
                jr.continuation_check_started_at = now_dt
                jr.continuation_check_deadline_at = now_dt + timedelta(seconds=confirm_seconds)
                jr.error_summary = (jr.error_summary + "\n" if jr.error_summary else "") + f"confirming: {reason}"
                jr.version = int(jr.version) + 1
                jr.save(
                    update_fields=[
                        "continuation_state",
                        "continuation_check_started_at",
                        "continuation_check_deadline_at",
                        "error_summary",
                        "version",
                        "updated_at",
                    ]
                )
                return True

        def _orphan_if_confirming_deadline_exceeded(*, job_run_id: int, reason: str) -> bool:
            now_dt = timezone.now()
            with transaction.atomic():
                try:
                    jr = JobRun.objects.select_for_update().get(id=job_run_id)
                except JobRun.DoesNotExist:
                    return False

                if jr.state != JobRun.State.RUNNING:
                    return False
                if jr.continuation_state != JobRun.ContinuationState.CONFIRMING:
                    return False
                if not jr.continuation_check_deadline_at or jr.continuation_check_deadline_at > now_dt:
                    return False

                jr.state = JobRun.State.ORPHANED
                jr.error_summary = (jr.error_summary + "\n" if jr.error_summary else "") + f"orphaned: {reason}"
                jr.assigned_worker_id = ""
                jr.assigned_at = None
                jr.started_at = None
                jr.finished_at = None
                jr.exit_code = None
                jr.continuation_state = JobRun.ContinuationState.NONE
                jr.continuation_check_started_at = None
                jr.continuation_check_deadline_at = None
                jr.version = int(jr.version) + 1
                jr.attempt = int(jr.attempt) + 1
                jr.save(
                    update_fields=[
                        "state",
                        "error_summary",
                        "assigned_worker_id",
                        "assigned_at",
                        "started_at",
                        "finished_at",
                        "exit_code",
                        "continuation_state",
                        "continuation_check_started_at",
                        "continuation_check_deadline_at",
                        "version",
                        "attempt",
                        "updated_at",
                    ]
                )
                return True

        try:
            while True:
                if stop_requested:
                    break
                if deadline is not None and time.time() >= deadline:
                    break

                # Refresh cached config view (DB overrides can change).
                # Note: some settings still require process restart to take full effect.
                cfg = get_scheduler_config()

                # Keep pipeline knobs in sync (safe to update at runtime).
                try:
                    leader_ping_batch_size = int(getattr(cfg, "leader_ping_batch_size", 2) or 2)
                except Exception:
                    leader_ping_batch_size = 2
                leader_ping_batch_size = max(1, min(50, int(leader_ping_batch_size)))

                with status_lock:
                    status = latest_status

                if status is None:
                    time.sleep(min(0.05, interval_seconds))
                    continue

                if status.is_leader and (time.time() - last_leader_tick_at) >= interval_seconds:
                    effective_epoch = int(status.leader_epoch or status.cluster_epoch or 0)
                    snapshot = run_leader_tick_snapshot(
                        redis_url=cfg.redis_url,
                        leader_epoch=effective_epoch,
                        assign_ahead_seconds=int(cfg.assign_ahead_seconds),
                        skip_late_runs_after_seconds=int(cfg.skip_late_runs_after_seconds),
                        reassign_assigned_after_seconds=int(cfg.reassign_assigned_after_seconds),
                        continuation_confirm_seconds=int(cfg.continuation_confirm_seconds),
                        assign_weight_leader=int(cfg.assign_weight_leader),
                        assign_weight_subleader=int(cfg.assign_weight_subleader),
                        assign_weight_worker=int(cfg.assign_weight_worker),
                        assign_running_load_weight=int(cfg.assign_running_load_weight),
                        rebalance_assigned_enabled=bool(cfg.rebalance_assigned_enabled),
                        rebalance_assigned_min_future_seconds=int(cfg.rebalance_assigned_min_future_seconds),
                        rebalance_assigned_max_per_tick=int(cfg.rebalance_assigned_max_per_tick),
                        rebalance_assigned_cooldown_seconds=int(cfg.rebalance_assigned_cooldown_seconds),
                    )
                    self.stdout.write(
                        "leader_tick "
                        + " ".join(
                            [
                                f"enabled_job_definitions={snapshot.enabled_job_definitions}",
                                f"pending_job_runs={snapshot.pending_job_runs}",
                                f"created_job_runs={snapshot.created_job_runs}",
                                f"assigned_job_runs={snapshot.assigned_job_runs}",
                                f"orphaned_job_runs={snapshot.orphaned_job_runs}",
                                f"confirming_job_runs={snapshot.confirming_job_runs}",
                                f"reassigned_job_runs={snapshot.reassigned_job_runs}",
                                f"rebalanced_job_runs={snapshot.rebalanced_job_runs}",
                            ]
                        )
                    )
                    last_leader_tick_at = time.time()

                # Phase F MVP: leader dispatches assigned runs via StartJob
                if status.is_leader and (time.time() - last_leader_dispatch_at) >= max(1.0, interval_seconds):
                    effective_epoch = int(status.leader_epoch or status.cluster_epoch or 0)
                    workers = list_workers(cfg.redis_url)
                    targets = {
                        w.worker_id: f"{w.grpc_host}:{w.grpc_port}"
                        for w in workers
                        if w.grpc_host and w.grpc_port and w.heartbeat_ttl_seconds > 0
                    }

                    running_counts = {
                        row["assigned_worker_id"]: int(row["c"])
                        for row in JobRun.objects.filter(state=JobRun.State.RUNNING)
                        .exclude(assigned_worker_id="")
                        .values("assigned_worker_id")
                        .annotate(c=Count("id"))
                    }

                    # Grab a small batch to keep the loop cheap.
                    assigned = (
                        JobRun.objects.select_related("job_definition")
                        .filter(
                            state=JobRun.State.ASSIGNED,
                            assigned_worker_id__isnull=False,
                        )
                        .exclude(assigned_worker_id="")
                        .order_by("scheduled_for", "id")[:20]
                    )

                    dispatch_deadline = time.time() + float(leader_dispatch_time_budget_seconds)
                    rpc_calls = 0
                    for jr in assigned:
                        if rpc_calls >= int(leader_dispatch_rpc_budget):
                            break
                        if time.time() >= dispatch_deadline:
                            break

                        target = targets.get(jr.assigned_worker_id)
                        if not target:
                            continue

                        # Do not start a second job on the same worker_id if DB already indicates RUNNING.
                        if int(running_counts.get(jr.assigned_worker_id, 0)) > 0:
                            continue

                        # If already started, skip.
                        if jr.started_at is not None:
                            continue

                        # Skip overly late runs (avoid executing backlog after downtime).
                        if (
                            int(getattr(cfg, "skip_late_runs_after_seconds", 0) or 0) > 0
                            and jr.scheduled_for is not None
                        ):
                            now_dt = timezone.now()
                            cutoff = now_dt - timedelta(seconds=int(cfg.skip_late_runs_after_seconds))
                            if jr.scheduled_for < cutoff:
                                ok = _mark_skipped_if_assigned(
                                    job_run_id=int(jr.id),
                                    reason=f"scheduled_for too old (scheduled_for={jr.scheduled_for.isoformat()} cutoff={cutoff.isoformat()})",
                                )
                                if ok:
                                    self.stdout.write(
                                        f"skip_job late job_run_id={jr.id} scheduled_for={jr.scheduled_for.isoformat()}"
                                    )
                                continue

                        jd = jr.job_definition
                        try:
                            rpc_calls += 1
                            resp = start_job_on_worker(
                                target=target,
                                leader_epoch=effective_epoch,
                                job_run_id=str(jr.id),
                                command_name=str(jd.command_name),
                                args_json=json.dumps(jd.default_args_json or {}, ensure_ascii=False),
                                timeout_seconds=int(jd.timeout_seconds or 0),
                                attempt=int(jr.attempt or 0),
                                tls_cert_file=cfg.tls_cert_file,
                                tls_key_file=cfg.tls_key_file,
                                timeout_rpc_seconds=1.0,
                            )
                            if resp.result == worker_pb2.StartJobResponse.ACCEPTED:
                                self.stdout.write(
                                    f"start_job accepted target={target} job_run_id={jr.id} command={jd.command_name}"
                                )
                        except Exception as e:
                            self.stdout.write(
                                f"start_job target={target} job_run_id={jr.id} error={type(e).__name__}"
                            )

                    last_leader_dispatch_at = time.time()

                # Config reload (M6'): UI creates ConfigReloadRequest; leader applies it to all active workers.
                if status.is_leader and (time.time() - last_leader_reload_at) >= max(1.0, interval_seconds):
                    req = (
                        ConfigReloadRequest.objects.filter(status=ConfigReloadRequest.Status.PENDING)
                        .order_by("requested_at", "id")
                        .first()
                    )
                    if req is not None:
                        effective_epoch = int(status.leader_epoch or status.cluster_epoch or 0)

                        leader_gen = reload_scheduler_settings_cache()
                        workers = list_workers(cfg.redis_url)
                        targets = {
                            w.worker_id: f"{w.grpc_host}:{w.grpc_port}"
                            for w in workers
                            if w.grpc_host and w.grpc_port and w.heartbeat_ttl_seconds > 0
                        }

                        results: dict[str, dict] = {}
                        ok_count = 0
                        for wid, target in targets.items():
                            try:
                                resp = reload_config_on_worker(
                                    target=target,
                                    leader_epoch=effective_epoch,
                                    requested_by=str(req.requested_by or ""),
                                    tls_cert_file=cfg.tls_cert_file,
                                    tls_key_file=cfg.tls_key_file,
                                    timeout_rpc_seconds=1.0,
                                )
                                results[wid] = {
                                    "target": target,
                                    "ok": bool(resp.ok),
                                    "message": str(resp.message or ""),
                                    "cache_generation": int(resp.cache_generation or 0),
                                }
                                if resp.ok:
                                    ok_count += 1
                            except Exception as e:
                                results[wid] = {
                                    "target": target,
                                    "ok": False,
                                    "message": f"{type(e).__name__}",
                                    "cache_generation": 0,
                                }

                        total = len(targets)
                        all_ok = (total == 0) or (ok_count == total)
                        req.status = ConfigReloadRequest.Status.APPLIED if all_ok else ConfigReloadRequest.Status.FAILED
                        req.applied_at = timezone.now()
                        req.leader_worker_id = str(worker_id)
                        req.leader_epoch = int(effective_epoch)
                        req.result_json = {
                            "leader_cache_generation": int(leader_gen),
                            "targets": results,
                            "ok_count": int(ok_count),
                            "total": int(total),
                        }
                        req.save(
                            update_fields=[
                                "status",
                                "applied_at",
                                "leader_worker_id",
                                "leader_epoch",
                                "result_json",
                            ]
                        )

                        self.stdout.write(
                            f"config_reload request_id={req.id} status={req.status} ok={ok_count}/{total}"
                        )

                    last_leader_reload_at = time.time()

                # M2: leader pings all known workers
                if status.is_leader and (time.time() - last_leader_ping_at) >= max(1.0, interval_seconds):
                    workers = [
                        w
                        for w in list_workers(cfg.redis_url)
                        if w.grpc_host and w.grpc_port and w.heartbeat_ttl_seconds > 0
                    ]
                    # Ensure stable ordering so round-robin is fair across ticks.
                    workers.sort(key=lambda w: str(w.worker_id or ""))
                    ok_count = 0
                    err_count = 0
                    ok_worker_ids: list[str] = []
                    err_worker_ids: list[str] = []
                    checked_worker_ids: list[str] = []

                    total = len(workers)
                    if total > 0:
                        bs = max(1, min(50, int(leader_ping_batch_size)))
                        start = int(ping_cursor) % total
                        batch = workers[start : start + bs]
                        if len(batch) < bs:
                            batch = batch + workers[0 : (bs - len(batch))]
                        ping_cursor = (start + bs) % total
                    else:
                        batch = []

                    for w in batch:
                        wid = str(w.worker_id or "")
                        if wid:
                            checked_worker_ids.append(wid)
                        target = f"{w.grpc_host}:{w.grpc_port}"
                        try:
                            resp = ping_worker(
                                target=target,
                                caller_role="leader",
                                leader_epoch=int(status.leader_epoch or status.cluster_epoch or 0),
                                tls_cert_file=cfg.tls_cert_file,
                                tls_key_file=cfg.tls_key_file,
                                timeout_seconds=0.5,
                            )
                            ok_count += 1
                            if wid:
                                ok_worker_ids.append(wid)
                        except Exception as e:
                            err_count += 1
                            if wid:
                                err_worker_ids.append(wid)
                            self.stdout.write(
                                f"leader_ping worker_id={wid or '-'} target={target} error={type(e).__name__}"
                            )

                    # Success logs are intentionally throttled to avoid blocking stdout,
                    # which can starve coordination tick and cause TTL expiration.
                    now_ts = time.time()
                    if err_count > 0 or (now_ts - last_leader_ping_summary_at) >= 15.0:
                        checked_s = ",".join(checked_worker_ids) if checked_worker_ids else ""
                        ok_s = ",".join(ok_worker_ids) if ok_worker_ids else ""
                        err_s = ",".join(err_worker_ids) if err_worker_ids else ""
                        self.stdout.write(
                            "leader_ping summary "
                            + " ".join(
                                [
                                    f"ok={ok_count}",
                                    f"error={err_count}",
                                    f"batch={len(batch)}/{total}",
                                    f"checked_worker_ids={checked_s}",
                                    f"ok_worker_ids={ok_s}",
                                    f"error_worker_ids={err_s}",
                                ]
                            )
                        )
                        last_leader_ping_summary_at = now_ts
                    last_leader_ping_at = time.time()

                # Reconcile: DB says RUNNING, but worker reports no current job (common after worker restart).
                if status.is_leader and (time.time() - last_leader_reconcile_at) >= max(1.0, interval_seconds):
                    effective_epoch = int(status.leader_epoch or status.cluster_epoch or 0)
                    workers = [
                        w
                        for w in list_workers(cfg.redis_url)
                        if w.grpc_host and w.grpc_port and w.heartbeat_ttl_seconds > 0
                    ]
                    total_workers = len(workers)
                    if total_workers > 0:
                        bs = max(1, int(leader_reconcile_worker_batch_size))
                        start = int(reconcile_cursor) % total_workers
                        batch = workers[start : start + bs]
                        if len(batch) < bs:
                            batch = batch + workers[0 : (bs - len(batch))]
                        reconcile_cursor = (start + bs) % total_workers
                    else:
                        batch = []

                    targets = {
                        w.worker_id: f"{w.grpc_host}:{w.grpc_port}"
                        for w in batch
                        if w.worker_id
                    }

                    status_by_worker: dict[str, str] = {}
                    for wid, target in targets.items():
                        try:
                            resp = get_status_worker(
                                target=target,
                                leader_epoch=effective_epoch,
                                tls_cert_file=cfg.tls_cert_file,
                                tls_key_file=cfg.tls_key_file,
                                timeout_seconds=0.5,
                            )
                            status_by_worker[wid] = (resp.current_job_run_id or "").strip()
                        except Exception:
                            # If we can't query status, don't make a strong decision here.
                            continue

                    # Check a small batch of RUNNING jobs for the reconciled workers only.
                    running = (
                        JobRun.objects.filter(state=JobRun.State.RUNNING, assigned_worker_id__in=list(targets.keys()))
                        .exclude(assigned_worker_id="")
                        .order_by("started_at", "id")[: int(leader_reconcile_jobrun_batch_size)]
                    )

                    for jr in running:
                        wid = jr.assigned_worker_id
                        if wid not in status_by_worker:
                            continue

                        cur = status_by_worker.get(wid) or ""
                        if jr.continuation_state == JobRun.ContinuationState.CONFIRMING:
                            # If deadline exceeded, orphan regardless of worker active.
                            _orphan_if_confirming_deadline_exceeded(
                                job_run_id=int(jr.id),
                                reason="confirming deadline exceeded (worker status mismatch)",
                            )
                            continue

                        if not cur:
                            _mark_confirming_if_running(
                                job_run_id=int(jr.id),
                                reason="worker reports no current job",
                                confirm_seconds=int(cfg.continuation_confirm_seconds),
                            )
                            continue

                        if cur != str(jr.id):
                            _mark_confirming_if_running(
                                job_run_id=int(jr.id),
                                reason=f"worker reports different job_run_id={cur}",
                                confirm_seconds=int(cfg.continuation_confirm_seconds),
                            )

                    last_leader_reconcile_at = time.time()

                time.sleep(interval_seconds)
        finally:
            stop_requested = True
            try:
                coordination_thread.join(timeout=2.0)
            except Exception:
                pass
            try:
                grpc_server.stop(grace=None)
            except Exception:
                pass
            coordinator.shutdown()
