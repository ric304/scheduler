from __future__ import annotations

import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class PortRange:
    start: int
    end: int


def _normalize_host(host: str) -> str:
    # socket.bind expects a concrete interface address. For "0.0.0.0", probe on all interfaces.
    # For empty host, default to 127.0.0.1.
    if not host:
        return "127.0.0.1"
    return host


def find_available_tcp_port(*, host: str, port_range: PortRange) -> int:
    """Find an available TCP port by attempting to bind within [start, end].

    Notes:
    - This is best-effort (race condition exists) but works well for local dev.
    - We keep the socket open only during the check; the caller binds again later.
    """

    if port_range.start <= 0 or port_range.end <= 0:
        raise ValueError("Port range must be positive")
    if port_range.end < port_range.start:
        raise ValueError("Port range end must be >= start")

    bind_host = _normalize_host(host)

    for port in range(port_range.start, port_range.end + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_host, port))
            return port
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass

    raise RuntimeError(f"No available port in range {port_range.start}-{port_range.end} for host={bind_host}")
