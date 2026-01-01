from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpsRoles:
    APP_OPERATOR: str = "schedule_ops_app_operator"
    OPS_ADMIN: str = "schedule_ops_ops_admin"
    SUPERUSER: str = "schedule_ops_superuser"


OPS_ROLES = OpsRoles()


_LEGACY_GROUP_NAMES = {
    # Backward compatibility (older naming)
    OPS_ROLES.APP_OPERATOR: ["Scheduler App Operator"],
    OPS_ROLES.OPS_ADMIN: ["Scheduler Ops Admin"],
    OPS_ROLES.SUPERUSER: ["Scheduler Superuser"],
}


def ensure_ops_groups() -> None:
    """Ensure fixed Ops role groups exist.

    This is intentionally safe to call at startup; it no-ops if auth tables
    are not ready yet.
    """

    try:
        from django.contrib.auth.models import Group
        from django.db.utils import OperationalError, ProgrammingError

        for name in (OPS_ROLES.APP_OPERATOR, OPS_ROLES.OPS_ADMIN, OPS_ROLES.SUPERUSER):
            try:
                Group.objects.get_or_create(name=name)
            except (OperationalError, ProgrammingError):
                return
    except Exception:
        return


def _in_group(user, group_name: str) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    names = [group_name] + list(_LEGACY_GROUP_NAMES.get(group_name, []))
    return user.groups.filter(name__in=names).exists()


def is_superuser(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_superuser", False)) or _in_group(user, OPS_ROLES.SUPERUSER)


def is_ops_admin(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return is_superuser(user) or _in_group(user, OPS_ROLES.OPS_ADMIN)


def is_app_operator(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return is_ops_admin(user) or _in_group(user, OPS_ROLES.APP_OPERATOR)
