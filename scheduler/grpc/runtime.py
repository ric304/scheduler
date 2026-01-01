from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import grpc
try:
    import boto3  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from scheduler.grpc import worker_pb2, worker_pb2_grpc
from scheduler.conf import get_bool, get_int, get_str, reload_scheduler_settings_cache
from scheduler.metrics import observe_job_finished, observe_job_started, set_worker_current_job
from scheduler.models import JobRun

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


def _collect_proc_tree_counters(pid: int) -> tuple[float, int, int, int]:
    """Return (cpu_seconds, rss_bytes_sum, io_read_bytes, io_write_bytes) for pid + children."""

    if psutil is None:
        return 0.0, 0, 0, 0

    try:
        root = psutil.Process(int(pid))
    except Exception:
        return 0.0, 0, 0, 0

    procs = [root]
    try:
        procs.extend(root.children(recursive=True))
    except Exception:
        pass

    cpu_s = 0.0
    rss_sum = 0
    io_r = 0
    io_w = 0
    for p in procs:
        try:
            ct = p.cpu_times()
            cpu_s += float(getattr(ct, "user", 0.0) or 0.0) + float(getattr(ct, "system", 0.0) or 0.0)
        except Exception:
            pass
        try:
            mi = p.memory_info()
            rss_sum += int(getattr(mi, "rss", 0) or 0)
        except Exception:
            pass
        try:
            io = p.io_counters()
            io_r += int(getattr(io, "read_bytes", 0) or 0)
            io_w += int(getattr(io, "write_bytes", 0) or 0)
        except Exception:
            pass

    return cpu_s, rss_sum, io_r, io_w


@dataclass
class WorkerRuntimeState:
    worker_id: str
    node_id: str

    # Updated by the main loop
    role: str = "worker"  # leader/subleader/worker
    cluster_epoch: int = 0
    leader_epoch: int = 0
    leader_worker_id: str = ""
    last_heartbeat_unix_ms: int = 0

    detached: bool = False
    draining: bool = False
    load: int = 0
    current_job_run_id: str = ""


def _job_logs_dir(*, worker_id: str) -> Path:
    base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
    return base / ".scheduler-logs" / worker_id


def _log_archive_config() -> dict:
    return {
        "enabled": get_bool(key="SCHEDULER_LOG_ARCHIVE_ENABLED", default=False),
        "endpoint_url": get_str(key="SCHEDULER_LOG_ARCHIVE_S3_ENDPOINT_URL", default="").strip(),
        "region": get_str(key="SCHEDULER_LOG_ARCHIVE_S3_REGION", default="us-east-1") or "us-east-1",
        "access_key_id": get_str(key="SCHEDULER_LOG_ARCHIVE_ACCESS_KEY_ID", default="").strip(),
        "secret_access_key": get_str(key="SCHEDULER_LOG_ARCHIVE_SECRET_ACCESS_KEY", default="").strip(),
        "bucket": get_str(key="SCHEDULER_LOG_ARCHIVE_BUCKET", default="").strip(),
        "public_base_url": get_str(key="SCHEDULER_LOG_ARCHIVE_PUBLIC_BASE_URL", default="").strip(),
        "prefix": get_str(key="SCHEDULER_LOG_ARCHIVE_PREFIX", default="job-logs") or "job-logs",
    }


def _local_log_policy_config() -> dict:
    return {
        "delete_after_upload": get_bool(key="SCHEDULER_LOG_LOCAL_DELETE_AFTER_UPLOAD", default=False),
        "retention_hours": get_int(key="SCHEDULER_LOG_LOCAL_RETENTION_HOURS", default=0),
    }


def _delete_local_log_file(path: Path) -> Optional[str]:
    try:
        path.unlink(missing_ok=True)
        return None
    except Exception as e:
        return f"{type(e).__name__}"


def _cleanup_old_local_logs(*, worker_id: str, exclude: Optional[Path] = None) -> None:
    cfg = _local_log_policy_config()
    retention_hours = int(cfg.get("retention_hours") or 0)
    if retention_hours <= 0:
        return

    logs_dir = _job_logs_dir(worker_id=worker_id)
    try:
        if not logs_dir.exists():
            return
    except Exception:
        return

    cutoff = time.time() - (retention_hours * 3600)
    try:
        for p in logs_dir.glob("jobrun_*.log"):
            try:
                if exclude is not None and p.resolve() == exclude.resolve():
                    continue
                st = p.stat()
                if st.st_mtime < cutoff:
                    _delete_local_log_file(p)
            except Exception:
                continue
    except Exception:
        return


