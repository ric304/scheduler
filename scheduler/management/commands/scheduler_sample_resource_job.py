from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from django.core.management.base import BaseCommand


@dataclass(frozen=True)
class _Params:
    cpu_seconds: float
    io_write_mb: int
    io_read_mb: int
    chunk_kb: int
    keep_file: bool
    file_path: str | None


def _parse_args_json_env() -> dict:
    raw = os.environ.get("SCHEDULER_ARGS_JSON", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _coerce_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _coerce_float(v, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _load_params(options: dict) -> _Params:
    env = _parse_args_json_env()

    cpu_seconds = options.get("cpu_seconds")
    io_write_mb = options.get("io_write_mb")
    io_read_mb = options.get("io_read_mb")
    chunk_kb = options.get("chunk_kb")
    keep_file = bool(options.get("keep_file"))
    file_path = options.get("file_path")

    # Allow the scheduler runtime env var to override.
    if "cpu_seconds" in env:
        cpu_seconds = env.get("cpu_seconds")
    if "io_write_mb" in env:
        io_write_mb = env.get("io_write_mb")
    if "io_read_mb" in env:
        io_read_mb = env.get("io_read_mb")
    if "chunk_kb" in env:
        chunk_kb = env.get("chunk_kb")
    if "keep_file" in env:
        keep_file = bool(env.get("keep_file"))
    if "file_path" in env:
        file_path = env.get("file_path")

    cpu_seconds_f = max(0.0, _coerce_float(cpu_seconds, 3.0))
    io_write_mb_i = max(0, _coerce_int(io_write_mb, 10))
    io_read_mb_i = max(0, _coerce_int(io_read_mb, 10))
    chunk_kb_i = min(1024, max(4, _coerce_int(chunk_kb, 256)))

    return _Params(
        cpu_seconds=cpu_seconds_f,
        io_write_mb=io_write_mb_i,
        io_read_mb=io_read_mb_i,
        chunk_kb=chunk_kb_i,
        keep_file=keep_file,
        file_path=str(file_path) if file_path else None,
    )


def _burn_cpu(seconds: float) -> int:
    if seconds <= 0:
        return 0

    # Pure-Python integer mixing loop so CPU time is visible in psutil.
    end = time.perf_counter() + float(seconds)
    x = 0x12345678
    iters = 0
    while time.perf_counter() < end:
        # Do some work per outer loop to reduce perf_counter overhead.
        for _ in range(200_000):
            x = (x * 1664525 + 1013904223) & 0xFFFFFFFF
        iters += 1
    return iters


def _maybe_posix_fadvise(fd: int, advice: int) -> None:
    fn = getattr(os, "posix_fadvise", None)
    if fn is None:
        return
    try:
        fn(fd, 0, 0, advice)
    except Exception:
        return


def _write_file(path: str, total_bytes: int, chunk_bytes: int) -> int:
    if total_bytes <= 0:
        # Still create/overwrite the file so read can work if requested.
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return 0

    chunk = (b"SCHEDULER_IO_TEST_" * ((chunk_bytes // 16) + 1))[:chunk_bytes]

    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    written = 0
    try:
        # Hint that we will write sequentially (best-effort).
        _maybe_posix_fadvise(fd, getattr(os, "POSIX_FADV_SEQUENTIAL", 2))

        while written < total_bytes:
            n = min(chunk_bytes, total_bytes - written)
            os.write(fd, chunk[:n])
            written += n

        os.fsync(fd)
    finally:
        os.close(fd)
    return written


def _read_file(path: str, total_bytes: int, chunk_bytes: int) -> int:
    if total_bytes <= 0:
        return 0

    fd = os.open(path, os.O_RDONLY)
    read_total = 0
    try:
        # Try to reduce page cache effects so read_bytes is more likely to move.
        _maybe_posix_fadvise(fd, getattr(os, "POSIX_FADV_DONTNEED", 4))
        _maybe_posix_fadvise(fd, getattr(os, "POSIX_FADV_SEQUENTIAL", 2))

        while read_total < total_bytes:
            n = min(chunk_bytes, total_bytes - read_total)
            b = os.read(fd, n)
            if not b:
                break
            read_total += len(b)
    finally:
        os.close(fd)
    return read_total


class Command(BaseCommand):
    help = (
        "Sample job that generates CPU load and file IO (read/write). "
        "Use for testing per-job CPU/IO metrics."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--cpu-seconds",
            type=float,
            default=3.0,
            help="CPU busy-loop duration in seconds (default: 3).",
        )
        parser.add_argument(
            "--io-write-mb",
            type=int,
            default=10,
            help="Bytes to write in MiB (default: 10).",
        )
        parser.add_argument(
            "--io-read-mb",
            type=int,
            default=10,
            help="Bytes to read in MiB (default: 10).",
        )
        parser.add_argument(
            "--chunk-kb",
            type=int,
            default=256,
            help="IO chunk size in KiB (default: 256).",
        )
        parser.add_argument(
            "--keep-file",
            action="store_true",
            default=False,
            help="Keep the temporary IO file (default: delete).",
        )
        parser.add_argument(
            "--file-path",
            default=None,
            help="Optional path for the IO file (default: temp file).",
        )

    def handle(self, *args, **options):
        p = _load_params(options)

        started = datetime.now(timezone.utc)
        self.stdout.write(
            "scheduler_sample_resource_job start "
            + f"utc={started.isoformat()} cpu_seconds={p.cpu_seconds} "
            + f"io_write_mb={p.io_write_mb} io_read_mb={p.io_read_mb} chunk_kb={p.chunk_kb}"
        )

        # CPU
        cpu_iters = _burn_cpu(p.cpu_seconds)

        # IO
        chunk_bytes = int(p.chunk_kb) * 1024
        max_mb = max(int(p.io_write_mb), int(p.io_read_mb))
        if p.file_path:
            path = p.file_path
        else:
            fd, path = tempfile.mkstemp(prefix="scheduler_io_test_", suffix=".bin")
            os.close(fd)

        write_bytes = int(p.io_write_mb) * 1024 * 1024
        read_bytes = int(p.io_read_mb) * 1024 * 1024

        # Ensure file is large enough for the requested read.
        _write_file(path, max(write_bytes, read_bytes, max_mb * 1024 * 1024), chunk_bytes)
        wrote = _write_file(path, write_bytes, chunk_bytes)
        read = _read_file(path, read_bytes, chunk_bytes)

        if not p.keep_file:
            try:
                os.remove(path)
            except Exception:
                pass

        finished = datetime.now(timezone.utc)
        self.stdout.write(
            "scheduler_sample_resource_job done "
            + f"utc={finished.isoformat()} cpu_iters={cpu_iters} "
            + f"io_wrote_bytes={wrote} io_read_bytes={read} "
            + (f"file={path} kept=1" if p.keep_file else "file_deleted=1")
        )
