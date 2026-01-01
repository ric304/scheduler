from __future__ import annotations

from dataclasses import dataclass
import time

from django.http import HttpResponse

from scheduler.conf import get_str

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
except Exception:  # pragma: no cover
    CONTENT_TYPE_LATEST = "text/plain"
    Counter = None  # type: ignore
    Gauge = None  # type: ignore
    Histogram = None  # type: ignore
    generate_latest = None  # type: ignore


@dataclass(frozen=True)
class _Metrics:
    job_runs_started_total: object | None
    job_runs_finished_total: object | None
    job_run_duration_seconds: object | None
    worker_current_job: object | None
    job_run_cpu_seconds_total: object | None
    job_run_io_read_bytes_total: object | None
    job_run_io_write_bytes_total: object | None
    job_run_peak_rss_bytes: object | None
    workers_online: object | None
    workers_min_online: object | None
    leader_present: object | None
    worker_running_jobs: object | None
    worker_assigned_jobs: object | None
    worker_high_load_threshold: object | None
    alert_high_load_enabled: object | None
    worker_role_change_total: object | None
    alert_role_change_enabled: object | None


def _build_metrics() -> _Metrics:
    if Counter is None or Histogram is None or Gauge is None:
        return _Metrics(None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None)

    job_runs_started_total = Counter(
        "scheduler_job_runs_started_total",
        "Number of job runs started on workers",
        labelnames=["command_name"],
    )
    job_runs_finished_total = Counter(
        "scheduler_job_runs_finished_total",
        "Number of job runs finished on workers",
        labelnames=["command_name", "result"],
    )
    job_run_duration_seconds = Histogram(
        "scheduler_job_run_duration_seconds",
        "Job run duration in seconds",
        labelnames=["command_name", "result"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
    )
    worker_current_job = Gauge(
        "scheduler_worker_current_job_run",
        "Current job run executing on a worker (1=running)",
        labelnames=["worker_id", "job_run_id"],
    )

    job_run_cpu_seconds_total = Counter(
        "scheduler_job_run_cpu_seconds_total",
        "Total CPU seconds consumed by job runs (process tree)",
        labelnames=["command_name", "result"],
    )
    job_run_io_read_bytes_total = Counter(
        "scheduler_job_run_io_read_bytes_total",
        "Total IO read bytes consumed by job runs (process tree)",
        labelnames=["command_name", "result"],
    )
    job_run_io_write_bytes_total = Counter(
        "scheduler_job_run_io_write_bytes_total",
        "Total IO write bytes consumed by job runs (process tree)",
        labelnames=["command_name", "result"],
    )
    job_run_peak_rss_bytes = Histogram(
        "scheduler_job_run_peak_rss_bytes",
        "Peak RSS (bytes) observed during a job run (process tree)",
        labelnames=["command_name", "result"],
        buckets=(
            8 * 1024 * 1024,
            16 * 1024 * 1024,
            32 * 1024 * 1024,
            64 * 1024 * 1024,
            128 * 1024 * 1024,
            256 * 1024 * 1024,
            512 * 1024 * 1024,
            1024 * 1024 * 1024,
            2 * 1024 * 1024 * 1024,
        ),
    )

    workers_online = Gauge(
        "scheduler_workers_online",
        "Number of workers currently online (Redis heartbeat TTL > 0)",
    )
    workers_min_online = Gauge(
        "scheduler_workers_min_online",
        "Configured minimum number of online workers (for alerting)",
    )
    leader_present = Gauge(
        "scheduler_leader_present",
        "Whether an active leader heartbeat is present (1=yes, 0=no)",
    )

    worker_running_jobs = Gauge(
        "scheduler_worker_running_jobs",
        "Number of RUNNING job runs per worker (from DB)",
        labelnames=["worker_id"],
    )
    worker_assigned_jobs = Gauge(
        "scheduler_worker_assigned_jobs",
        "Number of ASSIGNED job runs per worker (from DB)",
        labelnames=["worker_id"],
    )
    worker_high_load_threshold = Gauge(
        "scheduler_worker_high_load_threshold",
        "High-load threshold for worker job count alerting",
    )
    alert_high_load_enabled = Gauge(
        "scheduler_alert_high_load_enabled",
        "Whether SchedulerWorkerHighLoad alerting is enabled (1=yes, 0=no)",
    )

    worker_role_change_total = Counter(
        "scheduler_worker_role_change_total",
        "Number of observed worker role changes (leader/subleader) based on Redis locks",
        labelnames=["role"],
    )
    alert_role_change_enabled = Gauge(
        "scheduler_alert_role_change_enabled",
        "Whether SchedulerWorkerRoleChanged alerting is enabled (1=yes, 0=no)",
    )

    return _Metrics(
        job_runs_started_total,
        job_runs_finished_total,
        job_run_duration_seconds,
        worker_current_job,
        job_run_cpu_seconds_total,
        job_run_io_read_bytes_total,
        job_run_io_write_bytes_total,
        job_run_peak_rss_bytes,
        workers_online,
        workers_min_online,
        leader_present,
        worker_running_jobs,
        worker_assigned_jobs,
        worker_high_load_threshold,
        alert_high_load_enabled,
        worker_role_change_total,
        alert_role_change_enabled,
    )


METRICS = _build_metrics()


_DB_SYNC_CACHE: dict[str, object] = {
    "ts": 0.0,
    "started_last_id": 0,
    "finished_last_id": 0,
    # job_run_id(int) -> worker_id(str)
    "running": {},
}


_REDIS_SYNC_CACHE: dict[str, object] = {
    "ts": 0.0,
    "leader_worker_id": None,
    "subleader_worker_id": None,
}


def _sync_metrics_from_redis() -> None:
    if (
        METRICS.workers_online is None
        or METRICS.workers_min_online is None
        or METRICS.leader_present is None
        or METRICS.alert_role_change_enabled is None
    ):
        return

    now = time.time()
    ts_val = _REDIS_SYNC_CACHE.get("ts")
    last_ts = float(ts_val) if isinstance(ts_val, (int, float)) else 0.0
    if (now - last_ts) < 5.0:
        return
    _REDIS_SYNC_CACHE["ts"] = now

    try:
        from scheduler.redis_coordination import list_workers
    except Exception:
        return

    redis_url = get_str(key="SCHEDULER_REDIS_URL", default="", fresh=True).strip()
    if not redis_url:
        return

    try:
        workers = list_workers(redis_url)
        online = sum(1 for w in workers if int(w.heartbeat_ttl_seconds) > 0)
        leader_ok = any((w.is_leader and int(w.heartbeat_ttl_seconds) > 0) for w in workers)

        # Role-change detection (best-effort)
        leader_worker_id = None
        subleader_worker_id = None
        for w in workers:
            if w.is_leader:
                leader_worker_id = w.worker_id
            if w.is_subleader:
                subleader_worker_id = w.worker_id

        prev_leader = _REDIS_SYNC_CACHE.get("leader_worker_id")
        prev_subleader = _REDIS_SYNC_CACHE.get("subleader_worker_id")
        if METRICS.worker_role_change_total is not None:
            try:
                if prev_leader is not None and leader_worker_id != prev_leader:
                    METRICS.worker_role_change_total.labels(role="leader").inc()  # type: ignore[attr-defined]
                if prev_subleader is not None and subleader_worker_id != prev_subleader:
                    METRICS.worker_role_change_total.labels(role="subleader").inc()  # type: ignore[attr-defined]
            except Exception:
                pass
        _REDIS_SYNC_CACHE["leader_worker_id"] = leader_worker_id
        _REDIS_SYNC_CACHE["subleader_worker_id"] = subleader_worker_id

        # Alert enable flags (from Settings)
        role_change_enabled_raw = get_str(key="SCHEDULER_ALERT_ROLE_CHANGE_ENABLED", default="1", fresh=True).strip()
        role_change_enabled = 0.0 if role_change_enabled_raw in {"", "0", "false", "False", "no", "No"} else 1.0
        METRICS.alert_role_change_enabled.set(role_change_enabled)  # type: ignore[attr-defined]

        try:
            min_online_raw = get_str(key="SCHEDULER_MIN_ONLINE_WORKERS", default="1", fresh=True).strip()
            min_online = int(min_online_raw) if min_online_raw else 1
        except Exception:
            min_online = 1

        METRICS.workers_online.set(float(online))  # type: ignore[attr-defined]
        METRICS.workers_min_online.set(float(max(0, min_online)))  # type: ignore[attr-defined]
        METRICS.leader_present.set(1.0 if leader_ok else 0.0)  # type: ignore[attr-defined]
    except Exception:
        return


def _sync_metrics_from_db() -> None:
    """Populate/advance metrics from DB state.

    Note: Prometheus client metrics are process-local. In this project, job execution
    happens in worker processes, but /metrics is served by the Django web process.
    To make scheduler_* visible from the scraped Django endpoint, we sync from DB.

    This is throttled and designed for dev/ops visibility.
    """

    if METRICS.job_runs_started_total is None:
        return

    now = time.time()
    ts_val = _DB_SYNC_CACHE.get("ts")
    last_ts = float(ts_val) if isinstance(ts_val, (int, float)) else 0.0
    if (now - last_ts) < 5.0:
        return

    _DB_SYNC_CACHE["ts"] = now

    try:
        from scheduler.models import JobRun
    except Exception:
        return

    # --- Started counter (incremental by JobRun.id) ---
    started_last = _DB_SYNC_CACHE.get("started_last_id")
    started_last_id = int(started_last) if isinstance(started_last, (int, float)) else 0
    try:
        qs = (
            JobRun.objects.select_related("job_definition")
            .filter(id__gt=started_last_id)
            .exclude(started_at=None)
            .only("id", "started_at", "job_definition__command_name")
            .order_by("id")
        )
        max_id = started_last_id
        for r in qs[:2000]:
            max_id = max(max_id, int(r.id))
            cn = ""
            try:
                cn = str(r.job_definition.command_name if r.job_definition_id else "")
            except Exception:
                cn = ""
            observe_job_started(command_name=cn)
        _DB_SYNC_CACHE["started_last_id"] = max_id
    except Exception:
        pass

    # --- Finished counters + duration histogram (incremental by JobRun.id) ---
    finished_last = _DB_SYNC_CACHE.get("finished_last_id")
    finished_last_id = int(finished_last) if isinstance(finished_last, (int, float)) else 0
    try:
        qs = (
            JobRun.objects.select_related("job_definition")
            .filter(id__gt=finished_last_id)
            .exclude(finished_at=None)
            .only(
                "id",
                "state",
                "started_at",
                "finished_at",
                "resource_cpu_seconds_total",
                "resource_peak_rss_bytes",
                "resource_io_read_bytes",
                "resource_io_write_bytes",
                "job_definition__command_name",
            )
            .order_by("id")
        )
        max_id = finished_last_id
        for r in qs[:2000]:
            max_id = max(max_id, int(r.id))
            cn = ""
            try:
                cn = str(r.job_definition.command_name if r.job_definition_id else "")
            except Exception:
                cn = ""
            try:
                dur = 0.0
                if r.started_at and r.finished_at:
                    dur = max(0.0, float((r.finished_at - r.started_at).total_seconds()))
                observe_job_finished(command_name=cn, result=str(r.state), duration_seconds=dur)

                # Resource usage (best-effort; may be null)
                observe_job_resources(
                    command_name=cn,
                    result=str(r.state),
                    cpu_seconds_total=float(r.resource_cpu_seconds_total) if r.resource_cpu_seconds_total is not None else None,
                    peak_rss_bytes=int(r.resource_peak_rss_bytes) if r.resource_peak_rss_bytes is not None else None,
                    io_read_bytes=int(r.resource_io_read_bytes) if r.resource_io_read_bytes is not None else None,
                    io_write_bytes=int(r.resource_io_write_bytes) if r.resource_io_write_bytes is not None else None,
                )
            except Exception:
                continue
        _DB_SYNC_CACHE["finished_last_id"] = max_id
    except Exception:
        pass

    # --- Current running gauge (reflect DB state) ---
    if METRICS.worker_current_job is None:
        return
    try:
        running_rows = (
            JobRun.objects.select_related("job_definition")
            .filter(state=JobRun.State.RUNNING)
            .only("id", "assigned_worker_id")
        )
        current: dict[int, str] = {}
        for r in running_rows[:5000]:
            try:
                current[int(r.id)] = str(r.assigned_worker_id or "")
            except Exception:
                continue

        prev = _DB_SYNC_CACHE.get("running")
        prev_map: dict[int, str] = prev if isinstance(prev, dict) else {}

        # Turn off gauges for no-longer-running jobs
        for job_run_id, worker_id in list(prev_map.items()):
            if job_run_id not in current:
                set_worker_current_job(worker_id=str(worker_id or ""), job_run_id=str(job_run_id), running=False)
                prev_map.pop(job_run_id, None)

        # Turn on gauges for currently running jobs
        for job_run_id, worker_id in current.items():
            set_worker_current_job(worker_id=str(worker_id or ""), job_run_id=str(job_run_id), running=True)
            prev_map[job_run_id] = worker_id

        _DB_SYNC_CACHE["running"] = prev_map
    except Exception:
        return

    # --- Per-worker job counts (ASSIGNED/RUNNING) + alert thresholds ---
    if (
        METRICS.worker_running_jobs is None
        or METRICS.worker_assigned_jobs is None
        or METRICS.worker_high_load_threshold is None
        or METRICS.alert_high_load_enabled is None
    ):
        return

    try:
        from django.db.models import Count

        # Threshold/enable from Settings
        try:
            th_raw = get_str(key="SCHEDULER_WORKER_HIGH_LOAD_THRESHOLD", default="10", fresh=True).strip() or "10"
            th = max(0, int(th_raw))
        except Exception:
            th = 10
        en_raw = get_str(key="SCHEDULER_ALERT_HIGH_LOAD_ENABLED", default="1", fresh=True).strip()
        en = 0.0 if en_raw in {"", "0", "false", "False", "no", "No"} else 1.0
        METRICS.worker_high_load_threshold.set(float(th))  # type: ignore[attr-defined]
        METRICS.alert_high_load_enabled.set(float(en))  # type: ignore[attr-defined]

        running_counts: dict[str, int] = {}
        assigned_counts: dict[str, int] = {}

        for row in (
            JobRun.objects.filter(state=JobRun.State.RUNNING)
            .exclude(assigned_worker_id=None)
            .values("assigned_worker_id")
            .annotate(c=Count("id"))
        ):
            wid = str(row.get("assigned_worker_id") or "").strip()
            if wid:
                running_counts[wid] = int(row.get("c") or 0)

        for row in (
            JobRun.objects.filter(state=JobRun.State.ASSIGNED)
            .exclude(assigned_worker_id=None)
            .values("assigned_worker_id")
            .annotate(c=Count("id"))
        ):
            wid = str(row.get("assigned_worker_id") or "").strip()
            if wid:
                assigned_counts[wid] = int(row.get("c") or 0)

        prev_val = _DB_SYNC_CACHE.get("worker_ids")
        prev_worker_ids: set[str] = set(prev_val) if isinstance(prev_val, (list, set, tuple)) else set()
        cur_worker_ids = set(running_counts.keys()) | set(assigned_counts.keys())

        # Remove stale label series
        for wid in prev_worker_ids - cur_worker_ids:
            try:
                METRICS.worker_running_jobs.remove(worker_id=str(wid))  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                METRICS.worker_assigned_jobs.remove(worker_id=str(wid))  # type: ignore[attr-defined]
            except Exception:
                pass

        for wid in cur_worker_ids:
            METRICS.worker_running_jobs.labels(worker_id=str(wid)).set(float(running_counts.get(wid, 0)))  # type: ignore[attr-defined]
            METRICS.worker_assigned_jobs.labels(worker_id=str(wid)).set(float(assigned_counts.get(wid, 0)))  # type: ignore[attr-defined]

        _DB_SYNC_CACHE["worker_ids"] = sorted(cur_worker_ids)
    except Exception:
        return


def _metrics_token_ok(request) -> bool:
    required = get_str(key="SCHEDULER_METRICS_TOKEN", default="", fresh=True).strip()
    if not required:
        return True
    got = (request.headers.get("X-Scheduler-Token") or "").strip()
    return got == required


def metrics_view(request):
    if generate_latest is None:
        return HttpResponse("prometheus_client not installed", status=500, content_type="text/plain; charset=utf-8")

    if not _metrics_token_ok(request):
        return HttpResponse("unauthorized", status=401, content_type="text/plain; charset=utf-8")

    _sync_metrics_from_db()
    _sync_metrics_from_redis()

    body = generate_latest()
    return HttpResponse(body, content_type=CONTENT_TYPE_LATEST)


def observe_job_started(*, command_name: str) -> None:
    if METRICS.job_runs_started_total is None:
        return
    try:
        METRICS.job_runs_started_total.labels(command_name=str(command_name or "")).inc()  # type: ignore[attr-defined]
    except Exception:
        return


def observe_job_finished(*, command_name: str, result: str, duration_seconds: float) -> None:
    if METRICS.job_runs_finished_total is None or METRICS.job_run_duration_seconds is None:
        return
    try:
        cn = str(command_name or "")
        rs = str(result or "")
        METRICS.job_runs_finished_total.labels(command_name=cn, result=rs).inc()  # type: ignore[attr-defined]
        METRICS.job_run_duration_seconds.labels(command_name=cn, result=rs).observe(float(duration_seconds))  # type: ignore[attr-defined]
    except Exception:
        return


def observe_job_resources(
    *,
    command_name: str,
    result: str,
    cpu_seconds_total: float | None,
    peak_rss_bytes: int | None,
    io_read_bytes: int | None,
    io_write_bytes: int | None,
) -> None:
    if (
        METRICS.job_run_cpu_seconds_total is None
        or METRICS.job_run_io_read_bytes_total is None
        or METRICS.job_run_io_write_bytes_total is None
        or METRICS.job_run_peak_rss_bytes is None
    ):
        return
    try:
        cn = str(command_name or "")
        rs = str(result or "")
        if cpu_seconds_total is not None:
            METRICS.job_run_cpu_seconds_total.labels(command_name=cn, result=rs).inc(float(cpu_seconds_total))  # type: ignore[attr-defined]
        if io_read_bytes is not None:
            METRICS.job_run_io_read_bytes_total.labels(command_name=cn, result=rs).inc(float(io_read_bytes))  # type: ignore[attr-defined]
        if io_write_bytes is not None:
            METRICS.job_run_io_write_bytes_total.labels(command_name=cn, result=rs).inc(float(io_write_bytes))  # type: ignore[attr-defined]
        if peak_rss_bytes is not None:
            METRICS.job_run_peak_rss_bytes.labels(command_name=cn, result=rs).observe(float(peak_rss_bytes))  # type: ignore[attr-defined]
    except Exception:
        return


def set_worker_current_job(*, worker_id: str, job_run_id: str, running: bool) -> None:
    if METRICS.worker_current_job is None:
        return
    try:
        g = METRICS.worker_current_job.labels(worker_id=str(worker_id or ""), job_run_id=str(job_run_id or ""))  # type: ignore[attr-defined]
        g.set(1.0 if running else 0.0)
    except Exception:
        return