def _archive_log_if_enabled(*, local_path: Path, worker_id: str, job_run_id: int) -> tuple[Optional[str], Optional[str]]:
    cfg = _log_archive_config()
    if not cfg.get("enabled"):
        return None, None
    if boto3 is None:
        return None, "boto3 not installed"
    if not local_path.exists():
        return None, "local log file not found"
    if not cfg.get("endpoint_url") or not cfg.get("bucket"):
        return None, "log archive config missing endpoint_url/bucket"

    key = f"{cfg['prefix'].rstrip('/')}/{worker_id}/jobrun_{job_run_id}.log"
    try:
        client = boto3.client(
            "s3",
            endpoint_url=cfg["endpoint_url"],
            region_name=cfg["region"],
            aws_access_key_id=cfg["access_key_id"],
            aws_secret_access_key=cfg["secret_access_key"],
        )
        client.upload_file(str(local_path), cfg["bucket"], key)
    except Exception as e:
        return None, f"upload failed: {type(e).__name__}"

    public_base = cfg.get("public_base_url")
    if public_base:
        url = f"{public_base.rstrip('/')}/{cfg['bucket']}/{key}"
        return url, None
    return f"s3://{cfg['bucket']}/{key}", None


def _safe_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except Exception:
        return None


def _jobrun_mark_running(*, job_run_id: int, worker_id: str, leader_epoch: int, attempt: int, log_ref: str) -> bool:
    now = timezone.now()
    with transaction.atomic():
        try:
            jr = JobRun.objects.select_for_update().get(id=job_run_id)
        except JobRun.DoesNotExist:
            return False

        if jr.state != JobRun.State.ASSIGNED:
            return False
        if jr.assigned_worker_id != worker_id:
            return False
        # Fence: allow only the same/newer leader epoch (MVP uses >= check)
        if jr.leader_epoch is not None and int(jr.leader_epoch) > int(leader_epoch):
            return False

        jr.state = JobRun.State.RUNNING
        jr.started_at = now
        jr.attempt = int(attempt)
        jr.log_ref = str(log_ref)
        jr.version = int(jr.version) + 1
        jr.save(update_fields=["state", "started_at", "attempt", "log_ref", "version", "updated_at"])
        return True


def _jobrun_finish(
    *,
    job_run_id: int,
    worker_id: str,
    final_state: str,
    exit_code: Optional[int],
    error_summary: str,
    log_ref: str,
    resource_cpu_seconds_total: Optional[float] = None,
    resource_peak_rss_bytes: Optional[int] = None,
    resource_io_read_bytes: Optional[int] = None,
    resource_io_write_bytes: Optional[int] = None,
):
    now = timezone.now()
    with transaction.atomic():
        try:
            jr = JobRun.objects.select_for_update().get(id=job_run_id)
        except JobRun.DoesNotExist:
            return

        # Only the assigned worker should complete it.
        if jr.assigned_worker_id and jr.assigned_worker_id != worker_id:
            return

        # If already terminal, do nothing.
        if jr.state in {
            JobRun.State.SUCCEEDED,
            JobRun.State.FAILED,
            JobRun.State.CANCELED,
            JobRun.State.SKIPPED,
            JobRun.State.TIMED_OUT,
        }:
            return

        jr.state = final_state
        jr.finished_at = now
        jr.exit_code = exit_code
        jr.error_summary = (error_summary or "")[:2000]
        jr.log_ref = str(log_ref)
        if resource_cpu_seconds_total is not None:
            jr.resource_cpu_seconds_total = float(resource_cpu_seconds_total)
        if resource_peak_rss_bytes is not None:
            jr.resource_peak_rss_bytes = int(resource_peak_rss_bytes)
        if resource_io_read_bytes is not None:
            jr.resource_io_read_bytes = int(resource_io_read_bytes)
        if resource_io_write_bytes is not None:
            jr.resource_io_write_bytes = int(resource_io_write_bytes)
        jr.version = int(jr.version) + 1
        update_fields = ["state", "finished_at", "exit_code", "error_summary", "log_ref", "version", "updated_at"]
        if resource_cpu_seconds_total is not None:
            update_fields.append("resource_cpu_seconds_total")
        if resource_peak_rss_bytes is not None:
            update_fields.append("resource_peak_rss_bytes")
        if resource_io_read_bytes is not None:
            update_fields.append("resource_io_read_bytes")
        if resource_io_write_bytes is not None:
            update_fields.append("resource_io_write_bytes")
        jr.save(update_fields=update_fields)


