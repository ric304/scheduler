from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from scheduler.models import JobDefinition, JobRun
from scheduler.redis_coordination import list_workers


@dataclass(frozen=True)
class LeaderTickSnapshot:
    enabled_job_definitions: int
    pending_job_runs: int
    created_job_runs: int
    assigned_job_runs: int
    orphaned_job_runs: int
    confirming_job_runs: int
    reassigned_job_runs: int
    rebalanced_job_runs: int


def _floor_to_minute(dt):
    return dt.replace(second=0, microsecond=0)


def _iter_minute_slots(start_dt, end_dt):
    cur = start_dt
    while cur <= end_dt:
        yield cur
        cur = cur + timedelta(minutes=1)


def _schedule_matches_every_n_minutes(dt, every_n_minutes: int) -> bool:
    if every_n_minutes <= 0:
        return False
    return (dt.minute % every_n_minutes) == 0


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    s = (value or "").strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except Exception:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _schedule_matches_slot(dt, schedule: dict) -> bool:
    """Return True if the given minute slot should run for schedule.

    Supports:
    - Legacy: {"every_n_minutes": N}
    - M3': {"kind": "every_n_minutes"|"hourly"|"daily"|"weekdays"|"weekly", ...}
    """
    if not isinstance(schedule, dict):
        return False

    # Legacy format
    legacy_n = schedule.get("every_n_minutes")
    if legacy_n is not None and "kind" not in schedule:
        try:
            n = int(legacy_n)
        except Exception:
            return False
        return _schedule_matches_every_n_minutes(dt, n)

    kind = str(schedule.get("kind") or "").strip()
    if kind == "every_n_minutes":
        try:
            n = int(schedule.get("n") or 0)
        except Exception:
            return False
        return _schedule_matches_every_n_minutes(dt, n)

    if kind == "hourly":
        raw_minute = schedule.get("minute", 0)
        try:
            minute = int(raw_minute)
        except Exception:
            return False
        if not (0 <= minute <= 59):
            return False
        return dt.minute == minute

    if kind == "daily":
        hm = _parse_hhmm(str(schedule.get("time") or ""))
        if not hm:
            return False
        hh, mm = hm
        return dt.hour == hh and dt.minute == mm

    if kind == "weekdays":
        hm = _parse_hhmm(str(schedule.get("time") or ""))
        if not hm:
            return False
        hh, mm = hm
        if dt.weekday() >= 5:
            return False
        return dt.hour == hh and dt.minute == mm

    if kind == "weekly":
        hm = _parse_hhmm(str(schedule.get("time") or ""))
        if not hm:
            return False
        hh, mm = hm
        raw_weekday = schedule.get("weekday", -1)
        try:
            weekday = int(raw_weekday)
        except Exception:
            return False
        if not (0 <= weekday <= 6):
            return False
        return dt.weekday() == weekday and dt.hour == hh and dt.minute == mm

    return False


def _ensure_job_run(job_definition: JobDefinition, scheduled_for) -> bool:
    """Ensure a JobRun exists for (job_definition, scheduled_for).

    Returns True if created, False if already existed.
    """

    try:
        _, created = JobRun.objects.get_or_create(
            job_definition=job_definition,
            scheduled_for=scheduled_for,
            defaults={"state": JobRun.State.PENDING, "attempt": 0},
        )
        return bool(created)
    except IntegrityError:
        # Another leader tick created it concurrently.
        return False


