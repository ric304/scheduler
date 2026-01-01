from __future__ import annotations

from django.contrib import admin

from .models import AdminActionLog, SchedulerSetting


@admin.register(SchedulerSetting)
class SchedulerSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "updated_at")
    search_fields = ("key",)


@admin.register(AdminActionLog)
class AdminActionLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor", "target")
    list_filter = ("action",)
    search_fields = ("actor", "target")
    ordering = ("-created_at",)
