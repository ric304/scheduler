from __future__ import annotations

import os
import socket
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env if present (dev convenience)
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") not in {"0", "false", "False"}

ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "scheduler",
    "scheduler_ops",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "scheduler_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "scheduler_ops.context_processors.ops_version",
            ],
        },
    }
]

WSGI_APPLICATION = "scheduler_project.wsgi.application"


database_url = os.environ.get("DATABASE_URL")
if database_url:
    DATABASES = {"default": dj_database_url.parse(database_url, conn_max_age=60)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ja"
TIME_ZONE = "Asia/Tokyo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/ops/"
LOGOUT_REDIRECT_URL = "/accounts/login/"


# --- Scheduler settings (M0: configuration surface only) ---
SCHEDULER_NODE_ID = os.environ.get("SCHEDULER_NODE_ID") or socket.gethostname()
SCHEDULER_REDIS_URL = os.environ.get("SCHEDULER_REDIS_URL", "redis://localhost:6379/0")
SCHEDULER_GRPC_HOST = os.environ.get("SCHEDULER_GRPC_HOST", "127.0.0.1")

# If worker gRPC port is not explicitly specified, auto-select an available port from this range.
SCHEDULER_GRPC_PORT_RANGE_START = int(os.environ.get("SCHEDULER_GRPC_PORT_RANGE_START", "50051"))
SCHEDULER_GRPC_PORT_RANGE_END = int(os.environ.get("SCHEDULER_GRPC_PORT_RANGE_END", "50150"))

SCHEDULER_TLS_CERT_FILE = os.environ.get("SCHEDULER_TLS_CERT_FILE", "")
SCHEDULER_TLS_KEY_FILE = os.environ.get("SCHEDULER_TLS_KEY_FILE", "")

# --- Scheduling (M3) ---
# How far ahead leader assigns (and ensures JobRuns exist) for time-based jobs.
SCHEDULER_ASSIGN_AHEAD_SECONDS = int(os.environ.get("SCHEDULER_ASSIGN_AHEAD_SECONDS", "60"))

# If >0, skip executing assigned runs that are older than this many seconds.
# (Avoid executing a large backlog after downtime.)
SCHEDULER_SKIP_LATE_RUNS_AFTER_SECONDS = int(os.environ.get("SCHEDULER_SKIP_LATE_RUNS_AFTER_SECONDS", "300"))

# --- Reassignment / continuation (M5) ---
# If an ASSIGNED JobRun does not start within this grace period AND the assigned worker is not active,
# leader will orphan it and reassign.
SCHEDULER_REASSIGN_ASSIGNED_AFTER_SECONDS = int(os.environ.get("SCHEDULER_REASSIGN_ASSIGNED_AFTER_SECONDS", "10"))

# If a RUNNING JobRun's worker disappears, mark it as CONFIRMING for this long before reassigning.
SCHEDULER_CONTINUATION_CONFIRM_SECONDS = int(os.environ.get("SCHEDULER_CONTINUATION_CONFIRM_SECONDS", "30"))

# --- Assignment balancing (M3/M5) ---
# Weighted balancing by role (bigger = receive more assignments).
# Examples:
# - 1:3 (leader:worker) -> leader=1, subleader=2, worker=3
# - 2:3 (leader:worker) -> leader=2, subleader=2, worker=3
SCHEDULER_ASSIGN_WEIGHT_LEADER = int(os.environ.get("SCHEDULER_ASSIGN_WEIGHT_LEADER", "1"))
SCHEDULER_ASSIGN_WEIGHT_SUBLEADER = int(os.environ.get("SCHEDULER_ASSIGN_WEIGHT_SUBLEADER", "2"))
SCHEDULER_ASSIGN_WEIGHT_WORKER = int(os.environ.get("SCHEDULER_ASSIGN_WEIGHT_WORKER", "3"))

# How much a RUNNING job contributes compared to an ASSIGNED-but-not-started job.
SCHEDULER_ASSIGN_RUNNING_LOAD_WEIGHT = int(os.environ.get("SCHEDULER_ASSIGN_RUNNING_LOAD_WEIGHT", "2"))

# Optional: conservative rebalancing of ASSIGNED (not started) jobs.
SCHEDULER_REBALANCE_ASSIGNED_ENABLED = os.environ.get("SCHEDULER_REBALANCE_ASSIGNED_ENABLED", "1") not in {
    "0",
    "false",
    "False",
}
SCHEDULER_REBALANCE_ASSIGNED_MIN_FUTURE_SECONDS = int(
    os.environ.get("SCHEDULER_REBALANCE_ASSIGNED_MIN_FUTURE_SECONDS", "30")
)
SCHEDULER_REBALANCE_ASSIGNED_MAX_PER_TICK = int(os.environ.get("SCHEDULER_REBALANCE_ASSIGNED_MAX_PER_TICK", "50"))
SCHEDULER_REBALANCE_ASSIGNED_COOLDOWN_SECONDS = int(
    os.environ.get("SCHEDULER_REBALANCE_ASSIGNED_COOLDOWN_SECONDS", "5")
)

# --- Events API (M4) ---
# If set (non-empty), /api/events/ingest/ requires X-Scheduler-Token.
# If empty, it requires an authenticated Django session.
SCHEDULER_EVENTS_API_TOKEN = os.environ.get("SCHEDULER_EVENTS_API_TOKEN", "")

# --- Job log archival (S3-compatible / MinIO) ---
# When enabled, worker uploads its local log file after completion and updates JobRun.log_ref.
SCHEDULER_LOG_ARCHIVE_ENABLED = os.environ.get("SCHEDULER_LOG_ARCHIVE_ENABLED", "0") not in {"0", "false", "False"}
SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL = os.environ.get(
    "SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL", "http://127.0.0.1:9000"
)
SCHEDULER_LOG_ARCHIVE_S3_REGION = os.environ.get("SCHEDULER_LOG_ARCHIVE_S3_REGION", "us-east-1")
SCHEDULER_LOG_ARCHIVE_ACCESS_KEY_ID = os.environ.get("SCHEDULER_LOG_ARCHIVE_ACCESS_KEY_ID", "minioadmin")
SCHEDULER_LOG_ARCHIVE_SECRET_ACCESS_KEY = os.environ.get("SCHEDULER_LOG_ARCHIVE_SECRET_ACCESS_KEY", "minioadmin")
SCHEDULER_LOG_ARCHIVE_BUCKET = os.environ.get("SCHEDULER_LOG_ARCHIVE_BUCKET", "scheduler-logs")
# Public base URL used to build a clickable link (must be reachable from the browser).
SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL = os.environ.get(
    "SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL", "http://127.0.0.1:9000"
)
SCHEDULER_LOG_ARCHIVE_PREFIX = os.environ.get("SCHEDULER_LOG_ARCHIVE_PREFIX", "job-logs")

# --- Local log lifecycle (worker) ---
# If enabled, delete local log file after successful upload.
SCHEDULER_LOG_LOCAL_DELETE_AFTER_UPLOAD = os.environ.get("SCHEDULER_LOG_LOCAL_DELETE_AFTER_UPLOAD", "0") not in {
    "0",
    "false",
    "False",
}
# If >0, worker will delete local log files older than this many hours.
# (MVP: cleanup is triggered after each job finishes)
SCHEDULER_LOG_LOCAL_RETENTION_HOURS = int(os.environ.get("SCHEDULER_LOG_LOCAL_RETENTION_HOURS", "0"))

# Deployment hint for Ops UI behavior (e.g., show console log link only on k8s)
SCHEDULER_DEPLOYMENT = os.environ.get("SCHEDULER_DEPLOYMENT", "local")