class WorkerService(worker_pb2_grpc.WorkerServiceServicer):
    def __init__(self, state: WorkerRuntimeState, lock: threading.Lock):
        self._state = state
        self._lock = lock
        self._proc: Optional[subprocess.Popen] = None
        self._proc_job_run_id: str = ""
        self._proc_log_path: str = ""
        self._proc_cancel_requested: bool = False

    def Ping(self, request: worker_pb2.PingRequest, context: grpc.ServicerContext) -> worker_pb2.PingResponse:
        with self._lock:
            now_ms = int(time.time() * 1000)
            return worker_pb2.PingResponse(
                worker_id=self._state.worker_id,
                node_id=self._state.node_id,
                observed_leader_epoch=int(self._state.cluster_epoch),
                now_unix_ms=now_ms,
            )

    def GetStatus(
        self, request: worker_pb2.GetStatusRequest, context: grpc.ServicerContext
    ) -> worker_pb2.GetStatusResponse:
        with self._lock:
            return worker_pb2.GetStatusResponse(
                worker_id=self._state.worker_id,
                node_id=self._state.node_id,
                role=self._state.role,
                detached=bool(self._state.detached),
                draining=bool(self._state.draining),
                load=int(self._state.load),
                current_job_run_id=self._state.current_job_run_id,
                observed_leader_epoch=int(self._state.cluster_epoch),
                last_heartbeat_unix_ms=int(self._state.last_heartbeat_unix_ms),
            )

    def StartJob(self, request: worker_pb2.StartJobRequest, context: grpc.ServicerContext) -> worker_pb2.StartJobResponse:
        with self._lock:
            if int(request.leader_epoch) < int(self._state.cluster_epoch or 0):
                return worker_pb2.StartJobResponse(
                    result=worker_pb2.StartJobResponse.REJECTED_OLD_EPOCH,
                    message="old epoch",
                )
            if self._state.detached:
                return worker_pb2.StartJobResponse(
                    result=worker_pb2.StartJobResponse.REJECTED_DETACHED,
                    message="detached",
                )
            if self._state.draining:
                return worker_pb2.StartJobResponse(
                    result=worker_pb2.StartJobResponse.REJECTED_DRAINING,
                    message="draining",
                )
            if self._proc is not None or self._state.current_job_run_id:
                return worker_pb2.StartJobResponse(
                    result=worker_pb2.StartJobResponse.REJECTED_ALREADY_RUNNING,
                    message="already running",
                )

        job_run_id_str = (request.job_run_id or "").strip()
        job_run_id = _safe_int(job_run_id_str)
        command_name = (request.command_name or "").strip()
        args_json = request.args_json or "{}"
        timeout_seconds = int(request.timeout_seconds or 0)
        attempt = int(request.attempt or 0)

        if not job_run_id or job_run_id <= 0:
            return worker_pb2.StartJobResponse(
                result=worker_pb2.StartJobResponse.REJECTED_INVALID,
                message="invalid job_run_id",
            )
        if not command_name:
            return worker_pb2.StartJobResponse(
                result=worker_pb2.StartJobResponse.REJECTED_INVALID,
                message="invalid command_name",
            )
        try:
            parsed_args = json.loads(args_json) if args_json else {}
            if parsed_args is None:
                parsed_args = {}
            if not isinstance(parsed_args, (dict, list)):
                return worker_pb2.StartJobResponse(
                    result=worker_pb2.StartJobResponse.REJECTED_INVALID,
                    message="args_json must be object or array",
                )
        except Exception:
            return worker_pb2.StartJobResponse(
                result=worker_pb2.StartJobResponse.REJECTED_INVALID,
                message="args_json must be valid JSON",
            )

        logs_dir = _job_logs_dir(worker_id=self._state.worker_id)
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / f"jobrun_{job_run_id}.log"

        # Mark RUNNING in DB before starting.
        ok = _jobrun_mark_running(
            job_run_id=job_run_id,
            worker_id=self._state.worker_id,
            leader_epoch=int(request.leader_epoch),
            attempt=attempt,
            log_ref=str(log_path),
        )
        if not ok:
            return worker_pb2.StartJobResponse(
                result=worker_pb2.StartJobResponse.REJECTED_INVALID,
                message="job_run not eligible (state/worker/epoch)",
            )

        # Prometheus: record start and mark currently-running job.
        t0 = time.time()
        observe_job_started(command_name=str(command_name or ""))
        set_worker_current_job(worker_id=str(self._state.worker_id or ""), job_run_id=str(job_run_id), running=True)

        base_dir = Path(getattr(settings, "BASE_DIR", Path.cwd()))
        manage_py = base_dir / "manage.py"
        cmd = [sys.executable, str(manage_py), command_name]
        env = os.environ.copy()
        # Pass args via env to avoid forcing command-specific CLI flags.
        env["SCHEDULER_ARGS_JSON"] = json.dumps(parsed_args, ensure_ascii=False)
        env["SCHEDULER_JOB_RUN_ID"] = str(job_run_id)
        env["SCHEDULER_WORKER_ID"] = str(self._state.worker_id)

        try:
            log_f = open(log_path, "ab", buffering=0)
        except Exception as e:
            _jobrun_finish(
                job_run_id=job_run_id,
                worker_id=self._state.worker_id,
                final_state=JobRun.State.FAILED,
                exit_code=None,
                error_summary=f"failed to open log file: {type(e).__name__}",
                log_ref=str(log_path),
            )
            set_worker_current_job(worker_id=str(self._state.worker_id or ""), job_run_id=str(job_run_id), running=False)
            observe_job_finished(command_name=str(command_name or ""), result=str(JobRun.State.FAILED), duration_seconds=max(0.0, time.time() - t0))
            return worker_pb2.StartJobResponse(
                result=worker_pb2.StartJobResponse.REJECTED_INVALID,
                message="failed to open log file",
            )

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(base_dir),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            try:
                log_f.close()
            except Exception:
                pass
            _jobrun_finish(
                job_run_id=job_run_id,
                worker_id=self._state.worker_id,
                final_state=JobRun.State.FAILED,
                exit_code=None,
                error_summary=f"failed to start subprocess: {type(e).__name__}",
                log_ref=str(log_path),
            )
            set_worker_current_job(worker_id=str(self._state.worker_id or ""), job_run_id=str(job_run_id), running=False)
            observe_job_finished(command_name=str(command_name or ""), result=str(JobRun.State.FAILED), duration_seconds=max(0.0, time.time() - t0))
            with self._lock:
                self._state.current_job_run_id = ""
                self._state.load = 0
            return worker_pb2.StartJobResponse(
                result=worker_pb2.StartJobResponse.REJECTED_INVALID,
                message=f"failed to start subprocess: {type(e).__name__}",
            )

        with self._lock:
            self._proc = proc
            self._proc_job_run_id = job_run_id_str
            self._proc_log_path = str(log_path)
            self._proc_cancel_requested = False
            self._state.current_job_run_id = job_run_id_str
            self._state.load = 1

        def _wait_and_finalize():
            timed_out = False
            exit_code: Optional[int] = None
            res_cpu_s: Optional[float] = None
            res_peak_rss: Optional[int] = None
            res_io_r: Optional[int] = None
            res_io_w: Optional[int] = None

            # Resource monitoring (best-effort; requires psutil). Collect for proc tree.
            start_cpu_s = 0.0
            start_io_r = 0
            start_io_w = 0
            peak_rss = 0
            last_cpu_s = 0.0
            last_io_r = 0
            last_io_w = 0
            last_rss = 0
            if psutil is not None:
                s_cpu, s_rss, s_ir, s_iw = _collect_proc_tree_counters(proc.pid)
                start_cpu_s = float(s_cpu)
                start_io_r = int(s_ir)
                start_io_w = int(s_iw)
                peak_rss = int(s_rss)
                last_cpu_s = float(s_cpu)
                last_io_r = int(s_ir)
                last_io_w = int(s_iw)
                last_rss = int(s_rss)
            try:
                deadline = (time.time() + float(timeout_seconds)) if (timeout_seconds and timeout_seconds > 0) else None

                # Poll loop so we always capture CPU/IO while the process is still alive.
                while True:
                    if deadline is not None and time.time() >= deadline:
                        timed_out = True
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            try:
                                proc.wait(timeout=5)
                            except Exception:
                                pass
                        exit_code = proc.returncode
                        break

                    if psutil is not None:
                        c_cpu, c_rss, c_ir, c_iw = _collect_proc_tree_counters(proc.pid)
                        last_cpu_s = float(c_cpu)
                        last_io_r = int(c_ir)
                        last_io_w = int(c_iw)
                        last_rss = int(c_rss)
                        peak_rss = max(int(peak_rss), int(c_rss))

                    rc = proc.poll()
                    if rc is not None:
                        exit_code = int(rc)
                        break

                    time.sleep(0.5)
            finally:
                try:
                    log_f.close()
                except Exception:
                    pass

            if psutil is not None:
                # Use last sampled counters to avoid losing stats after the process exits.
                peak_rss = max(int(peak_rss), int(last_rss))
                res_cpu_s = max(0.0, float(last_cpu_s) - float(start_cpu_s))
                res_peak_rss = int(peak_rss)
                res_io_r = max(0, int(last_io_r) - int(start_io_r))
                res_io_w = max(0, int(last_io_w) - int(start_io_w))

            with self._lock:
                cancel_requested = bool(self._proc_cancel_requested)

            if timed_out:
                final_state = JobRun.State.TIMED_OUT
                summary = "timed out"
            elif cancel_requested:
                final_state = JobRun.State.CANCELED
                summary = "canceled"
            elif exit_code == 0:
                final_state = JobRun.State.SUCCEEDED
                summary = ""
            else:
                final_state = JobRun.State.FAILED
                summary = f"exit_code={exit_code}"

            final_log_ref = str(log_path)
            archived_ref, archive_err = _archive_log_if_enabled(
                local_path=Path(log_path),
                worker_id=str(self._state.worker_id),
                job_run_id=int(job_run_id),
            )
            if archived_ref:
                final_log_ref = str(archived_ref)
                cfg = _local_log_policy_config()
                if cfg.get("delete_after_upload"):
                    del_err = _delete_local_log_file(Path(log_path))
                    if del_err:
                        suffix = f"local_log_delete_failed: {del_err}"
                        summary = (summary + "\n" if summary else "") + suffix
            elif archive_err:
                suffix = f"log_archive_failed: {archive_err}"
                summary = (summary + "\n" if summary else "") + suffix

            _jobrun_finish(
                job_run_id=job_run_id,
                worker_id=self._state.worker_id,
                final_state=final_state,
                exit_code=exit_code,
                error_summary=summary,
                log_ref=final_log_ref,
                resource_cpu_seconds_total=res_cpu_s,
                resource_peak_rss_bytes=res_peak_rss,
                resource_io_read_bytes=res_io_r,
                resource_io_write_bytes=res_io_w,
            )

            set_worker_current_job(worker_id=str(self._state.worker_id or ""), job_run_id=str(job_run_id), running=False)
            observe_job_finished(
                command_name=str(command_name or ""),
                result=str(final_state),
                duration_seconds=max(0.0, time.time() - t0),
            )

            with self._lock:
                self._proc = None
                self._proc_job_run_id = ""
                self._proc_log_path = ""
                self._proc_cancel_requested = False
                self._state.current_job_run_id = ""
                self._state.load = 0

            _cleanup_old_local_logs(worker_id=str(self._state.worker_id), exclude=Path(log_path))

        t = threading.Thread(target=_wait_and_finalize, name=f"jobrun-{job_run_id}", daemon=True)
        t.start()

        return worker_pb2.StartJobResponse(
            result=worker_pb2.StartJobResponse.ACCEPTED,
            message="accepted",
        )

    def CancelJob(
        self, request: worker_pb2.CancelJobRequest, context: grpc.ServicerContext
    ) -> worker_pb2.CancelJobResponse:
        with self._lock:
            if int(request.leader_epoch) < int(self._state.cluster_epoch or 0):
                return worker_pb2.CancelJobResponse(
                    result=worker_pb2.CancelJobResponse.REJECTED_OLD_EPOCH,
                    message="old epoch",
                )
            job_run_id = (request.job_run_id or "").strip()
            if not job_run_id:
                return worker_pb2.CancelJobResponse(
                    result=worker_pb2.CancelJobResponse.NOT_FOUND,
                    message="missing job_run_id",
                )

            if self._proc is None or self._proc_job_run_id != job_run_id:
                return worker_pb2.CancelJobResponse(
                    result=worker_pb2.CancelJobResponse.NOT_FOUND,
                    message="not running",
                )

            proc = self._proc
            log_ref = self._proc_log_path
            self._proc_cancel_requested = True

        # Terminate outside lock.
        try:
            proc.terminate()
        except Exception:
            pass

        job_run_int = _safe_int(job_run_id)
        if job_run_int:
            res_cpu_s = None
            res_peak_rss = None
            res_io_r = None
            res_io_w = None
            if psutil is not None:
                try:
                    c_cpu, c_rss, c_ir, c_iw = _collect_proc_tree_counters(proc.pid)
                    res_cpu_s = max(0.0, float(c_cpu))
                    res_peak_rss = int(c_rss)
                    res_io_r = max(0, int(c_ir))
                    res_io_w = max(0, int(c_iw))
                except Exception:
                    pass
            _jobrun_finish(
                job_run_id=job_run_int,
                worker_id=self._state.worker_id,
                final_state=JobRun.State.CANCELED,
                exit_code=None,
                error_summary=(request.reason or "canceled")[:2000],
                log_ref=str(log_ref or ""),
                resource_cpu_seconds_total=res_cpu_s,
                resource_peak_rss_bytes=res_peak_rss,
                resource_io_read_bytes=res_io_r,
                resource_io_write_bytes=res_io_w,
            )

        return worker_pb2.CancelJobResponse(
            result=worker_pb2.CancelJobResponse.ACCEPTED,
            message="cancel requested",
        )

    def Drain(self, request: worker_pb2.DrainRequest, context: grpc.ServicerContext) -> worker_pb2.DrainResponse:
        with self._lock:
            # M2: accept toggling drain flag locally (no persistence yet)
            self._state.draining = bool(request.enable)
            return worker_pb2.DrainResponse(draining=bool(self._state.draining))

    def ReloadConfig(
        self, request: worker_pb2.ReloadConfigRequest, context: grpc.ServicerContext
    ) -> worker_pb2.ReloadConfigResponse:
        # Fence by epoch (same semantics as StartJob/CancelJob).
        with self._lock:
            if int(request.leader_epoch) < int(self._state.cluster_epoch or 0):
                return worker_pb2.ReloadConfigResponse(ok=False, message="old epoch", cache_generation=0)

        gen = reload_scheduler_settings_cache()
        return worker_pb2.ReloadConfigResponse(ok=True, message="reloaded", cache_generation=int(gen))

    def ConfirmContinuation(
        self, request: worker_pb2.ConfirmContinuationRequest, context: grpc.ServicerContext
    ) -> worker_pb2.ConfirmContinuationResponse:
        return worker_pb2.ConfirmContinuationResponse(
            decision=worker_pb2.ConfirmContinuationResponse.DECISION_UNSPECIFIED,
            message="M2: not implemented yet",
        )


