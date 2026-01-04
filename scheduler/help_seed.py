from __future__ import annotations

from django.db.utils import OperationalError, ProgrammingError


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


def ensure_setting_help_rows(*, apply_defaults: bool = True) -> None:
    """Ensure SchedulerSettingHelp rows exist for all known SCHEDULER_* keys.

    This removes the need for manual loaddata/seed steps in fresh DBs.
    Safe to call on startup; it no-ops if DB isn't ready.
    """

    try:
        from scheduler.conf import list_all_scheduler_setting_keys
        from scheduler.management.commands.scheduler_seed_setting_help import DEFAULT_HELP
        from scheduler.models import SchedulerSettingHelp

        keys = list_all_scheduler_setting_keys(fresh=True)
        existing = set(SchedulerSettingHelp.objects.values_list("key", flat=True))

        missing = [k for k in keys if k not in existing]
        if missing:
            rows = []
            for k in missing:
                if k in {"SCHEDULER_NODE_ID"}:
                    continue
                rows.append(
                    SchedulerSettingHelp(
                        key=k,
                        title="",
                        description="",
                        impact="",
                        editable=True,
                        input_type=SchedulerSettingHelp.InputType.TEXT,
                        enum_values_json=[],
                        constraints_json={},
                        examples_json=[],
                        is_secret=_is_secret_key(k),
                    )
                )
            if rows:
                SchedulerSettingHelp.objects.bulk_create(rows, ignore_conflicts=True)

        if apply_defaults:
            for k, spec in (DEFAULT_HELP or {}).items():
                if not str(k).startswith("SCHEDULER_"):
                    continue
                if k in {"SCHEDULER_NODE_ID"}:
                    continue
                defaults = {
                    "title": str(spec.get("title") or ""),
                    "description": str(spec.get("description") or ""),
                    "impact": str(spec.get("impact") or ""),
                    "editable": bool(spec.get("editable", True)),
                    "input_type": str(spec.get("input_type") or "text"),
                    "enum_values_json": list(spec.get("enum_values") or []),
                    "constraints_json": dict(spec.get("constraints") or {}),
                    "examples_json": list(spec.get("examples") or []),
                    "is_secret": _is_secret_key(k) or bool(spec.get("is_secret", False)),
                }
                SchedulerSettingHelp.objects.update_or_create(key=k, defaults=defaults)

    except (OperationalError, ProgrammingError):
        return
    except Exception:
        return