def run_leader_tick_snapshot(
    *,
    redis_url: str,
    leader_epoch: int,
    assign_ahead_seconds: int,
    reassign_assigned_after_seconds: int,
    continuation_confirm_seconds: int,
    assign_weight_leader: int,
    assign_weight_subleader: int,
    assign_weight_worker: int,
    assign_running_load_weight: int,
    rebalance_assigned_enabled: bool,
    rebalance_assigned_min_future_seconds: int,
    rebalance_assigned_max_per_tick: int,
    rebalance_assigned_cooldown_seconds: int,
) -> LeaderTickSnapshot:
    """Leader tick (M3):
    - Create upcoming JobRuns for time-based jobs (supports legacy every_n_minutes and M3' schedule kinds)
    - Assign eligible pending runs to active workers (no execution yet)
    """

    now = timezone.now()
    window_start = _floor_to_minute(now)
    window_end = _floor_to_minute(now + timedelta(seconds=max(0, int(assign_ahead_seconds))))

    # Active workers from Redis
    workers = [w for w in list_workers(redis_url) if w.heartbeat_ttl_seconds > 0]
    worker_ids = [w.worker_id for w in workers]
    role_by_worker_id = {w.worker_id: ("leader" if w.is_leader else ("subleader" if w.is_subleader else "worker")) for w in workers}

    created_job_runs = 0
    assigned_job_runs = 0
    orphaned_job_runs = 0
    confirming_job_runs = 0
    reassigned_job_runs = 0
    rebalanced_job_runs = 0

    with transaction.atomic():
        enabled_defs = JobDefinition.objects.filter(enabled=True).count()

        # M5 MVP: orphan/reassign when assigned worker is not active
        reassign_assigned_after_seconds = max(1, int(reassign_assigned_after_seconds))
        continuation_confirm_seconds = max(1, int(continuation_confirm_seconds))

        active_worker_set = set(worker_ids)

        # 0) ASSIGNED that did not start and worker is inactive -> ORPHANED
        assigned_cutoff = now - timedelta(seconds=max(0, int(reassign_assigned_after_seconds)))
        stuck_assigned = (
            JobRun.objects.select_for_update(skip_locked=True)
            .filter(
                state=JobRun.State.ASSIGNED,
                assigned_at__isnull=False,
                assigned_at__lt=assigned_cutoff,
            )
            .exclude(assigned_worker_id="")
            .order_by("assigned_at", "id")
        )
        for jr in stuck_assigned:
            if jr.assigned_worker_id in active_worker_set:
                continue
            jr.state = JobRun.State.ORPHANED
            jr.error_summary = (jr.error_summary + "\n" if jr.error_summary else "") + "orphaned: assigned worker inactive"
            jr.assigned_worker_id = ""
            jr.assigned_at = None
            jr.version = int(jr.version) + 1
            jr.attempt = int(jr.attempt) + 1
            jr.save(update_fields=["state", "error_summary", "assigned_worker_id", "assigned_at", "version", "attempt", "updated_at"])
            orphaned_job_runs += 1

        # 0.1) RUNNING with missing worker -> enter CONFIRMING, and after deadline -> ORPHANED
        running = (
            JobRun.objects.select_for_update(skip_locked=True)
            .filter(state=JobRun.State.RUNNING)
            .exclude(assigned_worker_id="")
            .order_by("started_at", "id")
        )
        for jr in running:
            if jr.continuation_state == JobRun.ContinuationState.NONE:
                if jr.assigned_worker_id in active_worker_set:
                    continue
                jr.continuation_state = JobRun.ContinuationState.CONFIRMING
                jr.continuation_check_started_at = now
                jr.continuation_check_deadline_at = now + timedelta(seconds=max(1, int(continuation_confirm_seconds)))
                jr.version = int(jr.version) + 1
                jr.save(
                    update_fields=[
                        "continuation_state",
                        "continuation_check_started_at",
                        "continuation_check_deadline_at",
                        "version",
                        "updated_at",
                    ]
                )
                confirming_job_runs += 1
                continue

            if (
                jr.continuation_state == JobRun.ContinuationState.CONFIRMING
                and jr.continuation_check_deadline_at
                and jr.continuation_check_deadline_at <= now
            ):
                jr.state = JobRun.State.ORPHANED
                jr.error_summary = (jr.error_summary + "\n" if jr.error_summary else "") + "orphaned: confirming deadline exceeded"
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
                orphaned_job_runs += 1

        # 1) Create JobRuns for time jobs within lookahead window
        time_jobs = JobDefinition.objects.filter(enabled=True, type=JobDefinition.JobType.TIME).only(
            "id",
            "schedule",
            "concurrency_policy",
        )
        for jd in time_jobs:
            schedule = jd.schedule or {}
            for slot in _iter_minute_slots(window_start, window_end):
                if not _schedule_matches_slot(slot, schedule):
                    continue
                if _ensure_job_run(jd, slot):
                    created_job_runs += 1

        # 2) Assign pending (and orphaned) runs
        if worker_ids:
            # Load snapshot from DB: counts by worker for ASSIGNED/RUNNING
            assigned_counts = {
                row["assigned_worker_id"]: int(row["c"])
                for row in JobRun.objects.filter(state=JobRun.State.ASSIGNED)
                .exclude(assigned_worker_id="")
                .values("assigned_worker_id")
                .annotate(c=Count("id"))
            }
            running_counts = {
                row["assigned_worker_id"]: int(row["c"])
                for row in JobRun.objects.filter(state=JobRun.State.RUNNING)
                .exclude(assigned_worker_id="")
                .values("assigned_worker_id")
                .annotate(c=Count("id"))
            }

            # Role weights: leader/subleader get fewer assignments by default.
            weight_leader = max(1, int(assign_weight_leader))
            weight_subleader = max(1, int(assign_weight_subleader))
            weight_worker = max(1, int(assign_weight_worker))
            running_load_weight = max(1, int(assign_running_load_weight))

            def _weight_for(worker_id: str) -> int:
                role = role_by_worker_id.get(worker_id, "worker")
                if role == "leader":
                    return max(1, weight_leader)
                if role == "subleader":
                    return max(1, weight_subleader)
                return max(1, weight_worker)

            def _effective_load(worker_id: str) -> int:
                return int(assigned_counts.get(worker_id, 0)) + int(running_counts.get(worker_id, 0)) * max(1, running_load_weight)

            def _pick_worker(job_run_id: int) -> str:
                # Pick least (load/weight); stable tie-breaker by worker_id and job_run_id.
                best_worker_id = worker_ids[0]
                best_num = _effective_load(best_worker_id)
                best_den = _weight_for(best_worker_id)
                for wid in worker_ids[1:]:
                    num = _effective_load(wid)
                    den = _weight_for(wid)
                    # Compare fractions without float: num/den < best_num/best_den
                    if (num * best_den) < (best_num * den):
                        best_worker_id = wid
                        best_num = num
                        best_den = den
                    elif (num * best_den) == (best_num * den):
                        # Stable tie-break
                        if wid < best_worker_id:
                            best_worker_id = wid
                            best_num = num
                            best_den = den
                return best_worker_id

            # 1.5) Optional: rebalance ASSIGNED runs (not started) to new/less-loaded workers.
            # This is intentionally conservative to avoid churn:
            # - Only future runs (scheduled_for sufficiently ahead)
            # - Only if assigned_at is older than cooldown
            if rebalance_assigned_enabled and len(worker_ids) > 1:
                min_future_seconds = max(0, int(rebalance_assigned_min_future_seconds))
                cooldown_seconds = max(0, int(rebalance_assigned_cooldown_seconds))
                max_per_tick = max(0, int(rebalance_assigned_max_per_tick))

                future_cutoff = now + timedelta(seconds=min_future_seconds)
                cooldown_cutoff = now - timedelta(seconds=cooldown_seconds)

                candidates = (
                    JobRun.objects.select_for_update(skip_locked=True)
                    .filter(
                        state=JobRun.State.ASSIGNED,
                        started_at__isnull=True,
                        scheduled_for__isnull=False,
                        scheduled_for__gt=future_cutoff,
                    )
                    .exclude(assigned_worker_id="")
                    .order_by("assigned_at", "id")
                )
                if max_per_tick > 0:
                    candidates = candidates[:max_per_tick]

                for jr in candidates:
                    if jr.assigned_worker_id not in active_worker_set:
                        continue
                    if jr.assigned_at and jr.assigned_at > cooldown_cutoff:
                        continue

                    current = jr.assigned_worker_id
                    # Evaluate assignment excluding this run from current worker.
                    assigned_counts[current] = max(0, int(assigned_counts.get(current, 0)) - 1)
                    best = _pick_worker(int(jr.id))
                    if best == current:
                        assigned_counts[current] = int(assigned_counts.get(current, 0)) + 1
                        continue

                    jr.assigned_worker_id = best
                    jr.assigned_at = now
                    jr.leader_epoch = int(leader_epoch)
                    jr.error_summary = (jr.error_summary + "\n" if jr.error_summary else "") + f"rebalanced: {current} -> {best}"
                    jr.version = int(jr.version) + 1
                    jr.save(update_fields=["assigned_worker_id", "assigned_at", "leader_epoch", "error_summary", "version", "updated_at"])

                    assigned_counts[best] = int(assigned_counts.get(best, 0)) + 1
                    rebalanced_job_runs += 1

            # Assign only runs in the current window (including the immediate lookahead)
            pending = (
                JobRun.objects.select_for_update(skip_locked=True)
                .filter(
                    state__in=[JobRun.State.PENDING, JobRun.State.ORPHANED],
                    scheduled_for__isnull=False,
                    scheduled_for__lte=window_end,
                )
                .order_by("scheduled_for", "id")
            )

            for jr in pending:
                is_reassign = jr.state == JobRun.State.ORPHANED
                assigned_worker_id = _pick_worker(int(jr.id))
                jr.assigned_worker_id = assigned_worker_id
                jr.assigned_at = now
                jr.state = JobRun.State.ASSIGNED
                jr.leader_epoch = int(leader_epoch)
                jr.version = int(jr.version) + 1
                jr.save(update_fields=["assigned_worker_id", "assigned_at", "state", "leader_epoch", "version", "updated_at"])
                assigned_job_runs += 1
                if is_reassign:
                    reassigned_job_runs += 1

                # Update in-memory load snapshot so the batch balances well.
                assigned_counts[assigned_worker_id] = int(assigned_counts.get(assigned_worker_id, 0)) + 1

        pending_runs = JobRun.objects.filter(state=JobRun.State.PENDING).count()

    return LeaderTickSnapshot(
        enabled_job_definitions=enabled_defs,
        pending_job_runs=pending_runs,
        created_job_runs=created_job_runs,
        assigned_job_runs=assigned_job_runs,
        orphaned_job_runs=orphaned_job_runs,
        confirming_job_runs=confirming_job_runs,
        reassigned_job_runs=reassigned_job_runs,
        rebalanced_job_runs=rebalanced_job_runs,
    )