def _load_tls_material(cert_file: str, key_file: str) -> tuple[bytes, bytes]:
    with open(cert_file, "rb") as f:
        cert_pem = f.read()
    with open(key_file, "rb") as f:
        key_pem = f.read()
    return cert_pem, key_pem


def create_server_credentials(*, cert_file: str, key_file: str) -> grpc.ServerCredentials:
    cert_pem, key_pem = _load_tls_material(cert_file, key_file)
    # MVP: shared secret across pods. Trust the same certificate as root.
    return grpc.ssl_server_credentials(
        private_key_certificate_chain_pairs=[(key_pem, cert_pem)],
        root_certificates=cert_pem,
        require_client_auth=True,
    )


def create_channel_credentials(*, cert_file: str, key_file: str) -> grpc.ChannelCredentials:
    cert_pem, key_pem = _load_tls_material(cert_file, key_file)
    return grpc.ssl_channel_credentials(
        root_certificates=cert_pem,
        private_key=key_pem,
        certificate_chain=cert_pem,
    )


def start_worker_grpc_server(
    *,
    host: str,
    port: int,
    state: WorkerRuntimeState,
    lock: threading.Lock,
    tls_cert_file: str,
    tls_key_file: str,
) -> grpc.Server:
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=16))
    worker_pb2_grpc.add_WorkerServiceServicer_to_server(WorkerService(state, lock), server)

    if tls_cert_file and tls_key_file:
        creds = create_server_credentials(cert_file=tls_cert_file, key_file=tls_key_file)
        server.add_secure_port(f"{host}:{port}", creds)
    else:
        server.add_insecure_port(f"{host}:{port}")

    server.start()
    return server


