from __future__ import annotations

import json
import math
import smtplib
import time
import urllib.parse
import urllib.request
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Sum
from django.db.models.functions import TruncMinute

from scheduler.conf import (
    get_scheduler_config,
    get_setting_with_source,
    list_all_scheduler_setting_keys,
    get_str,
)
from scheduler.help_seed import ensure_setting_help_rows
from scheduler.redis_coordination import get_cluster_leadership, list_workers
from scheduler.models import AdminActionLog, ConfigReloadRequest, JobDefinition, JobRun, SchedulerSetting, SchedulerSettingHelp

from scheduler_ops.roles import OPS_ROLES, ensure_ops_groups, is_app_operator, is_ops_admin, is_superuser


_PROM_CACHE: dict[str, object] = {"ts": 0.0, "data": None}
_HEALTH_CACHE: dict[str, object] = {"offline_since": None}
_ALERTS_CACHE: dict[str, object] = {"ts": 0.0, "data": None}


def _sanitize_json_numbers(obj):
    """Convert non-finite floats (NaN/Inf) into None so responses are valid JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_json_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json_numbers(v) for v in obj]
    if isinstance(obj, tuple):
        return [_sanitize_json_numbers(v) for v in obj]
    return obj


def _prometheus_base_url() -> str:
    return get_str(key="SCHEDULER_PROMETHEUS_URL", default="", fresh=True).strip().rstrip("/")


def _prometheus_query(*, base_url: str, query: str, timeout_seconds: float = 2.0) -> tuple[Optional[object], Optional[str]]:
    if not base_url:
        return None, "not configured"
    try:
        url = f"{base_url}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=float(timeout_seconds)) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("status") != "success":
            return None, "query failed"
        data = payload.get("data") or {}
        return data.get("result"), None
    except Exception as e:
        return None, f"{type(e).__name__}"


def _prometheus_query_range(
    *,
    base_url: str,
    query: str,
    start_unix: float,
    end_unix: float,
    step_seconds: int,
    timeout_seconds: float = 2.0,
) -> tuple[Optional[object], Optional[str]]:
    if not base_url:
        return None, "not configured"
    try:
        params = {
            "query": query,
            "start": str(float(start_unix)),
            "end": str(float(end_unix)),
            "step": str(int(step_seconds)),
        }
        url = f"{base_url}/api/v1/query_range?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=float(timeout_seconds)) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("status") != "success":
            return None, "query failed"
        data = payload.get("data") or {}
        return data.get("result"), None
    except Exception as e:
        return None, f"{type(e).__name__}"


def _prometheus_summary_cached() -> dict:
    base = _prometheus_base_url()
    if not base:
        return {"enabled": False}

    now = time.time()
    ts_val = _PROM_CACHE.get("ts")
    last_ts = float(ts_val) if isinstance(ts_val, (int, float)) else 0.0
    cached = _PROM_CACHE.get("data")
    if cached is not None and (now - last_ts) < 5.0:
        sanitized = _sanitize_json_numbers(cached)
        if sanitized is not cached:
            _PROM_CACHE["data"] = sanitized
        return sanitized  # type: ignore[return-value]

    # Queries (keep minimal; avoid expensive per-command breakdown for MVP)
    q_running = "sum(scheduler_worker_current_job_run)"
    q_finished_5m = "sum(increase(scheduler_job_runs_finished_total[5m])) by (result)"
    q_p95_5m = "histogram_quantile(0.95, sum(rate(scheduler_job_run_duration_seconds_bucket[5m])) by (le))"

    # Overall load (derived from per-job resource metrics recorded at completion)
    q_cpu_cores_5m = "sum(rate(scheduler_job_run_cpu_seconds_total[5m]))"
    q_io_read_bps_5m = "sum(rate(scheduler_job_run_io_read_bytes_total[5m]))"
    q_io_write_bps_5m = "sum(rate(scheduler_job_run_io_write_bytes_total[5m]))"
    q_mem_p95_5m = "histogram_quantile(0.95, sum(rate(scheduler_job_run_peak_rss_bytes_bucket[5m])) by (le))"

    running_res, err1 = _prometheus_query(base_url=base, query=q_running)
    finished_res, err2 = _prometheus_query(base_url=base, query=q_finished_5m)
    p95_res, err3 = _prometheus_query(base_url=base, query=q_p95_5m)

    cpu_res, err4 = _prometheus_query(base_url=base, query=q_cpu_cores_5m)
    io_r_res, err5 = _prometheus_query(base_url=base, query=q_io_read_bps_5m)
    io_w_res, err6 = _prometheus_query(base_url=base, query=q_io_write_bps_5m)
    mem_p95_res, err7 = _prometheus_query(base_url=base, query=q_mem_p95_5m)

    # Small sparklines (last 30 minutes, 60s step)
    end_unix = time.time()
    start_unix = end_unix - (30 * 60)
    cpu_series_res, err8 = _prometheus_query_range(
        base_url=base,
        query=q_cpu_cores_5m,
        start_unix=start_unix,
        end_unix=end_unix,
        step_seconds=60,
    )
    mem_series_res, err9 = _prometheus_query_range(
        base_url=base,
        query=q_mem_p95_5m,
        start_unix=start_unix,
        end_unix=end_unix,
        step_seconds=60,
    )

    err = err1 or err2 or err3 or err4 or err5 or err6 or err7 or err8 or err9

    def _finite_float(v) -> Optional[float]:
        try:
            x = float(v)
            if not math.isfinite(x):
                return None
            return x
        except Exception:
            return None

    def _first_float(result) -> Optional[float]:
        try:
            if not result:
                return None
            v = result[0].get("value")
            if not v or len(v) < 2:
                return None
            return _finite_float(v[1])
        except Exception:
            return None

    running = _first_float(running_res)
    cpu_cores = _first_float(cpu_res)
    io_read_bps = _first_float(io_r_res)
    io_write_bps = _first_float(io_w_res)
    mem_p95_bytes = _first_float(mem_p95_res)

    finished: dict[str, float] = {}
    try:
        if isinstance(finished_res, list):
            for row in finished_res:
                if not isinstance(row, dict):
                    continue
                metric = row.get("metric") or {}
                if not isinstance(metric, dict):
                    metric = {}
                result_label = str(metric.get("result") or "")
                v = row.get("value")
                if result_label and v and len(v) >= 2:
                    fv = _finite_float(v[1])
                    if fv is not None:
                        finished[result_label] = fv
    except Exception:
        finished = {}

    p95 = _first_float(p95_res)

    def _series_to_points(result) -> list[list[float]]:
        # Returns [[unix_ms, value], ...] for the first series.
        try:
            if not result:
                return []
            first = result[0]
            if not isinstance(first, dict):
                return []
            values = first.get("values")
            if not isinstance(values, list):
                return []
            out: list[list[float]] = []
            for pair in values:
                if not pair or len(pair) < 2:
                    continue
                ts_s = float(pair[0])
                fv = _finite_float(pair[1])
                if fv is None:
                    continue
                out.append([ts_s * 1000.0, fv])
            return out
        except Exception:
            return []

    out = {
        "enabled": True,
        "ok": err is None,
        "error": None if err is None else str(err),
        "running_jobs": int(running or 0),
        "finished_5m": finished,
        "p95_duration_seconds_5m": p95,
        "load": {
            "cpu_cores_5m": cpu_cores,
            "io_read_bps_5m": io_read_bps,
            "io_write_bps_5m": io_write_bps,
            "mem_p95_bytes_5m": mem_p95_bytes,
        },
        "sparklines": {
            "cpu_cores_5m": _series_to_points(cpu_series_res),
            "mem_p95_bytes_5m": _series_to_points(mem_series_res),
        },
    }
    out = _sanitize_json_numbers(out)
    _PROM_CACHE["ts"] = now
    _PROM_CACHE["data"] = out
    return out


def _prometheus_alerts_cached() -> dict:
    base = _prometheus_base_url()
    if not base:
        return {"enabled": False}

    now = time.time()
    ts_val = _ALERTS_CACHE.get("ts")
    last_ts = float(ts_val) if isinstance(ts_val, (int, float)) else 0.0
    cached = _ALERTS_CACHE.get("data")
    if cached is not None and (now - last_ts) < 5.0:
        sanitized = _sanitize_json_numbers(cached)
        if sanitized is not cached:
            _ALERTS_CACHE["data"] = sanitized
        return sanitized  # type: ignore[return-value]

    err: Optional[str] = None
    alerts_out: list[dict] = []
    try:
        url = f"{base}/api/v1/alerts"
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("status") != "success":
            err = "alerts query failed"
        else:
            data = payload.get("data") or {}
            alerts = data.get("alerts")
            if isinstance(alerts, list):
                for a in alerts:
                    if not isinstance(a, dict):
                        continue
                    labels = a.get("labels") if isinstance(a.get("labels"), dict) else {}
                    annotations = a.get("annotations") if isinstance(a.get("annotations"), dict) else {}
                    state = str(a.get("state") or "")
                    name = str(labels.get("alertname") or "")
                    alerts_out.append(
                        {
                            "state": state,
                            "name": name,
                            "severity": str(labels.get("severity") or ""),
                            "active_at": a.get("activeAt"),
                            "summary": str(annotations.get("summary") or annotations.get("description") or ""),
                            "labels": labels,
                            "annotations": annotations,
                        }
                    )
    except Exception as e:
        err = type(e).__name__

    out = {
        "enabled": True,
        "ok": err is None,
        "error": None if err is None else str(err),
        "alerts": alerts_out,
    }
    out = _sanitize_json_numbers(out)
    _ALERTS_CACHE["ts"] = now
    _ALERTS_CACHE["data"] = out
    return out


def _require_app_operator(view_func):
    def _wrapped(request, *args, **kwargs):
        ensure_ops_groups()
        if not is_app_operator(request.user):
            raise PermissionDenied("App operator role required")
        return view_func(request, *args, **kwargs)

    return _wrapped


def _require_ops_admin(view_func):
    def _wrapped(request, *args, **kwargs):
        ensure_ops_groups()
        if not is_ops_admin(request.user):
            raise PermissionDenied("Ops admin role required")
        return view_func(request, *args, **kwargs)

    return _wrapped


def _require_superuser(view_func):
    def _wrapped(request, *args, **kwargs):
        ensure_ops_groups()
        if not is_superuser(request.user):
            raise PermissionDenied("Superuser role required")
        return view_func(request, *args, **kwargs)

    return _wrapped


def _ops_group_names() -> list[str]:
    return [OPS_ROLES.APP_OPERATOR, OPS_ROLES.OPS_ADMIN, OPS_ROLES.SUPERUSER]


def _user_payload(u) -> dict:
    groups = set(u.groups.values_list("name", flat=True))
    return {
        "id": u.id,
        "username": u.username,
        "is_active": bool(u.is_active),
        "last_login": u.last_login.isoformat() if u.last_login else None,
        "ops_roles": {
            "app_operator": OPS_ROLES.APP_OPERATOR in groups,
            "ops_admin": OPS_ROLES.OPS_ADMIN in groups,
            "superuser": OPS_ROLES.SUPERUSER in groups,
        },
    }


@login_required
@_require_app_operator
def index(request):
    cfg = get_scheduler_config()
    leadership = get_cluster_leadership(cfg.redis_url)
    workers_list = list_workers(cfg.redis_url)
    active_workers = sum(1 for w in workers_list if w.heartbeat_ttl_seconds > 0)
    min_online_workers = get_str(key="SCHEDULER_MIN_ONLINE_WORKERS", default="1", fresh=True).strip() or "1"

    high_load_threshold = get_str(key="SCHEDULER_WORKER_HIGH_LOAD_THRESHOLD", default="10", fresh=True).strip() or "10"
    high_load_enabled = get_str(key="SCHEDULER_ALERT_HIGH_LOAD_ENABLED", default="1", fresh=True).strip()
    role_change_enabled = get_str(key="SCHEDULER_ALERT_ROLE_CHANGE_ENABLED", default="1", fresh=True).strip()

    slack_url = get_str(key="SCHEDULER_NOTIFY_SLACK_WEBHOOK_URL", default="", fresh=True).strip()
    teams_url = get_str(key="SCHEDULER_NOTIFY_TEAMS_WEBHOOK_URL", default="", fresh=True).strip()
    email_to = get_str(key="SCHEDULER_NOTIFY_EMAIL_TO", default="", fresh=True).strip()

    def _truthy(s: str) -> bool:
        return str(s or "").strip().lower() not in {"", "0", "false", "no"}

    return render(
        request,
        "scheduler_ops/index.html",
        {
            "settings": settings,
            "leadership": leadership,
            "active_workers": active_workers,
            "min_online_workers": min_online_workers,
            "high_load_threshold": high_load_threshold,
            "high_load_enabled": _truthy(high_load_enabled),
            "role_change_enabled": _truthy(role_change_enabled),
            "notify_slack_configured": bool(slack_url),
            "notify_teams_configured": bool(teams_url),
            "notify_email_to": email_to,
        },
    )


def _send_slack(*, webhook_url: str, text: str) -> Optional[str]:
    if not webhook_url:
        return None
    try:
        payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            _ = resp.read(1)
        return None
    except Exception as e:
        return type(e).__name__


def _send_teams(*, webhook_url: str, text: str) -> Optional[str]:
    if not webhook_url:
        return None
    try:
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": "Scheduler Alert",
            "title": "Scheduler Alert",
            "text": text,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            _ = resp.read(1)
        return None
    except Exception as e:
        return type(e).__name__


def _send_email(*, subject: str, body: str) -> Optional[str]:
    to_raw = get_str(key="SCHEDULER_NOTIFY_EMAIL_TO", default="", fresh=True).strip()
    if not to_raw:
        return None

    host = get_str(key="SCHEDULER_NOTIFY_SMTP_HOST", default="", fresh=True).strip()
    if not host:
        return "smtp not configured"

    port_raw = get_str(key="SCHEDULER_NOTIFY_SMTP_PORT", default="587", fresh=True).strip() or "587"
    try:
        port = int(port_raw)
    except Exception:
        port = 587

    user = get_str(key="SCHEDULER_NOTIFY_SMTP_USER", default="", fresh=True).strip()
    password = get_str(key="SCHEDULER_NOTIFY_SMTP_PASSWORD", default="", fresh=True)
    from_addr = get_str(key="SCHEDULER_NOTIFY_EMAIL_FROM", default="", fresh=True).strip() or user or "scheduler@localhost"
    use_tls_raw = get_str(key="SCHEDULER_NOTIFY_SMTP_USE_TLS", default="1", fresh=True).strip()
    use_tls = str(use_tls_raw).lower() not in {"", "0", "false", "no"}

    to_addrs = [a.strip() for a in to_raw.split(",") if a.strip()]
    if not to_addrs:
        return None

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)
        msg.set_content(body)

        with smtplib.SMTP(host=host, port=port, timeout=5) as s:
            s.ehlo()
            if use_tls:
                try:
                    s.starttls()
                    s.ehlo()
                except Exception as e:
                    return f"starttls failed: {type(e).__name__}"

            if user:
                try:
                    s.login(user, password)
                except Exception as e:
                    return f"smtp auth failed: {type(e).__name__}"

            s.send_message(msg)
        return None
    except Exception as e:
        # Keep message minimal (avoid leaking details)
        return f"send failed: {type(e).__name__}"


@csrf_exempt
@require_POST
def api_alert_webhook(request, token: str):
    """Alertmanager -> Ops webhook.

    URL includes a simple token to prevent casual spoofing.
    """

    required = get_str(key="SCHEDULER_ALERT_WEBHOOK_TOKEN", default="dev", fresh=True).strip() or "dev"
    if str(token or "") != required:
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        payload = {}

    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        alerts = []

    lines: list[str] = []
    for a in alerts[:20]:
        if not isinstance(a, dict):
            continue
        status = str(a.get("status") or "")
        labels = a.get("labels") if isinstance(a.get("labels"), dict) else {}
        annotations = a.get("annotations") if isinstance(a.get("annotations"), dict) else {}
        name = str(labels.get("alertname") or "")
        sev = str(labels.get("severity") or "")
        summary = str(annotations.get("summary") or annotations.get("description") or "")
        lines.append(f"[{status}] {name} ({sev}) {summary}".strip())

    if not lines:
        lines = ["(no alerts)"]

    text = "\n".join(lines)
    subject = "[Scheduler] Alert"

    slack_url = get_str(key="SCHEDULER_NOTIFY_SLACK_WEBHOOK_URL", default="", fresh=True).strip()
    teams_url = get_str(key="SCHEDULER_NOTIFY_TEAMS_WEBHOOK_URL", default="", fresh=True).strip()

    slack_err = _send_slack(webhook_url=slack_url, text=text)
    teams_err = _send_teams(webhook_url=teams_url, text=text)
    mail_err = _send_email(subject=subject, body=text)

    sent = {
        "slack": bool(slack_url) and slack_err is None,
        "teams": bool(teams_url) and teams_err is None,
        "email": bool(get_str(key="SCHEDULER_NOTIFY_EMAIL_TO", default="", fresh=True).strip()) and mail_err is None,
    }
    errs = {"slack": slack_err, "teams": teams_err, "email": mail_err}

    try:
        AdminActionLog.objects.create(
            actor="",
            action="alert.webhook",
            target="alertmanager",
            payload_json={"sent": sent, "errors": errs},
        )
    except Exception:
        pass

    return JsonResponse({"ok": True, "sent": sent, "errors": errs})


@login_required
@_require_ops_admin
def workers(request):
    cfg = get_scheduler_config()
    leadership = get_cluster_leadership(cfg.redis_url)
    workers_list = list_workers(cfg.redis_url)
    return render(
        request,
        "scheduler_ops/workers.html",
        {
            "settings": settings,
            "leadership": leadership,
            "workers": workers_list,
        },
    )


@login_required
@_require_app_operator
def jobs(request):
    job_defs = JobDefinition.objects.order_by("id")
    return render(
        request,
        "scheduler_ops/jobs.html",
        {
            "settings": settings,
            "job_defs": job_defs,
            "jobs_payload": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "type": j.type,
                    "command_name": j.command_name,
                    "default_args_json": j.default_args_json,
                    "schedule": j.schedule,
                    "timeout_seconds": j.timeout_seconds,
                    "max_retries": j.max_retries,
                    "updated_at": j.updated_at.isoformat() if j.updated_at else None,
                }
                for j in job_defs
            ],
        },
    )


@login_required
@_require_app_operator
def job_runs(request):
    runs = (
        JobRun.objects.select_related("job_definition")
        .order_by("-id")
        .only(
            "id",
            "state",
            "attempt",
            "scheduled_for",
            "assigned_worker_id",
            "started_at",
            "finished_at",
            "error_summary",
            "log_ref",
            "resource_cpu_seconds_total",
            "resource_peak_rss_bytes",
            "resource_io_read_bytes",
            "resource_io_write_bytes",
            "job_definition__id",
            "job_definition__name",
            "job_definition__type",
            "job_definition__schedule",
        )[:500]
    )
    return render(
        request,
        "scheduler_ops/job_runs.html",
        {
            "settings": settings,
            "runs": runs,
        },
    )


@login_required
@_require_ops_admin
def settings_page(request):
    ensure_setting_help_rows(apply_defaults=True)
    return render(
        request,
        "scheduler_ops/settings.html",
        {
            "settings": settings,
            "all_keys": list_all_scheduler_setting_keys(fresh=True),
        },
    )


@login_required
@_require_superuser
def users(request):
    return render(
        request,
        "scheduler_ops/users.html",
        {
            "settings": settings,
        },
    )


@login_required
@_require_superuser
def api_users(request):
    ensure_ops_groups()
    User = get_user_model()
    qs = User.objects.order_by("id").prefetch_related("groups")
    return JsonResponse(
        {
            "ok": True,
            "server_time": timezone.now().isoformat(),
            "users": [_user_payload(u) for u in qs],
        }
    )


def _set_ops_roles_for_user(*, user, roles: dict) -> None:
    ensure_ops_groups()
    wanted = set()
    if bool((roles or {}).get("app_operator")):
        wanted.add(OPS_ROLES.APP_OPERATOR)
    if bool((roles or {}).get("ops_admin")):
        wanted.add(OPS_ROLES.OPS_ADMIN)
    if bool((roles or {}).get("superuser")):
        wanted.add(OPS_ROLES.SUPERUSER)

    ops_groups = {g.name: g for g in Group.objects.filter(name__in=_ops_group_names())}
    # Remove all ops groups then add wanted.
    user.groups.remove(*[ops_groups[n] for n in ops_groups.keys()])
    user.groups.add(*[ops_groups[n] for n in wanted if n in ops_groups])


@login_required
@_require_superuser
@require_POST
def api_users_create(request):
    data = _parse_json_body(request)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    is_active = bool(data.get("is_active", True))
    roles = data.get("roles") if isinstance(data.get("roles"), dict) else {}

    errors = []
    if not username:
        errors.append("username is required")
    if not password:
        errors.append("password is required")
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    User = get_user_model()
    if User.objects.filter(username=username).exists():
        return JsonResponse({"ok": False, "errors": ["username already exists"]}, status=400)

    u = User(username=username, is_active=is_active)
    u.set_password(password)
    u.save()
    _set_ops_roles_for_user(user=u, roles=roles or {})

    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="user.create",
        target=str(u.id),
        payload_json={"username": u.username},
    )

    return JsonResponse({"ok": True, "server_time": timezone.now().isoformat(), "user": _user_payload(u)})


@login_required
@_require_superuser
@require_POST
def api_users_update(request, user_id: int):
    data = _parse_json_body(request)
    is_active = bool(data.get("is_active", True))
    roles = data.get("roles") if isinstance(data.get("roles"), dict) else {}
    password = str(data.get("password") or "")

    User = get_user_model()
    try:
        u = User.objects.prefetch_related("groups").get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"ok": False, "errors": ["user not found"]}, status=404)

    # Prevent accidental lockout of the current operator (when they are not a Django superuser).
    if int(getattr(request.user, "id", -1)) == int(u.id):
        if not bool(getattr(u, "is_superuser", False)) and not bool((roles or {}).get("superuser")):
            return JsonResponse({"ok": False, "errors": ["cannot remove own superuser role"]}, status=400)

    u.is_active = is_active
    if password:
        u.set_password(password)
    u.save()
    _set_ops_roles_for_user(user=u, roles=roles or {})

    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="user.update",
        target=str(u.id),
        payload_json={"is_active": bool(is_active), "roles": roles, "password": "<changed>" if password else "<unchanged>"},
    )

    return JsonResponse({"ok": True, "server_time": timezone.now().isoformat(), "user": _user_payload(u)})


@login_required
@_require_superuser
@require_POST
def api_users_delete(request, user_id: int):
    User = get_user_model()
    try:
        u = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"ok": False, "errors": ["user not found"]}, status=404)

    # Prevent deleting the currently logged-in user to avoid lockout.
    if int(getattr(request.user, "id", -1)) == int(getattr(u, "id", -2)):
        return JsonResponse({"ok": False, "errors": ["cannot delete yourself"]}, status=400)

    username = str(getattr(u, "username", "") or "")
    u.delete()

    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="user.delete",
        target=str(user_id),
        payload_json={"username": username},
    )

    return JsonResponse(
        {
            "ok": True,
            "server_time": timezone.now().isoformat(),
            "deleted": {"user_id": user_id, "username": username},
        }
    )


_NON_EDITABLE_KEYS = {
    # node_id is computed per worker at startup.
    "SCHEDULER_NODE_ID",
}


_ENUM_SETTINGS: dict[str, list[str]] = {
    "SCHEDULER_DEPLOYMENT": ["local", "k8s"],
}


_BOOL_SETTINGS = {
    "SCHEDULER_LOG_ARCHIVE_ENABLED",
    "SCHEDULER_LOG_LOCAL_DELETE_AFTER_UPLOAD",
    "SCHEDULER_REBALANCE_ASSIGNED_ENABLED",
}


def _setting_schema(key: str) -> dict:
    if key in _NON_EDITABLE_KEYS:
        return {"editable": False, "input_type": "readonly", "enum_values": []}
    if key in _ENUM_SETTINGS:
        return {"editable": True, "input_type": "enum", "enum_values": _ENUM_SETTINGS[key]}
    if key in _BOOL_SETTINGS:
        return {"editable": True, "input_type": "bool", "enum_values": ["true", "false"]}
    return {"editable": True, "input_type": "text", "enum_values": []}


def _schema_from_help_row(row: SchedulerSettingHelp) -> dict:
    enum_values = []
    try:
        enum_values = list(row.enum_values_json or [])
    except Exception:
        enum_values = []
    return {
        "editable": bool(row.editable),
        "input_type": str(row.input_type or "text"),
        "enum_values": enum_values,
        "title": str(row.title or ""),
        "description": str(row.description or ""),
        "impact": str(row.impact or ""),
        "is_secret": bool(row.is_secret),
        "constraints": dict(row.constraints_json or {}),
        "examples": list(row.examples_json or []),
        "help_updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _get_schema_with_help(*, key: str) -> dict:
    if key in _NON_EDITABLE_KEYS:
        return {"editable": False, "input_type": "readonly", "enum_values": []}
    row = SchedulerSettingHelp.objects.filter(key=key).only(
        "key",
        "title",
        "description",
        "impact",
        "editable",
        "input_type",
        "enum_values_json",
        "constraints_json",
        "examples_json",
        "is_secret",
        "updated_at",
    ).first()
    if row:
        return _schema_from_help_row(row)
    return _setting_schema(key)


def _is_secret_key(key: str) -> bool:
    u = str(key or "").upper()
    if ("SECRET" in u) or ("TOKEN" in u) or ("PASSWORD" in u):
        return True
    # Keys that commonly embed credentials even if the name doesn't include PASSWORD/TOKEN/SECRET.
    if u.endswith("_REDIS_URL") or (u == "SCHEDULER_REDIS_URL"):
        return True
    if "ACCESS_KEY" in u:
        return True
    return False


def _mask_value(v) -> str:
    s = str(v or "")
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return ("*" * (len(s) - 4)) + s[-4:]


@login_required
@_require_ops_admin
def api_settings(request):
    ensure_setting_help_rows(apply_defaults=True)
    keys = list_all_scheduler_setting_keys(fresh=True)
    db_rows = {r.key: r for r in SchedulerSetting.objects.filter(key__in=keys).only("key", "value_json", "updated_at")}
    help_rows = {
        r.key: r
        for r in SchedulerSettingHelp.objects.filter(key__in=keys).only(
            "key",
            "title",
            "description",
            "impact",
            "editable",
            "input_type",
            "enum_values_json",
            "constraints_json",
            "examples_json",
            "is_secret",
            "updated_at",
        )
    }

    can_view_secrets = is_superuser(request.user)
    can_edit_help = is_superuser(request.user)

    items = []
    for k in keys:
        val, source = get_setting_with_source(key=k, default=None, fresh=True)
        db_row = db_rows.get(k)

        help_row = help_rows.get(k)
        schema = _schema_from_help_row(help_row) if help_row else _setting_schema(k)

        is_secret = _is_secret_key(k) or bool(schema.get("is_secret"))
        if is_secret and not can_view_secrets:
            display = _mask_value(val)
            raw_value = None
        else:
            display = val
            raw_value = val

        items.append(
            {
                "key": k,
                "source": source,
                "value": display,
                "raw_value": raw_value,
                "db_override": bool(db_row is not None),
                "db_updated_at": db_row.updated_at.isoformat() if (db_row and db_row.updated_at) else None,
                "is_secret": bool(is_secret),
                "editable": bool(schema.get("editable", True)),
                "input_type": str(schema.get("input_type", "text")),
                "enum_values": list(schema.get("enum_values") or []),
                "help_title": str(schema.get("title") or ""),
                "help_description": str(schema.get("description") or ""),
                "help_impact": str(schema.get("impact") or ""),
                "help_constraints": dict(schema.get("constraints") or {}),
                "help_examples": list(schema.get("examples") or []),
                "help_updated_at": schema.get("help_updated_at"),
            }
        )

    latest = (
        ConfigReloadRequest.objects.order_by("-id")
        .only("id", "status", "requested_at", "applied_at", "requested_by")
        .first()
    )

    return JsonResponse(
        {
            "ok": True,
            "server_time": timezone.now().isoformat(),
            "can_view_secrets": bool(can_view_secrets),
            "can_edit_help": bool(can_edit_help),
            "items": items,
            "latest_reload": (
                {
                    "id": latest.id,
                    "status": latest.status,
                    "requested_by": latest.requested_by,
                    "requested_at": latest.requested_at.isoformat() if latest.requested_at else None,
                    "applied_at": latest.applied_at.isoformat() if latest.applied_at else None,
                }
                if latest
                else None
            ),
        }
    )


@login_required
@_require_ops_admin
@require_POST
def api_settings_set(request):
    try:
        data = json.loads(request.body.decode("utf-8") if request.body else "{}")
    except Exception:
        data = {}

    key = str(data.get("key") or "").strip()
    raw_value = data.get("value")
    if not key or not key.startswith("SCHEDULER_"):
        return JsonResponse({"ok": False, "error": "invalid key"}, status=400)

    schema = _get_schema_with_help(key=key)
    if not schema.get("editable"):
        return JsonResponse({"ok": False, "error": "not editable"}, status=400)

    # Accept either JSON value (already parsed) or string input.
    value = raw_value
    if isinstance(raw_value, str):
        s = raw_value.strip()
        if s == "":
            value = ""
        else:
            try:
                value = json.loads(s)
            except Exception:
                value = s

    # Validate limited-choice settings server-side as well.
    if schema.get("input_type") == "bool":
        if not isinstance(value, bool):
            # tolerate 0/1
            if isinstance(value, (int, float)):
                value = bool(int(value))
            elif isinstance(value, str):
                value = value.strip().lower() not in {"", "0", "false", "no"}
            else:
                return JsonResponse({"ok": False, "error": "invalid bool"}, status=400)
    if schema.get("input_type") == "enum":
        allowed = set(schema.get("enum_values") or [])
        if str(value) not in allowed:
            return JsonResponse({"ok": False, "error": f"invalid enum (allowed={sorted(allowed)})"}, status=400)
        value = str(value)

    # Optional constraint validation (min/max)
    constraints = schema.get("constraints") or {}
    if isinstance(constraints, dict):
        min_v = constraints.get("min")
        max_v = constraints.get("max")
        if min_v is not None or max_v is not None:
            try:
                num = int(value)
            except Exception:
                return JsonResponse({"ok": False, "error": "invalid number"}, status=400)
            if min_v is not None and num < int(min_v):
                return JsonResponse({"ok": False, "error": f"too small (min={min_v})"}, status=400)
            if max_v is not None and num > int(max_v):
                return JsonResponse({"ok": False, "error": f"too large (max={max_v})"}, status=400)

    obj, _ = SchedulerSetting.objects.update_or_create(
        key=key,
        defaults={"value_json": {"value": value}},
    )

    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="setting.set",
        target=key,
        payload_json={"value": "<redacted>" if _is_secret_key(key) else value},
    )

    return JsonResponse({"ok": True, "key": obj.key, "updated_at": obj.updated_at.isoformat()})


@login_required
@_require_ops_admin
@require_POST
def api_settings_delete(request):
    try:
        data = json.loads(request.body.decode("utf-8") if request.body else "{}")
    except Exception:
        data = {}

    key = str(data.get("key") or "").strip()
    if not key or not key.startswith("SCHEDULER_"):
        return JsonResponse({"ok": False, "error": "invalid key"}, status=400)

    schema = _get_schema_with_help(key=key)
    if not schema.get("editable"):
        return JsonResponse({"ok": False, "error": "not editable"}, status=400)

    SchedulerSetting.objects.filter(key=key).delete()
    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="setting.delete",
        target=key,
        payload_json={},
    )
    return JsonResponse({"ok": True})


@login_required
@_require_ops_admin
@require_POST
def api_settings_apply(request):
    req = ConfigReloadRequest.objects.create(
        requested_by=getattr(request.user, "username", "") or "",
        status=ConfigReloadRequest.Status.PENDING,
    )
    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="setting.apply",
        target=str(req.id),
        payload_json={},
    )
    return JsonResponse({"ok": True, "request_id": req.id})


@login_required
@_require_superuser
@require_POST
def api_settings_help_set(request):
    ensure_setting_help_rows(apply_defaults=True)
    try:
        data = json.loads(request.body.decode("utf-8") if request.body else "{}")
    except Exception:
        data = {}

    key = str(data.get("key") or "").strip()
    if not key or not key.startswith("SCHEDULER_"):
        return JsonResponse({"ok": False, "error": "invalid key"}, status=400)
    if key in _NON_EDITABLE_KEYS:
        return JsonResponse({"ok": False, "error": "not editable"}, status=400)

    title = str(data.get("title") or "")
    description = str(data.get("description") or "")
    impact = str(data.get("impact") or "")
    input_type = str(data.get("input_type") or "text")
    editable = bool(data.get("editable", True))

    enum_values = data.get("enum_values")
    if isinstance(enum_values, str):
        # allow comma-separated
        enum_values = [s.strip() for s in enum_values.split(",") if s.strip()]
    if not isinstance(enum_values, list):
        enum_values = []

    constraints = data.get("constraints")
    if isinstance(constraints, str):
        s = constraints.strip()
        constraints = json.loads(s) if s else {}
    if not isinstance(constraints, dict):
        return JsonResponse({"ok": False, "error": "invalid constraints"}, status=400)

    examples = data.get("examples")
    if isinstance(examples, str):
        s = examples.strip()
        examples = json.loads(s) if s else []
    if not isinstance(examples, list):
        return JsonResponse({"ok": False, "error": "invalid examples"}, status=400)

    # Never allow unmarking obvious secrets.
    is_secret = _is_secret_key(key) or bool(data.get("is_secret", False))

    obj, _ = SchedulerSettingHelp.objects.update_or_create(
        key=key,
        defaults={
            "title": title,
            "description": description,
            "impact": impact,
            "editable": editable,
            "input_type": input_type,
            "enum_values_json": enum_values,
            "constraints_json": constraints,
            "examples_json": examples,
            "is_secret": is_secret,
        },
    )

    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="setting_help.set",
        target=key,
        payload_json={"title": title},
    )
    return JsonResponse({"ok": True, "key": obj.key, "updated_at": obj.updated_at.isoformat()})


def _get_log_max_bytes(request) -> int:
    try:
        raw = int(request.GET.get("max_bytes") or 0)
    except Exception:
        raw = 0
    # MVP: allow up to 512KiB in modal
    if raw <= 0:
        return 256 * 1024
    return max(1, min(raw, 512 * 1024))


def _allowed_log_url_prefixes() -> list[str]:
    prefixes: list[str] = []
    base = get_str(key="SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL", default="").strip()
    if not base:
        base = get_str(key="SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL", default="").strip()
    if base:
        prefixes.append(base.rstrip("/") + "/")
        prefixes.append(base.rstrip("/"))
    return prefixes


def _s3_ref_to_http_if_possible(ref: str) -> str | None:
    # Stored log_ref can be either http(s), local path, or s3://bucket/key (legacy/default when PUBLIC_BASE_URL was blank).
    # When PUBLIC_BASE_URL is blank, use S3_ENDPOINT_URL as the base so Ops UI can still fetch logs.
    if not ref.startswith("s3://"):
        return None
    raw = ref[len("s3://") :]
    if "/" not in raw:
        return None
    bucket, key = raw.split("/", 1)
    bucket = (bucket or "").strip()
    key = (key or "").lstrip("/")
    if not bucket or not key:
        return None
    base = get_str(key="SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL", default="").strip() or get_str(
        key="SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL", default=""
    ).strip()
    if not base:
        return None
    return f"{base.rstrip('/')}/{bucket}/{key}"


def _is_allowed_log_url(url: str) -> bool:
    prefixes = _allowed_log_url_prefixes()
    if not prefixes:
        return False
    return any(url.startswith(p) for p in prefixes)


def _read_tail_bytes_from_file(path: Path, max_bytes: int) -> tuple[bytes, bool]:
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_bytes(), False
    with path.open("rb") as f:
        f.seek(max(0, size - max_bytes))
        return f.read(max_bytes), True


def _read_tail_bytes_from_http(url: str, max_bytes: int) -> tuple[bytes, bool]:
    # Try HTTP Range to get the tail (MinIO supports this)
    req = urllib.request.Request(url, headers={"Range": f"bytes=-{max_bytes}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read(max_bytes + 1)
            return (data[:max_bytes], len(data) > max_bytes)
    except Exception:
        # Fallback: read from start with cap
        req2 = urllib.request.Request(url)
        with urllib.request.urlopen(req2, timeout=10) as resp:
            data = resp.read(max_bytes + 1)
            return (data[:max_bytes], len(data) > max_bytes)


def _read_log_bytes_from_ref(log_ref: str, max_bytes: int) -> tuple[bytes, bool, str, str | None]:
    ref = (log_ref or "").strip()
    if not ref:
        return b"", False, "none", "no log_ref"

    if ref.startswith("s3://"):
        converted = _s3_ref_to_http_if_possible(ref)
        if converted:
            ref = converted

    if ref.startswith("http://") or ref.startswith("https://"):
        if not _is_allowed_log_url(ref):
            return b"", False, "url", "log_ref URL not allowed"
        try:
            data, truncated = _read_tail_bytes_from_http(ref, max_bytes)
            return data, truncated, "url", None
        except Exception as e:
            return b"", False, "url", f"fetch failed: {type(e).__name__}"

    # Local file path
    p = Path(ref)
    try:
        if not p.is_absolute():
            base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
            p = (base / p).resolve()
        if not p.exists():
            return b"", False, "file", "file not found"
        data, truncated = _read_tail_bytes_from_file(p, max_bytes)
        return data, truncated, "file", None
    except Exception as e:
        return b"", False, "file", f"read failed: {type(e).__name__}"


def _stream_http_response(url: str):
    resp = urllib.request.urlopen(url, timeout=30)

    def gen():
        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass

    return resp, gen()


@login_required
@_require_app_operator
def api_job_run_log(request, run_id: int):
    max_bytes = _get_log_max_bytes(request)
    try:
        jr = JobRun.objects.only("id", "log_ref").get(id=run_id)
    except JobRun.DoesNotExist:
        return JsonResponse({"ok": False, "errors": ["not found"]}, status=404)

    data, truncated, source, err = _read_log_bytes_from_ref(jr.log_ref or "", max_bytes)
    text = data.decode("utf-8", errors="replace")
    return JsonResponse(
        {
            "ok": err is None,
            "errors": [err] if err else [],
            "run_id": jr.id,
            "log_ref": jr.log_ref,
            "source": source,
            "truncated": bool(truncated),
            "max_bytes": max_bytes,
            "log_text": text,
        }
    )


@login_required
@_require_app_operator
def api_job_run_log_download(request, run_id: int):
    try:
        jr = JobRun.objects.only("id", "log_ref").get(id=run_id)
    except JobRun.DoesNotExist:
        return HttpResponse("not found", status=404, content_type="text/plain; charset=utf-8")

    filename = f"jobrun_{jr.id}.log"

    ref = (jr.log_ref or "").strip()
    if not ref:
        return HttpResponse("no log_ref", status=400, content_type="text/plain; charset=utf-8")

    if ref.startswith("s3://"):
        converted = _s3_ref_to_http_if_possible(ref)
        if converted:
            ref = converted

    # Proxy allowed http(s) logs (e.g. MinIO) so browser always uses our download button.
    if ref.startswith("http://") or ref.startswith("https://"):
        if not _is_allowed_log_url(ref):
            return HttpResponse("log_ref URL not allowed", status=400, content_type="text/plain; charset=utf-8")
        try:
            upstream_resp, iterator = _stream_http_response(ref)
        except Exception as e:
            return HttpResponse(f"fetch failed: {type(e).__name__}", status=400, content_type="text/plain; charset=utf-8")

        out = StreamingHttpResponse(iterator, content_type="text/plain; charset=utf-8")
        # Pass through length when available (optional)
        try:
            cl = upstream_resp.headers.get("Content-Length")
            if cl:
                out["Content-Length"] = cl
        except Exception:
            pass
        out["Content-Disposition"] = f'attachment; filename="{filename}"'
        out["X-Log-Truncated"] = "0"
        return out

    # Local file path streaming
    p = Path(ref)
    try:
        if not p.is_absolute():
            base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
            p = (base / p).resolve()
        if not p.exists():
            return HttpResponse("file not found", status=400, content_type="text/plain; charset=utf-8")
        f = p.open("rb")
    except Exception as e:
        return HttpResponse(f"read failed: {type(e).__name__}", status=400, content_type="text/plain; charset=utf-8")

    out = FileResponse(f, content_type="text/plain; charset=utf-8", as_attachment=True, filename=filename)
    out["X-Log-Truncated"] = "0"
    return out


@login_required
@_require_app_operator
def api_dashboard(request):
    cfg = get_scheduler_config()
    leadership = get_cluster_leadership(cfg.redis_url)
    workers_list = list_workers(cfg.redis_url)
    active_workers = sum(1 for w in workers_list if w.heartbeat_ttl_seconds > 0)

    # Recent window for per-worker summaries.
    allowed_recent_minutes = {5, 15, 30, 60}
    recent_minutes = 15
    try:
        raw = request.GET.get("recent_minutes")
        if raw is not None and str(raw).strip() != "":
            cand = int(str(raw).strip())
            if cand in allowed_recent_minutes:
                recent_minutes = cand
    except Exception:
        recent_minutes = 15

    # Worker-level load summary (DB-backed counts + Redis worker registry)
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

    # Recent per-worker resource usage (best-effort; recorded at completion).
    # This is NOT "current" CPU/mem/IO, but it helps spot heavy workers.
    recent_since = timezone.now() - timedelta(minutes=int(recent_minutes))
    terminal_states = [
        JobRun.State.SUCCEEDED,
        JobRun.State.FAILED,
        JobRun.State.CANCELED,
        JobRun.State.SKIPPED,
        JobRun.State.TIMED_OUT,
    ]
    recent_by_worker: dict[str, dict[str, object]] = {}
    try:
        recent_qs = (
            JobRun.objects.filter(state__in=terminal_states)
            .exclude(finished_at=None)
            .filter(finished_at__gte=recent_since)
        )

        recent_rows = (
            recent_qs.exclude(assigned_worker_id="")
            .values("assigned_worker_id")
            .annotate(
                finished_count=Count("id"),
                cpu_seconds_total=Sum("resource_cpu_seconds_total"),
                io_read_bytes_total=Sum("resource_io_read_bytes"),
                io_write_bytes_total=Sum("resource_io_write_bytes"),
                peak_rss_bytes_max=Max("resource_peak_rss_bytes"),
            )
        )
        for row in recent_rows:
            wid = str(row.get("assigned_worker_id") or "")
            if not wid:
                continue
            recent_by_worker[wid] = {
                "finished_count": int(row.get("finished_count") or 0),
                "cpu_seconds_total": float(row.get("cpu_seconds_total") or 0.0),
                "io_read_bytes_total": int(row.get("io_read_bytes_total") or 0),
                "io_write_bytes_total": int(row.get("io_write_bytes_total") or 0),
                "peak_rss_bytes_max": int(row.get("peak_rss_bytes_max") or 0),
            }
    except Exception:
        recent_by_worker = {}

    # System Load (DB-backed): aggregate job-run performance over the same window.
    # - CPU cores avg ~= cpu_seconds_total / window_seconds
    # - IO read/write bps ~= bytes_total / window_seconds
    # - mem p95 uses resource_peak_rss_bytes distribution (best-effort).
    window_seconds = int(recent_minutes) * 60
    finished_counts: dict[str, int] = {s: 0 for s in terminal_states}
    load_payload: dict[str, object] = {
        "window_seconds": window_seconds,
        "cpu_cores_avg": None,
        "io_read_bps": None,
        "io_write_bps": None,
        "mem_p95_bytes": None,
        "finished_counts": {},
    }
    try:
        # Use the same recent_qs when available.
        recent_qs  # type: ignore[name-defined]
    except Exception:
        recent_qs = (
            JobRun.objects.filter(state__in=terminal_states)
            .exclude(finished_at=None)
            .filter(finished_at__gte=recent_since)
        )

    try:
        # Finished counts by state.
        for row in recent_qs.values("state").annotate(c=Count("id")):
            st = str(row.get("state") or "")
            if st:
                finished_counts[st] = int(row.get("c") or 0)

        agg = recent_qs.aggregate(
            cpu_seconds_total=Sum("resource_cpu_seconds_total"),
            io_read_bytes_total=Sum("resource_io_read_bytes"),
            io_write_bytes_total=Sum("resource_io_write_bytes"),
        )
        cpu_seconds_total = float(agg.get("cpu_seconds_total") or 0.0)
        io_read_bytes_total = float(agg.get("io_read_bytes_total") or 0.0)
        io_write_bytes_total = float(agg.get("io_write_bytes_total") or 0.0)

        cpu_cores_avg = (cpu_seconds_total / float(window_seconds)) if window_seconds > 0 else None
        io_read_bps = (io_read_bytes_total / float(window_seconds)) if window_seconds > 0 else None
        io_write_bps = (io_write_bytes_total / float(window_seconds)) if window_seconds > 0 else None

        # Mem p95 (best-effort; cap to avoid large payloads).
        peaks = list(
            recent_qs.exclude(resource_peak_rss_bytes=None)
            .order_by("-finished_at")
            .values_list("resource_peak_rss_bytes", flat=True)[:2000]
        )
        peaks_clean = [int(v) for v in peaks if v is not None]
        mem_p95 = None
        if peaks_clean:
            peaks_clean.sort()
            idx = int(round(0.95 * (len(peaks_clean) - 1)))
            idx = max(0, min(idx, len(peaks_clean) - 1))
            mem_p95 = int(peaks_clean[idx])

        load_payload = {
            "window_seconds": int(window_seconds),
            "cpu_cores_avg": float(cpu_cores_avg) if cpu_cores_avg is not None else None,
            "io_read_bps": float(io_read_bps) if io_read_bps is not None else None,
            "io_write_bps": float(io_write_bps) if io_write_bps is not None else None,
            "mem_p95_bytes": mem_p95,
            "finished_counts": {k: int(v) for k, v in finished_counts.items()},
        }
    except Exception:
        pass

    # System Load sparklines (DB-backed): past 30 minutes, 1-minute buckets.
    # Best-effort: we group completed job runs by finished_at minute.
    try:
        spark_minutes = 30
        bucket_seconds = 60
        end_bucket = timezone.now().replace(second=0, microsecond=0)
        start_bucket = end_bucket - timedelta(minutes=spark_minutes - 1)
        spark_until = end_bucket + timedelta(minutes=1)

        spark_qs = (
            JobRun.objects.filter(state__in=terminal_states)
            .exclude(finished_at=None)
            .filter(finished_at__gte=start_bucket, finished_at__lt=spark_until)
        )

        cpu_by_bucket: dict[object, float] = {}
        for row in (
            spark_qs.annotate(bucket=TruncMinute("finished_at"))
            .values("bucket")
            .annotate(cpu_seconds_total=Sum("resource_cpu_seconds_total"))
        ):
            b = row.get("bucket")
            if b is None:
                continue
            cpu_by_bucket[b] = float(row.get("cpu_seconds_total") or 0.0)

        io_by_bucket: dict[object, tuple[float, float]] = {}
        for row in (
            spark_qs.annotate(bucket=TruncMinute("finished_at"))
            .values("bucket")
            .annotate(
                io_read_bytes_total=Sum("resource_io_read_bytes"),
                io_write_bytes_total=Sum("resource_io_write_bytes"),
            )
        ):
            b = row.get("bucket")
            if b is None:
                continue
            io_by_bucket[b] = (
                float(row.get("io_read_bytes_total") or 0.0),
                float(row.get("io_write_bytes_total") or 0.0),
            )

        mem_values_by_bucket: dict[object, list[int]] = {}
        mem_rows = list(
            spark_qs.exclude(resource_peak_rss_bytes=None)
            .annotate(bucket=TruncMinute("finished_at"))
            .values_list("bucket", "resource_peak_rss_bytes")[:50000]
        )
        for b, v in mem_rows:
            if b is None or v is None:
                continue
            mem_values_by_bucket.setdefault(b, []).append(int(v))

        def _p95_int(values: list[int]) -> int | None:
            if not values:
                return None
            values.sort()
            idx = int(round(0.95 * (len(values) - 1)))
            idx = max(0, min(idx, len(values) - 1))
            return int(values[idx])

        buckets = [start_bucket + timedelta(minutes=i) for i in range(spark_minutes)]
        cpu_series = []
        mem_series = []
        io_read_series = []
        io_write_series = []
        for b in buckets:
            cpu_seconds = float(cpu_by_bucket.get(b, 0.0))
            cpu_cores = cpu_seconds / float(bucket_seconds) if bucket_seconds > 0 else None
            cpu_series.append([b.isoformat(), float(cpu_cores) if cpu_cores is not None else None])

            mem_p95_b = _p95_int(mem_values_by_bucket.get(b, []))
            mem_series.append([b.isoformat(), int(mem_p95_b) if mem_p95_b is not None else None])

            io_read_bytes, io_write_bytes = io_by_bucket.get(b, (0.0, 0.0))
            io_read_bps = io_read_bytes / float(bucket_seconds) if bucket_seconds > 0 else None
            io_write_bps = io_write_bytes / float(bucket_seconds) if bucket_seconds > 0 else None
            io_read_series.append([b.isoformat(), float(io_read_bps) if io_read_bps is not None else None])
            io_write_series.append([b.isoformat(), float(io_write_bps) if io_write_bps is not None else None])

        load_payload["sparklines"] = {
            "minutes": int(spark_minutes),
            "bucket_seconds": int(bucket_seconds),
            "cpu_cores": cpu_series,
            "mem_p95_bytes": mem_series,
            "io_read_bps": io_read_series,
            "io_write_bps": io_write_series,
        }
    except Exception:
        # Keep dashboard usable even if sparkline aggregation fails.
        pass

    def _role_weight_for(w):
        if w.is_leader:
            return max(1, int(cfg.assign_weight_leader))
        if w.is_subleader:
            return max(1, int(cfg.assign_weight_subleader))
        return max(1, int(cfg.assign_weight_worker))

    def _effective_load_for(worker_id: str) -> int:
        return int(assigned_counts.get(worker_id, 0)) + int(running_counts.get(worker_id, 0)) * max(
            1, int(cfg.assign_running_load_weight)
        )

    worker_rows = []
    for w in workers_list:
        wid = str(w.worker_id or "")
        if not wid:
            continue
        assigned = int(assigned_counts.get(wid, 0))
        running = int(running_counts.get(wid, 0))
        role_weight = int(_role_weight_for(w))
        effective = int(_effective_load_for(wid))
        normalized = round(effective / float(role_weight), 3) if role_weight > 0 else float(effective)
        recent = recent_by_worker.get(wid) or {}
        worker_rows.append(
            {
                "worker_id": wid,
                "node_id": w.node_id,
                "is_leader": bool(w.is_leader),
                "is_subleader": bool(w.is_subleader),
                "heartbeat_ttl_seconds": w.heartbeat_ttl_seconds,
                "assigned_job_count": assigned,
                "running_job_count": running,
                "role_weight": role_weight,
                "effective_load": effective,
                "normalized_load": normalized,
                "recent": {
                    "window_seconds": int(recent_minutes) * 60,
                    "finished_count": recent.get("finished_count"),
                    "cpu_seconds_total": recent.get("cpu_seconds_total"),
                    "io_read_bytes_total": recent.get("io_read_bytes_total"),
                    "io_write_bytes_total": recent.get("io_write_bytes_total"),
                    "peak_rss_bytes_max": recent.get("peak_rss_bytes_max"),
                },
            }
        )
    worker_rows.sort(key=lambda r: (float(r.get("normalized_load") or 0), int(r.get("running_job_count") or 0)), reverse=True)

    totals = {
        "active_workers": int(active_workers),
        "assigned_jobs": int(sum(int(v) for v in assigned_counts.values())),
        "running_jobs": int(sum(int(v) for v in running_counts.values())),
        "effective_load": int(sum(int(r.get("effective_load") or 0) for r in worker_rows)),
    }

    # --- Connectivity / health summary (best-effort) ---
    def _ok_payload(*, ok: bool, detail: str | None = None, enabled: bool = True) -> dict:
        return {"enabled": bool(enabled), "ok": bool(ok), "detail": detail}

    # Redis
    redis_health = _ok_payload(ok=False, detail="unknown")
    try:
        import redis  # type: ignore

        r = redis.from_url(cfg.redis_url)
        r.ping()
        redis_health = _ok_payload(ok=True)
    except Exception as e:
        redis_health = _ok_payload(ok=False, detail=type(e).__name__)

    # Prometheus (only required if configured)
    prom = _prometheus_summary_cached()
    alerts = _prometheus_alerts_cached()
    prom_enabled = bool(prom.get("enabled"))
    prom_ok = bool(prom.get("ok")) if prom_enabled else False
    prom_health = _ok_payload(ok=prom_ok, detail=str(prom.get("error") or "") or None, enabled=prom_enabled)

    # Object storage / S3 (only required if log archival enabled)
    log_archive_enabled = bool(get_str(key="SCHEDULER_LOG_ARCHIVE_ENABLED", default="0", fresh=True).strip() not in {"0", "false", "False"})
    obj_health = _ok_payload(ok=False, detail="disabled", enabled=log_archive_enabled)
    if log_archive_enabled:
        try:
            import boto3  # type: ignore

            endpoint_url = get_str(key="SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL", default="", fresh=True).strip()
            region = get_str(key="SCHEDULER_LOG_ARCHIVE_S3_REGION", default="us-east-1", fresh=True).strip() or "us-east-1"
            access_key_id = get_str(key="SCHEDULER_LOG_ARCHIVE_ACCESS_KEY_ID", default="", fresh=True).strip()
            secret_access_key = get_str(key="SCHEDULER_LOG_ARCHIVE_SECRET_ACCESS_KEY", default="", fresh=True).strip()
            bucket = get_str(key="SCHEDULER_LOG_ARCHIVE_BUCKET", default="", fresh=True).strip()
            if not endpoint_url or not bucket:
                obj_health = _ok_payload(ok=False, detail="not configured", enabled=True)
            else:
                client = boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    region_name=region,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                )
                client.head_bucket(Bucket=bucket)
                obj_health = _ok_payload(ok=True, enabled=True)
        except Exception as e:
            obj_health = _ok_payload(ok=False, detail=type(e).__name__, enabled=True)

    # K8s API (required only when deployment=k8s)
    deployment = get_str(key="SCHEDULER_DEPLOYMENT", default="local", fresh=True).strip() or "local"
    k8s_required = deployment == "k8s"
    k8s_health = _ok_payload(ok=False, detail="n/a", enabled=k8s_required)
    if k8s_required:
        try:
            import os
            import ssl

            host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "").strip() or "443"
            if not host:
                k8s_health = _ok_payload(ok=False, detail="KUBERNETES_SERVICE_HOST missing", enabled=True)
            else:
                url = f"https://{host}:{port}/version"
                headers = {"Accept": "application/json"}
                token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
                ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
                if os.path.exists(token_path):
                    try:
                        with open(token_path, "r", encoding="utf-8") as f:
                            token = f.read().strip()
                        if token:
                            headers["Authorization"] = f"Bearer {token}"
                    except Exception:
                        pass
                ctx = ssl.create_default_context(cafile=ca_path) if os.path.exists(ca_path) else ssl.create_default_context()
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=2.0, context=ctx) as resp:
                    _ = resp.read(1)
                k8s_health = _ok_payload(ok=True, enabled=True)
        except Exception as e:
            k8s_health = _ok_payload(ok=False, detail=type(e).__name__, enabled=True)

    # Alertmanager (optional; used for silencing alerts from Ops UI)
    am_url = get_str(key="SCHEDULER_ALERTMANAGER_URL", default="", fresh=True).strip().rstrip("/")
    am_enabled = bool(am_url)
    am_health = _ok_payload(ok=False, detail="not configured", enabled=am_enabled)
    if am_enabled:
        try:
            url = f"{am_url}/api/v2/status"
            req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                _ = resp.read(1)
            am_health = _ok_payload(ok=True, enabled=True)
        except Exception as e:
            am_health = _ok_payload(ok=False, detail=type(e).__name__, enabled=True)

    # Min online workers (alert threshold / minimum workers health)
    try:
        min_online_workers = int(get_str(key="SCHEDULER_MIN_ONLINE_WORKERS", default="1", fresh=True).strip() or "1")
        if min_online_workers < 0:
            min_online_workers = 0
    except Exception:
        min_online_workers = 1

    min_workers_ok = bool(active_workers >= int(min_online_workers))

    def _required_ok(h: dict) -> bool:
        if not bool(h.get("enabled")):
            return True
        return bool(h.get("ok"))

    online = all(
        [
            _required_ok(redis_health),
            _required_ok(prom_health),
            _required_ok(obj_health),
            _required_ok(k8s_health),
            _required_ok(am_health),
            bool(min_workers_ok),
        ]
    )

    offline_since = _HEALTH_CACHE.get("offline_since")
    if online:
        _HEALTH_CACHE["offline_since"] = None
        offline_since = None
    else:
        if offline_since is None:
            offline_since = timezone.now().isoformat()
            _HEALTH_CACHE["offline_since"] = offline_since
    now = timezone.now()

    # Service links (used by UI to link from health chips)
    prom_base_url = get_str(key="SCHEDULER_PROMETHEUS_URL", default="", fresh=True).strip().rstrip("/")
    obj_public_base_url = get_str(key="SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL", default="", fresh=True).strip().rstrip("/")
    obj_endpoint_url = get_str(key="SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL", default="", fresh=True).strip().rstrip("/")
    obj_effective_url = obj_public_base_url or obj_endpoint_url
    return JsonResponse(
        {
            "server_time": now.isoformat(),
            "active_workers": active_workers,
            "leadership": {
                "leader_worker_id": leadership.leader_worker_id,
                "cluster_epoch": leadership.cluster_epoch,
            },
            "workers": {
                "totals": totals,
                "recent_minutes": int(recent_minutes),
                "top": worker_rows[:10],
            },
            "system_load": load_payload,
            "health": {
                "online": bool(online),
                "offline_since": offline_since,
                "min_online_workers": int(min_online_workers),
                "min_workers": {
                    "enabled": True,
                    "ok": bool(min_workers_ok),
                    "detail": f"{active_workers}/{int(min_online_workers)}",
                },
                "redis": redis_health,
                "prometheus": prom_health,
                "object_storage": obj_health,
                "k8s_api": k8s_health,
                "alertmanager": am_health,
            },
            "links": {
                "prometheus_url": prom_base_url or None,
                "alertmanager_url": am_url or None,
                "object_storage_url": obj_effective_url or None,
            },
            "prometheus": prom,
            "alerts": alerts,
        }
    )


@login_required
@require_POST
@_require_ops_admin
def api_alertmanager_silence_create(request):
    """Create an Alertmanager silence for a specific alert.

    Requires ops-admin.
    Body: {"matchers": [{"name":...,"value":...,"isRegex":false},...], "duration_minutes": 60}
    """

    am_url = get_str(key="SCHEDULER_ALERTMANAGER_URL", default="", fresh=True).strip().rstrip("/")
    if not am_url:
        return JsonResponse({"ok": False, "error": "Alertmanager not configured"}, status=400)

    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        payload = {}

    matchers = payload.get("matchers") or []
    if not isinstance(matchers, list) or not matchers:
        return JsonResponse({"ok": False, "error": "matchers required"}, status=400)

    duration_minutes = payload.get("duration_minutes")
    try:
        dur_min = int(duration_minutes) if duration_minutes is not None else 60
        dur_min = max(1, min(60 * 24 * 7, dur_min))
    except Exception:
        dur_min = 60

    # Validate matchers
    am_matchers = []
    for m in matchers:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        value = str(m.get("value") or "").strip()
        is_regex = bool(m.get("isRegex"))
        if not name or value == "":
            continue
        am_matchers.append({"name": name, "value": value, "isRegex": is_regex})
    if not am_matchers:
        return JsonResponse({"ok": False, "error": "valid matchers required"}, status=400)

    now = timezone.now()
    ends = now + timedelta(minutes=dur_min)
    created_by = str(getattr(request.user, "username", "") or "")
    comment = f"scheduler_ops silence {dur_min}m"

    silence = {
        "matchers": am_matchers,
        "startsAt": now.isoformat(),
        "endsAt": ends.isoformat(),
        "createdBy": created_by,
        "comment": comment,
    }

    try:
        url = f"{am_url}/api/v2/silences"
        req = urllib.request.Request(
            url,
            data=json.dumps(silence).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            raw = resp.read()
        out = json.loads(raw.decode("utf-8")) if raw else {}
        silence_id = str(out.get("silenceID") or "")
        AdminActionLog.objects.create(
            actor=str(getattr(request.user, "username", "") or ""),
            action="alertmanager_silence_create",
            target=silence_id,
            payload_json={"matchers": am_matchers, "duration_minutes": dur_min},
        )
        return JsonResponse({"ok": True, "silence_id": silence_id})
    except Exception as e:
        return JsonResponse({"ok": False, "error": type(e).__name__}, status=502)


@login_required
@_require_ops_admin
def api_workers(request):
    cfg = get_scheduler_config()
    leadership = get_cluster_leadership(cfg.redis_url)
    workers_list = list_workers(cfg.redis_url)

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

    running_by_worker: dict[str, dict] = {}
    running_qs = (
        JobRun.objects.select_related("job_definition")
        .filter(state=JobRun.State.RUNNING)
        .exclude(assigned_worker_id="")
        .only(
            "id",
            "state",
            "attempt",
            "scheduled_for",
            "assigned_worker_id",
            "started_at",
            "leader_epoch",
            "job_definition__id",
            "job_definition__name",
            "job_definition__command_name",
        )
        .order_by("-started_at", "-id")
    )
    for r in running_qs:
        wid = r.assigned_worker_id
        if not wid or wid in running_by_worker:
            continue
        running_by_worker[wid] = {
            "id": r.id,
            "state": r.state,
            "attempt": r.attempt,
            "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
            "assigned_worker_id": r.assigned_worker_id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "leader_epoch": r.leader_epoch,
            "job_definition_id": r.job_definition_id,
            "job_definition_name": r.job_definition.name if r.job_definition_id else None,
            "command_name": r.job_definition.command_name if r.job_definition_id else None,
        }

    now = timezone.now()
    def _role_weight_for(w):
        if w.is_leader:
            return max(1, int(cfg.assign_weight_leader))
        if w.is_subleader:
            return max(1, int(cfg.assign_weight_subleader))
        return max(1, int(cfg.assign_weight_worker))

    def _effective_load_for(worker_id: str) -> int:
        return int(assigned_counts.get(worker_id, 0)) + int(running_counts.get(worker_id, 0)) * max(
            1, int(cfg.assign_running_load_weight)
        )

    return JsonResponse(
        {
            "server_time": now.isoformat(),
            "leadership": {
                "leader_worker_id": leadership.leader_worker_id,
                "cluster_epoch": leadership.cluster_epoch,
            },
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "node_id": w.node_id,
                    "grpc_host": w.grpc_host,
                    "grpc_port": w.grpc_port,
                    "is_leader": w.is_leader,
                    "is_subleader": w.is_subleader,
                    "last_seen": w.last_seen,
                    "heartbeat_ttl_seconds": w.heartbeat_ttl_seconds,
                    "assigned_job_count": assigned_counts.get(w.worker_id, 0),
                    "running_job_count": running_counts.get(w.worker_id, 0),
                    "role_weight": _role_weight_for(w),
                    "effective_load": _effective_load_for(w.worker_id),
                    "normalized_load": round(_effective_load_for(w.worker_id) / float(_role_weight_for(w)), 3),
                    "running_job_run": running_by_worker.get(w.worker_id),
                }
                for w in workers_list
            ],
        }
    )


@login_required
@_require_app_operator
def api_job_run_detail(request, run_id: int):
    now = timezone.now()
    try:
        r = (
            JobRun.objects.select_related("job_definition")
            .only(
                "id",
                "state",
                "attempt",
                "scheduled_for",
                "assigned_worker_id",
                "assigned_at",
                "leader_epoch",
                "started_at",
                "finished_at",
                "exit_code",
                "error_summary",
                "log_ref",
                "job_definition__id",
                "job_definition__name",
                "job_definition__command_name",
                "job_definition__timeout_seconds",
                "job_definition__max_retries",
                "job_definition__default_args_json",
                "job_definition__schedule",
            )
            .get(id=run_id)
        )
    except JobRun.DoesNotExist:
        return JsonResponse({"ok": False, "errors": ["job_run not found"], "server_time": now.isoformat()}, status=404)

    return JsonResponse(
        {
            "ok": True,
            "server_time": now.isoformat(),
            "run": {
                "id": r.id,
                "state": r.state,
                "attempt": r.attempt,
                "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
                "assigned_worker_id": r.assigned_worker_id,
                "assigned_at": r.assigned_at.isoformat() if r.assigned_at else None,
                "leader_epoch": r.leader_epoch,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "exit_code": r.exit_code,
                "error_summary": r.error_summary,
                "log_ref": r.log_ref,
                "job_definition": {
                    "id": r.job_definition_id,
                    "name": r.job_definition.name if r.job_definition_id else None,
                    "command_name": r.job_definition.command_name if r.job_definition_id else None,
                    "timeout_seconds": r.job_definition.timeout_seconds if r.job_definition_id else None,
                    "max_retries": r.job_definition.max_retries if r.job_definition_id else None,
                    "default_args_json": r.job_definition.default_args_json if r.job_definition_id else None,
                    "schedule": r.job_definition.schedule if r.job_definition_id else None,
                },
            },
        }
    )


@login_required
@_require_app_operator
@require_POST
def api_job_run_rerun(request, run_id: int):
    now = timezone.now()
    try:
        r = (
            JobRun.objects.select_related("job_definition")
            .only(
                "id",
                "state",
                "scheduled_for",
                "job_definition__id",
                "job_definition__name",
            )
            .get(id=run_id)
        )
    except JobRun.DoesNotExist:
        return JsonResponse({"ok": False, "errors": ["job_run not found"], "server_time": now.isoformat()}, status=404)

    if r.state != JobRun.State.SKIPPED:
        return JsonResponse(
            {
                "ok": False,
                "errors": ["only SKIPPED job_run can be re-run"],
                "server_time": now.isoformat(),
            },
            status=400,
        )

    # Create a new JobRun scheduled for 'now' (with seconds/micros) so it is eligible
    # for assignment immediately and won't collide with minute-slot schedules.
    scheduled_for = timezone.now()
    new_run: JobRun | None = None
    with transaction.atomic():
        for _ in range(10):
            try:
                new_run = JobRun.objects.create(
                    job_definition_id=r.job_definition_id,
                    scheduled_for=scheduled_for,
                    state=JobRun.State.PENDING,
                    attempt=0,
                    assigned_worker_id="",
                    error_summary="",
                    log_ref="",
                    idempotency_key="",
                )
                break
            except IntegrityError:
                scheduled_for = scheduled_for + timedelta(microseconds=1)

    if not new_run:
        return JsonResponse(
            {
                "ok": False,
                "errors": ["failed to create new job_run"],
                "server_time": now.isoformat(),
            },
            status=409,
        )

    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="job_run.rerun",
        target=str(run_id),
        payload_json={
            "from_run_id": int(run_id),
            "new_run_id": int(new_run.id),
            "job_definition_id": int(r.job_definition_id),
            "job_definition_name": (r.job_definition.name if r.job_definition_id else None),
            "from_state": str(r.state),
            "from_scheduled_for": (r.scheduled_for.isoformat() if r.scheduled_for else None),
            "new_scheduled_for": (new_run.scheduled_for.isoformat() if new_run.scheduled_for else None),
        },
    )

    return JsonResponse(
        {
            "ok": True,
            "server_time": now.isoformat(),
            "new_run": {
                "id": new_run.id,
                "state": new_run.state,
                "scheduled_for": new_run.scheduled_for.isoformat() if new_run.scheduled_for else None,
                "job_definition_id": new_run.job_definition_id,
                "job_definition_name": (r.job_definition.name if r.job_definition_id else None),
                "job_definition_type": (r.job_definition.type if r.job_definition_id else None),
                "job_definition_schedule": (r.job_definition.schedule if r.job_definition_id else None),
                "attempt": new_run.attempt,
                "assigned_worker_id": new_run.assigned_worker_id,
                "started_at": new_run.started_at.isoformat() if new_run.started_at else None,
                "finished_at": new_run.finished_at.isoformat() if new_run.finished_at else None,
                "error_summary": new_run.error_summary,
                "log_ref": new_run.log_ref,
                "resource_cpu_seconds_total": new_run.resource_cpu_seconds_total,
                "resource_peak_rss_bytes": new_run.resource_peak_rss_bytes,
                "resource_io_read_bytes": new_run.resource_io_read_bytes,
                "resource_io_write_bytes": new_run.resource_io_write_bytes,
            },
        }
    )


@login_required
@_require_app_operator
def api_jobs(request):
    now = timezone.now()
    job_defs = JobDefinition.objects.order_by("id").only(
        "id",
        "name",
        "enabled",
        "type",
        "command_name",
        "timeout_seconds",
        "max_retries",
        "updated_at",
    )
    return JsonResponse(
        {
            "server_time": now.isoformat(),
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "type": j.type,
                    "command_name": j.command_name,
                    "default_args_json": j.default_args_json,
                    "schedule": j.schedule,
                    "timeout_seconds": j.timeout_seconds,
                    "max_retries": j.max_retries,
                    "updated_at": j.updated_at.isoformat() if j.updated_at else None,
                }
                for j in job_defs
            ],
        }
    )


def _parse_json_body(request):
    try:
        raw = request.body.decode("utf-8") if request.body else "{}"
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _job_payload(j: JobDefinition) -> dict:
    return {
        "id": j.id,
        "name": j.name,
        "enabled": j.enabled,
        "type": j.type,
        "command_name": j.command_name,
        "default_args_json": j.default_args_json,
        "schedule": j.schedule,
        "timeout_seconds": j.timeout_seconds,
        "max_retries": j.max_retries,
        "updated_at": j.updated_at.isoformat() if j.updated_at else None,
    }


def _make_copy_name(original: str) -> str:
    base = str(original or "").strip() or "Untitled"

    # Prefer Japanese suffix since Ops UI is Japanese.
    candidate = f"{base} コピー"
    if not JobDefinition.objects.filter(name=candidate).exists():
        return candidate

    for i in range(1, 1000):
        candidate = f"{base} コピー（{i}）"
        if not JobDefinition.objects.filter(name=candidate).exists():
            return candidate
    # Fallback: should be extremely rare.
    return f"{base} コピー（{int(time.time())}）"


def _validate_and_build_fields(data: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []

    name = str(data.get("name") or "").strip()
    if not name:
        errors.append("name is required")

    enabled = bool(data.get("enabled"))

    raw_type = str(data.get("type") or "").strip()
    if raw_type not in {JobDefinition.JobType.TIME, JobDefinition.JobType.EVENT}:
        errors.append("type must be 'time' or 'event'")

    command_name = str(data.get("command_name") or "").strip()
    if not command_name:
        errors.append("command_name is required")
    if command_name and any(ch.isspace() for ch in command_name):
        errors.append("command_name must be a single management command name (no args). Use default_args_json for parameters")

    timeout_seconds = int(data.get("timeout_seconds") or 0)
    max_retries = int(data.get("max_retries") or 0)
    if timeout_seconds < 0:
        errors.append("timeout_seconds must be >= 0")
    if max_retries < 0:
        errors.append("max_retries must be >= 0")

    default_args_json = data.get("default_args_json")
    if default_args_json is None:
        default_args_json = {}
    if not isinstance(default_args_json, dict):
        errors.append("default_args_json must be an object")
        default_args_json = {}

    def _parse_hhmm(raw: str) -> str | None:
        s = str(raw or "").strip()
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
        return f"{hh:02d}:{mm:02d}"

    schedule: dict = {}
    if raw_type == JobDefinition.JobType.TIME:
        incoming_schedule = data.get("schedule")
        if isinstance(incoming_schedule, dict) and incoming_schedule.get("kind"):
            kind = str(incoming_schedule.get("kind") or "").strip()
            if kind == "every_n_minutes":
                try:
                    n = int(incoming_schedule.get("n") or 0)
                except Exception:
                    n = 0
                if n <= 0:
                    errors.append("schedule.n must be > 0 for kind=every_n_minutes")
                else:
                    schedule = {"kind": "every_n_minutes", "n": n}
            elif kind == "hourly":
                raw_minute = incoming_schedule.get("minute", 0)
                try:
                    minute = int(raw_minute)
                except Exception:
                    minute = -1
                if not (0 <= minute <= 59):
                    errors.append("schedule.minute must be 0..59 for kind=hourly")
                else:
                    schedule = {"kind": "hourly", "minute": minute}
            elif kind in {"daily", "weekdays"}:
                t = _parse_hhmm(str(incoming_schedule.get("time") or ""))
                if not t:
                    errors.append("schedule.time must be HH:MM for kind=daily/weekdays")
                else:
                    schedule = {"kind": kind, "time": t}
            elif kind == "weekly":
                t = _parse_hhmm(str(incoming_schedule.get("time") or ""))
                raw_weekday = incoming_schedule.get("weekday", -1)
                try:
                    weekday = int(raw_weekday)
                except Exception:
                    weekday = -1
                if not t:
                    errors.append("schedule.time must be HH:MM for kind=weekly")
                if not (0 <= weekday <= 6):
                    errors.append("schedule.weekday must be 0..6 for kind=weekly (0=Mon)")
                if t and (0 <= weekday <= 6):
                    schedule = {"kind": "weekly", "weekday": weekday, "time": t}
            else:
                errors.append("schedule.kind must be one of every_n_minutes/hourly/daily/weekdays/weekly")
        else:
            # Legacy support (or older UI): every_n_minutes
            try:
                every_n_minutes = int(data.get("every_n_minutes") or 0)
            except Exception:
                every_n_minutes = 0
            if every_n_minutes <= 0:
                # Also accept legacy schedule dict: {every_n_minutes:N}
                if isinstance(incoming_schedule, dict) and incoming_schedule.get("every_n_minutes"):
                    try:
                        every_n_minutes = int(incoming_schedule.get("every_n_minutes") or 0)
                    except Exception:
                        every_n_minutes = 0
                if every_n_minutes <= 0:
                    errors.append("schedule is required for time jobs")
                else:
                    schedule = {"every_n_minutes": every_n_minutes}
            else:
                schedule = {"every_n_minutes": every_n_minutes}

    fields = {
        "name": name,
        "enabled": enabled,
        "type": raw_type,
        "command_name": command_name,
        "default_args_json": default_args_json,
        "schedule": schedule,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
    }
    return fields, errors


@login_required
@_require_app_operator
@require_POST
def api_jobs_create(request):
    data = _parse_json_body(request)
    fields, errors = _validate_and_build_fields(data)
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    j = JobDefinition.objects.create(**fields)
    return JsonResponse({"ok": True, "job": _job_payload(j), "server_time": timezone.now().isoformat()})


@login_required
@_require_app_operator
@require_POST
def api_jobs_update(request, job_id: int):
    data = _parse_json_body(request)
    fields, errors = _validate_and_build_fields(data)
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    try:
        j = JobDefinition.objects.get(id=job_id)
    except JobDefinition.DoesNotExist:
        return JsonResponse({"ok": False, "errors": ["job not found"]}, status=404)

    for k, v in fields.items():
        setattr(j, k, v)
    j.save()

    return JsonResponse({"ok": True, "job": _job_payload(j), "server_time": timezone.now().isoformat()})


@login_required
@_require_app_operator
@require_POST
def api_jobs_duplicate(request, job_id: int):
    try:
        src = JobDefinition.objects.get(id=job_id)
    except JobDefinition.DoesNotExist:
        return JsonResponse({"ok": False, "errors": ["job not found"]}, status=404)

    new_job = JobDefinition.objects.create(
        name=_make_copy_name(src.name),
        enabled=False,
        type=src.type,
        command_name=src.command_name,
        default_args_json=src.default_args_json,
        schedule=src.schedule,
        timeout_seconds=src.timeout_seconds,
        max_retries=src.max_retries,
        retry_backoff_seconds=src.retry_backoff_seconds,
        concurrency_policy=src.concurrency_policy,
    )

    return JsonResponse({"ok": True, "job": _job_payload(new_job), "server_time": timezone.now().isoformat()})


@login_required
@_require_app_operator
@require_POST
def api_jobs_delete(request, job_id: int):
    try:
        j = JobDefinition.objects.get(id=job_id)
    except JobDefinition.DoesNotExist:
        return JsonResponse({"ok": False, "errors": ["job not found"]}, status=404)

    # JobRun has FK to JobDefinition with CASCADE; deleting the job deletes its runs.
    run_count = int(JobRun.objects.filter(job_definition_id=j.id).count())
    job_name = str(j.name)
    j.delete()

    AdminActionLog.objects.create(
        actor=getattr(request.user, "username", "") or "",
        action="job.delete",
        target=str(job_id),
        payload_json={"name": job_name, "deleted_job_runs": run_count},
    )

    return JsonResponse(
        {
            "ok": True,
            "server_time": timezone.now().isoformat(),
            "deleted": {"job_id": job_id, "name": job_name, "deleted_job_runs": run_count},
        }
    )


@login_required
@_require_app_operator
def api_job_runs(request):
    now = timezone.now()
    runs = (
        JobRun.objects.select_related("job_definition")
        .order_by("-id")
        .only(
            "id",
            "state",
            "attempt",
            "scheduled_for",
            "assigned_worker_id",
            "started_at",
            "finished_at",
            "error_summary",
            "log_ref",
            "resource_cpu_seconds_total",
            "resource_peak_rss_bytes",
            "resource_io_read_bytes",
            "resource_io_write_bytes",
            "job_definition__id",
            "job_definition__name",
            "job_definition__type",
            "job_definition__schedule",
        )[:500]
    )
    return JsonResponse(
        {
            "server_time": now.isoformat(),
            "runs": [
                {
                    "id": r.id,
                    "job_definition_id": r.job_definition_id,
                    "job_definition_name": r.job_definition.name if r.job_definition_id else None,
                    "job_definition_type": r.job_definition.type if r.job_definition_id else None,
                    "job_definition_schedule": r.job_definition.schedule if r.job_definition_id else None,
                    "state": r.state,
                    "attempt": r.attempt,
                    "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
                    "assigned_worker_id": r.assigned_worker_id,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "error_summary": r.error_summary,
                    "log_ref": r.log_ref,
                    "resource_cpu_seconds_total": r.resource_cpu_seconds_total,
                    "resource_peak_rss_bytes": r.resource_peak_rss_bytes,
                    "resource_io_read_bytes": r.resource_io_read_bytes,
                    "resource_io_write_bytes": r.resource_io_write_bytes,
                }
                for r in runs
            ],
        }
    )
