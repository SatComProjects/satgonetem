"""Iperf3 process readiness and output helpers."""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING, Any

from satgonetem.traffic.iperf3_ports import (
    _IPERF3_SERVER_READY_POLL_INTERVAL,
    _IPERF3_SERVER_READY_TIMEOUT,
    _IPERF3_SERVER_START_FALLBACK,
)

if TYPE_CHECKING:
    from satgonetem.models.node import Node


def _is_iperf3_server_listening(
    destination: Node, listen_ip: str, port: int
) -> bool | None:
    """Check whether an iperf3 server is listening on *listen_ip*:*port*.

    Uses the kernel listener table when available and falls back to a
    process check for containers without ``ss``.

    Returns:
        True when the listener/process was observed, False when it was not,
        or None when the probe could not be executed (e.g. a mocked node).
    """
    port_token = f"{listen_ip}:{port}"
    # Bracketed so pgrep -f cannot match the wrapper sh -c command line that
    # carries the pattern itself.
    process_pattern = f"[i]perf3 -s -1 -B {listen_ip} -p {port}"
    check_cmd = (
        f"ss -ltn 2>/dev/null | grep -F {shlex.quote(port_token)} "
        f"|| pgrep -f {shlex.quote(process_pattern)} 2>/dev/null"
    )
    try:
        output = destination.execute_command(check_cmd, ok_exit_codes=(0, 1))
    except TypeError:
        # Nodes with a reduced execute_command signature.
        try:
            output = destination.execute_command(check_cmd)
        except Exception:
            return None
    except Exception:
        return None
    if output is None:
        return None
    return bool(str(output).strip())


def _wait_for_iperf3_server(
    destination: Node,
    listen_ip: str,
    port: int,
    timeout: float = _IPERF3_SERVER_READY_TIMEOUT,
    poll_interval: float = _IPERF3_SERVER_READY_POLL_INTERVAL,
    fallback: float = _IPERF3_SERVER_START_FALLBACK,
) -> bool:
    """Wait for the destination iperf3 server to accept connections.

    Polls for at most *timeout* seconds. When the listener probe is not
    available at all, keeps the legacy fixed startup grace period instead.

    Returns:
        True when the server was observed listening, False otherwise.
    """
    deadline = time.monotonic() + timeout
    while True:
        listening = _is_iperf3_server_listening(destination, listen_ip, port)
        if listening is True:
            return True
        if listening is None:
            time.sleep(fallback)
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def _extract_iperf3_json(text: Any) -> str | None:
    """Return the JSON object in an iperf3 output file, or None.

    iperf3 writes everything to the redirected stream: warnings before the
    JSON object (e.g. "warning: UDP block size ... exceeds TCP MSS") and
    some errors after it. The object runs from the first "{" to the last "}".
    """
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


def _text_outside_iperf3_json(text: Any) -> str | None:
    """Return what iperf3 printed around its JSON object, or None."""
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    if not 0 <= start < end:
        return text.strip() or None
    outside = (text[:start] + " " + text[end + 1 :]).strip()
    return outside or None
