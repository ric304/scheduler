from __future__ import annotations

from django.urls import path

from .api_views import ingest_event

app_name = "scheduler_api"

urlpatterns = [
    path("events/ingest/", ingest_event, name="ingest_event"),
]
