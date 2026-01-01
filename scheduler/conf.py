from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any

from django.conf import settings


# These values are process-local or environment-bound and should not be overridden via DB.
_NON_OVERRIDABLE_KEYS = {
    "SCHEDULER_NODE_ID",
}


_settings_cache_lock = threading.Lock()
_settings_cache: dict[str, Any] | None = None
_settings_cache_generation: int = 0


def _normalize_setting_value(value_json: Any) -> Any:
    # Backward/forward compatible:
    # - Prefer {"value": ...} wrapper (recommended)
    # - Allow storing primitives directly (string/int/bool/list/dict)
    if isinstance(value_json, dict) and "value" in value_json and len(value_json) == 1:
        return value_json.get("value")
    return value_json


def _load_settings_overrides_from_db() -> dict[str, Any]:
    try:
        from scheduler.models import SchedulerSetting

        rows = SchedulerSetting.objects.all().only("key", "value_json")
        return {str(r.key): _normalize_setting_value(r.value_json) for r in rows}
    except Exception:
        # DB未初期化/マイグレーション前などでも落とさない。
        return {}


def reload_scheduler_settings_cache() -> int:
    """Clear cached SchedulerSetting overrides.

    Returns new cache generation.
    """

    global _settings_cache, _settings_cache_generation
    with _settings_cache_lock:
        _settings_cache = None
        _settings_cache_generation += 1
        return int(_settings_cache_generation)


def _get_db_overrides(*, fresh: bool = False) -> dict[str, Any]:
    global _settings_cache
    if fresh:
        return _load_settings_overrides_from_db()
    with _settings_cache_lock:
        if _settings_cache is None:
            _settings_cache = _load_settings_overrides_from_db()
        return dict(_settings_cache)


def get_setting(*, key: str, default: Any = None, fresh: bool = False) -> Any:
    if key in _NON_OVERRIDABLE_KEYS:
        return getattr(settings, key, default)
    db = _get_db_overrides(fresh=fresh)
    if key in db:
        return db[key]
    if hasattr(settings, key):
        return getattr(settings, key)
    return default


def get_setting_with_source(*, key: str, default: Any = None, fresh: bool = False) -> tuple[Any, str]:
    if key in _NON_OVERRIDABLE_KEYS:
        return getattr(settings, key, default), "env"
    db = _get_db_overrides(fresh=fresh)
    if key in db:
        return db[key], "db"
    if hasattr(settings, key):
        return getattr(settings, key), "env"
    return default, "default"


def list_all_scheduler_setting_keys(*, fresh: bool = False) -> list[str]:
    keys = {k for k in dir(settings) if k.startswith("SCHEDULER_")}
    keys.update(_get_db_overrides(fresh=fresh).keys())

    # Include help-seeded keys so Ops Settings can manage parameters via DB
    # without requiring environment-variable exposure in settings.py.
    try:
        from scheduler.models import SchedulerSettingHelp

        help_keys = set(SchedulerSettingHelp.objects.values_list("key", flat=True))
        keys.update({str(k) for k in help_keys if str(k).startswith("SCHEDULER_")})
    except Exception:
        # DB未初期化/マイグレーション前などでも落とさない。
        pass

    keys.difference_update(_NON_OVERRIDABLE_KEYS)
    return sorted(keys)


def get_str(*, key: str, default: str = "", fresh: bool = False) -> str:
    v = get_setting(key=key, default=default, fresh=fresh)
    return str(v) if v is not None else str(default)


def get_int(*, key: str, default: int = 0, fresh: bool = False) -> int:
    v = get_setting(key=key, default=default, fresh=fresh)
    try:
        return int(v)
    except Exception:
        return int(default)


def get_bool(*, key: str, default: bool = False, fresh: bool = False) -> bool:
    v = get_setting(key=key, default=default, fresh=fresh)
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, (int, float)):
        return bool(int(v))
    if isinstance(v, str):
        return v.strip() not in {"", "0", "false", "False", "no", "No"}
    return bool(v) if v is not None else bool(default)


@dataclass(frozen=True)
class SchedulerRuntimeConfig:
    node_id: str
    redis_url: str
    grpc_host: str
    grpc_port_range_start: int
    grpc_port_range_end: int
    tls_cert_file: str
    tls_key_file: str

    assign_ahead_seconds: int

    skip_late_runs_after_seconds: int

    reassign_assigned_after_seconds: int
    continuation_confirm_seconds: int

    assign_weight_leader: int
    assign_weight_subleader: int
    assign_weight_worker: int
    assign_running_load_weight: int

    rebalance_assigned_enabled: bool
    rebalance_assigned_min_future_seconds: int
    rebalance_assigned_max_per_tick: int
    rebalance_assigned_cooldown_seconds: int


def get_scheduler_config() -> SchedulerRuntimeConfig:
    return SchedulerRuntimeConfig(
        node_id=str(getattr(settings, "SCHEDULER_NODE_ID", "")),
        redis_url=get_str(key="SCHEDULER_REDIS_URL"),
        grpc_host=get_str(key="SCHEDULER_GRPC_HOST"),
        grpc_port_range_start=get_int(key="SCHEDULER_GRPC_PORT_RANGE_START", default=50051),
        grpc_port_range_end=get_int(key="SCHEDULER_GRPC_PORT_RANGE_END", default=50150),
        tls_cert_file=get_str(key="SCHEDULER_TLS_CERT_FILE"),
        tls_key_file=get_str(key="SCHEDULER_TLS_KEY_FILE"),
        assign_ahead_seconds=get_int(key="SCHEDULER_ASSIGN_AHEAD_SECONDS", default=60),

        skip_late_runs_after_seconds=get_int(key="SCHEDULER_SKIP_LATE_RUNS_AFTER_SECONDS", default=300),
        reassign_assigned_after_seconds=get_int(key="SCHEDULER_REASSIGN_ASSIGNED_AFTER_SECONDS", default=10),
        continuation_confirm_seconds=get_int(key="SCHEDULER_CONTINUATION_CONFIRM_SECONDS", default=30),
        assign_weight_leader=get_int(key="SCHEDULER_ASSIGN_WEIGHT_LEADER", default=1),
        assign_weight_subleader=get_int(key="SCHEDULER_ASSIGN_WEIGHT_SUBLEADER", default=2),
        assign_weight_worker=get_int(key="SCHEDULER_ASSIGN_WEIGHT_WORKER", default=3),
        assign_running_load_weight=get_int(key="SCHEDULER_ASSIGN_RUNNING_LOAD_WEIGHT", default=2),

        rebalance_assigned_enabled=get_bool(key="SCHEDULER_REBALANCE_ASSIGNED_ENABLED", default=True),
        rebalance_assigned_min_future_seconds=get_int(key="SCHEDULER_REBALANCE_ASSIGNED_MIN_FUTURE_SECONDS", default=30),
        rebalance_assigned_max_per_tick=get_int(key="SCHEDULER_REBALANCE_ASSIGNED_MAX_PER_TICK", default=50),
        rebalance_assigned_cooldown_seconds=get_int(key="SCHEDULER_REBALANCE_ASSIGNED_COOLDOWN_SECONDS", default=5),
    )
