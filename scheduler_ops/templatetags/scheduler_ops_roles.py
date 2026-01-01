from __future__ import annotations

from django import template

from scheduler_ops.roles import is_app_operator, is_ops_admin, is_superuser

register = template.Library()


@register.filter(name="ops_is_app")
def ops_is_app(user) -> bool:
    return is_app_operator(user)


@register.filter(name="ops_is_admin")
def ops_is_admin(user) -> bool:
    return is_ops_admin(user)


@register.filter(name="ops_is_super")
def ops_is_super(user) -> bool:
    return is_superuser(user)
