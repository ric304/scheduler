from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

from scheduler.metrics import metrics_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("ops/", include("scheduler_ops.urls")),
    path("api/", include("scheduler.api_urls")),
    path("metrics/", metrics_view, name="metrics"),
]
