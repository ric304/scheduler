from __future__ import annotations

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from scheduler_ops.roles import OPS_ROLES, ensure_ops_groups


class Command(BaseCommand):
    help = "Create default Scheduler Ops role groups."

    def handle(self, *args, **options):
        # Not required anymore (auto-created on startup), but kept as a utility.
        ensure_ops_groups()
        existing = Group.objects.filter(name__in=[OPS_ROLES.APP_OPERATOR, OPS_ROLES.OPS_ADMIN, OPS_ROLES.SUPERUSER]).values_list(
            "name", flat=True
        )
        self.stdout.write(self.style.SUCCESS(f"Ensured groups: {', '.join(sorted(existing))}"))