def ping_worker(
    *,
    target: str,
    caller_role: str,
    leader_epoch: int,
    tls_cert_file: str,
    tls_key_file: str,
    timeout_seconds: float = 0.5,
) -> worker_pb2.PingResponse:
    if tls_cert_file and tls_key_file:
        creds = create_channel_credentials(cert_file=tls_cert_file, key_file=tls_key_file)
        channel = grpc.secure_channel(target, creds)
    else:
        channel = grpc.insecure_channel(target)

    stub = worker_pb2_grpc.WorkerServiceStub(channel)
    try:
        return stub.Ping(
            worker_pb2.PingRequest(caller_role=caller_role, leader_epoch=int(leader_epoch)),
            timeout=timeout_seconds,
        )
    finally:
        channel.close()


def get_status_worker(
    *,
    target: str,
    leader_epoch: int,
    tls_cert_file: str,
    tls_key_file: str,
    timeout_seconds: float = 0.5,
) -> worker_pb2.GetStatusResponse:
    if tls_cert_file and tls_key_file:
        creds = create_channel_credentials(cert_file=tls_cert_file, key_file=tls_key_file)
        channel = grpc.secure_channel(target, creds)
    else:
        channel = grpc.insecure_channel(target)

    stub = worker_pb2_grpc.WorkerServiceStub(channel)
    try:
        return stub.GetStatus(
            worker_pb2.GetStatusRequest(leader_epoch=int(leader_epoch)),
            timeout=timeout_seconds,
        )
    finally:
        channel.close()


