from __future__ import annotations

from typing import Any


def ops_version(request) -> dict[str, Any]:
    try:
        from scheduler_project import __version__ as project_version
    except Exception:
        project_version = "unknown"

    return {"ops_version": project_version}
