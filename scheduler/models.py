from __future__ import annotations

from django.db import models


class JobDefinition(models.Model):
    class JobType(models.TextChoices):
        TIME = "time", "time"
        EVENT = "event", "event"

    class ConcurrencyPolicy(models.TextChoices):
        FORBID = "forbid", "forbid"
        ALLOW = "allow", "allow"
        REPLACE = "replace", "replace"

    name = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    type = models.CharField(max_length=16, choices=JobType.choices)

    command_name = models.CharField(max_length=200)
    default_args_json = models.JSONField(default=dict, blank=True)

    schedule = models.JSONField(default=dict, blank=True)

    timeout_seconds = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=0)
    retry_backoff_seconds = models.IntegerField(default=0)

    concurrency_policy = models.CharField(
        max_length=16,
        choices=ConcurrencyPolicy.choices,
        default=ConcurrencyPolicy.FORBID,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scheduler_job_definitions"
        indexes = [
            models.Index(fields=["enabled"], name="sched_jobdef_enabled"),
            models.Index(fields=["type"], name="sched_jobdef_type"),
        ]

    def __str__(self) -> str:
        return self.name


class JobRun(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "PENDING"
        ASSIGNED = "ASSIGNED", "ASSIGNED"
        RUNNING = "RUNNING", "RUNNING"
        SUCCEEDED = "SUCCEEDED", "SUCCEEDED"
        FAILED = "FAILED", "FAILED"
        CANCELED = "CANCELED", "CANCELED"
        SKIPPED = "SKIPPED", "SKIPPED"
        TIMED_OUT = "TIMED_OUT", "TIMED_OUT"
        ORPHANED = "ORPHANED", "ORPHANED"

    class ContinuationState(models.TextChoices):
        NONE = "NONE", "NONE"
        CONFIRMING = "CONFIRMING", "CONFIRMING"

    job_definition = models.ForeignKey(JobDefinition, on_delete=models.CASCADE)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    assigned_worker_id = models.CharField(max_length=128, blank=True)

    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempt = models.IntegerField(default=0)
    version = models.IntegerField(default=0)

    leader_epoch = models.BigIntegerField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    exit_code = models.IntegerField(null=True, blank=True)
    error_summary = models.TextField(blank=True)
    log_ref = models.CharField(max_length=512, blank=True)
    idempotency_key = models.CharField(max_length=256, blank=True)

    # --- Resource usage (recorded by worker at completion; optional/MVP) ---
    resource_cpu_seconds_total = models.FloatField(null=True, blank=True)
    resource_peak_rss_bytes = models.BigIntegerField(null=True, blank=True)
    resource_io_read_bytes = models.BigIntegerField(null=True, blank=True)
    resource_io_write_bytes = models.BigIntegerField(null=True, blank=True)

    continuation_state = models.CharField(
        max_length=16,
        choices=ContinuationState.choices,
        default=ContinuationState.NONE,
    )
    continuation_check_started_at = models.DateTimeField(null=True, blank=True)
    continuation_check_deadline_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scheduler_job_runs"
        constraints = [
            models.UniqueConstraint(
                fields=["job_definition", "scheduled_for"],
                name="sched_jobrun_unique_schedule",
            )
        ]
        indexes = [
            models.Index(fields=["state", "scheduled_for"], name="sched_jobrun_state_scheduled"),
            models.Index(fields=["assigned_worker_id", "state"], name="sched_jobrun_worker_state"),
            models.Index(fields=["created_at"], name="sched_jobrun_created_at"),
        ]

    def __str__(self) -> str:
        return f"JobRun({self.id}) {self.state}"


class Event(models.Model):
    event_type = models.CharField(max_length=128)
    payload_json = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=256, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "scheduler_events"
        indexes = [
            models.Index(fields=["processed_at", "created_at"], name="sched_event_proc_created"),
        ]

    def __str__(self) -> str:
        return f"{self.event_type}"


class SchedulerSetting(models.Model):
    key = models.CharField(max_length=128, unique=True)
    value_json = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scheduler_settings"

    def __str__(self) -> str:
        return self.key


class SchedulerSettingHelp(models.Model):
    """Human-facing help for a setting key.

    Stored in DB so Ops UI can show meaning/impact without redeploying code.
    """

    class InputType(models.TextChoices):
        TEXT = "text", "text"
        BOOL = "bool", "bool"
        ENUM = "enum", "enum"

    key = models.CharField(max_length=128, unique=True)

    title = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    impact = models.TextField(blank=True)

    editable = models.BooleanField(default=True)
    input_type = models.CharField(max_length=16, choices=InputType.choices, default=InputType.TEXT)
    enum_values_json = models.JSONField(default=list, blank=True)
    constraints_json = models.JSONField(default=dict, blank=True)
    examples_json = models.JSONField(default=list, blank=True)
    is_secret = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scheduler_setting_help"

    def __str__(self) -> str:
        return self.key


class AdminActionLog(models.Model):
    actor = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=128)
    target = models.CharField(max_length=256, blank=True)
    payload_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "scheduler_admin_action_logs"
        indexes = [
            models.Index(fields=["created_at"], name="sched_audit_created_at"),
            models.Index(fields=["action"], name="sched_audit_action"),
        ]

    def __str__(self) -> str:
        return f"{self.created_at.isoformat()} {self.action}"


class ConfigReloadRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "PENDING"
        APPLIED = "APPLIED", "APPLIED"
        FAILED = "FAILED", "FAILED"

    requested_by = models.CharField(max_length=150, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    applied_at = models.DateTimeField(null=True, blank=True)

    leader_worker_id = models.CharField(max_length=128, blank=True)
    leader_epoch = models.BigIntegerField(null=True, blank=True)

    result_json = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "scheduler_config_reload_requests"
        indexes = [
            models.Index(fields=["status", "requested_at"], name="sched_reload_status_req"),
            models.Index(fields=["requested_at"], name="sched_reload_requested_at"),
        ]

    def __str__(self) -> str:
        return f"ConfigReloadRequest({self.id}) {self.status}"