def start_job_on_worker(
    *,
    target: str,
    leader_epoch: int,
    job_run_id: str,
    command_name: str,
    args_json: str,
    timeout_seconds: int,
    attempt: int,
    tls_cert_file: str,
    tls_key_file: str,
    timeout_rpc_seconds: float = 1.0,
) -> worker_pb2.StartJobResponse:
    if tls_cert_file and tls_key_file:
        creds = create_channel_credentials(cert_file=tls_cert_file, key_file=tls_key_file)
        channel = grpc.secure_channel(target, creds)
    else:
        channel = grpc.insecure_channel(target)

    stub = worker_pb2_grpc.WorkerServiceStub(channel)
    try:
        return stub.StartJob(
            worker_pb2.StartJobRequest(
                leader_epoch=int(leader_epoch),
                job_run_id=str(job_run_id),
                command_name=str(command_name),
                args_json=str(args_json or "{}"),
                timeout_seconds=int(timeout_seconds or 0),
                attempt=int(attempt or 0),
            ),
            timeout=timeout_rpc_seconds,
        )
    finally:
        channel.close()


def cancel_job_on_worker(
    *,
    target: str,
    leader_epoch: int,
    job_run_id: str,
    reason: str,
    tls_cert_file: str,
    tls_key_file: str,
    timeout_rpc_seconds: float = 1.0,
) -> worker_pb2.CancelJobResponse:
    if tls_cert_file and tls_key_file:
        creds = create_channel_credentials(cert_file=tls_cert_file, key_file=tls_key_file)
        channel = grpc.secure_channel(target, creds)
    else:
        channel = grpc.insecure_channel(target)

    stub = worker_pb2_grpc.WorkerServiceStub(channel)
    try:
        return stub.CancelJob(
            worker_pb2.CancelJobRequest(
                leader_epoch=int(leader_epoch),
                job_run_id=str(job_run_id),
                reason=str(reason or ""),
            ),
            timeout=timeout_rpc_seconds,
        )
    finally:
        channel.close()


def reload_config_on_worker(
    *,
    target: str,
    leader_epoch: int,
    requested_by: str,
    tls_cert_file: str,
    tls_key_file: str,
    timeout_rpc_seconds: float = 1.0,
) -> worker_pb2.ReloadConfigResponse:
    if tls_cert_file and tls_key_file:
        creds = create_channel_credentials(cert_file=tls_cert_file, key_file=tls_key_file)
        channel = grpc.secure_channel(target, creds)
    else:
        channel = grpc.insecure_channel(target)

    stub = worker_pb2_grpc.WorkerServiceStub(channel)
    try:
        return stub.ReloadConfig(
            worker_pb2.ReloadConfigRequest(
                leader_epoch=int(leader_epoch),
                requested_by=str(requested_by or ""),
            ),
            timeout=timeout_rpc_seconds,
        )
    finally:
        channel.close()
