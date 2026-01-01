from __future__ import annotations

import json
from typing import Any

from scheduler.conf import get_str
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from scheduler.models import Event, JobDefinition, JobRun


def _get_client_token(request) -> str:
    return (request.headers.get("X-Scheduler-Token") or "").strip()


def _is_authenticated_for_events(request) -> bool:
    # Token can be overridden via SchedulerSetting (DB). Read fresh to avoid stale auth decisions.
    token_required = get_str(key="SCHEDULER_EVENTS_API_TOKEN", default="", fresh=True).strip()
    if token_required:
        return _get_client_token(request) == token_required
    return bool(getattr(request, "user", None) and request.user.is_authenticated)


def _event_job_matches(job_def: JobDefinition, event_type: str) -> bool:
    schedule = job_def.schedule or {}

    # MVP: schedule supports either {"event_type": "foo"} or {"event_types": ["foo", "bar"]}
    single = (schedule.get("event_type") or "").strip()
    if single:
        return single == event_type

    many = schedule.get("event_types")
    if isinstance(many, list):
        many_norm = [str(x).strip() for x in many if str(x).strip()]
        return event_type in many_norm

    return False


def _safe_body_json(request) -> dict[str, Any]:
    try:
        raw = request.body.decode("utf-8") if request.body else ""
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@csrf_exempt
@require_POST
def ingest_event(request):
    if not _is_authenticated_for_events(request):
        return JsonResponse({"ok": False, "errors": ["unauthorized"]}, status=401)

    data = _safe_body_json(request)
    event_type = (data.get("event_type") or "").strip()
    if not event_type:
        return JsonResponse({"ok": False, "errors": ["event_type is required"]}, status=400)

    payload_json = data.get("payload_json")
    if payload_json is None:
        payload_json = {}
    if not isinstance(payload_json, (dict, list)):
        return JsonResponse({"ok": False, "errors": ["payload_json must be object or array"]}, status=400)

    dedupe_key = data.get("dedupe_key")
    if dedupe_key is not None:
        dedupe_key = str(dedupe_key).strip() or None

    # MVP idempotency: if dedupe_key exists and same event_type+dedupe_key already stored, return it.
    if dedupe_key:
        existing = (
            Event.objects.filter(event_type=event_type, dedupe_key=dedupe_key)
            .order_by("-id")
            .only("id", "processed_at")
            .first()
        )
        if existing is not None:
            return JsonResponse(
                {
                    "ok": True,
                    "event_id": existing.id,
                    "deduped": True,
                    "created_job_run_ids": [],
                }
            )

    now = timezone.now()

    ev = Event.objects.create(
        event_type=event_type,
        payload_json=payload_json,
        dedupe_key=dedupe_key,
        processed_at=None,
    )

    # Find matching enabled event jobs
    job_defs = JobDefinition.objects.filter(enabled=True, type=JobDefinition.JobType.EVENT).only("id", "schedule")
    matched_job_ids: list[int] = []
    created_run_ids: list[int] = []

    for jd in job_defs:
        if not _event_job_matches(jd, event_type):
            continue
        matched_job_ids.append(int(jd.id))

        # Use microsecond timestamp to avoid collisions; keep scheduled_for for ordering.
        # Store some idempotency marker for observability / future fencing.
        if dedupe_key:
            idem = f"event:{event_type}:{dedupe_key}:job:{jd.id}"
        else:
            idem = f"event:{ev.id}:job:{jd.id}"

        jr = JobRun.objects.create(
            job_definition=jd,
            scheduled_for=now,
            state=JobRun.State.PENDING,
            attempt=0,
            idempotency_key=idem,
        )
        created_run_ids.append(int(jr.id))

    ev.processed_at = timezone.now()
    ev.save(update_fields=["processed_at"])

    return JsonResponse(
        {
            "ok": True,
            "event_id": ev.id,
            "deduped": False,
            "matched_job_definition_ids": matched_job_ids,
            "created_job_run_ids": created_run_ids,
        }
    )
