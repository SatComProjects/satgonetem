from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")  # non-GUI backend


if TYPE_CHECKING:
    pass


# Default iperf3 server port pool: one port per concurrent flow of the
# process, so it must hold the largest number of flows run at once.
# SATGONETEM_IPERF3_PORT_RANGE ("first-last") or set_iperf3_port_range()
# override it.
_IPERF3_PORT_RANGE_START = 5201
_IPERF3_PORT_RANGE_END = 6200
_IPERF3_PORT_RANGE_ENV = "SATGONETEM_IPERF3_PORT_RANGE"
_IPERF3_SERVER_READY_TIMEOUT = 2.0
_IPERF3_SERVER_READY_POLL_INTERVAL = 0.1
_IPERF3_SERVER_START_FALLBACK = 0.5
# The server writes its JSON when its side of the test ends, which can be
# after the client returned: the receiver-side report is waited for.
_IPERF3_RESULT_WAIT_TIMEOUT = 3.0
_IPERF3_RESULT_POLL_INTERVAL = 0.25


class _Iperf3PortAllocator:
    """Thread-safe allocator of unique iperf3 server ports.

    A single allocator instance is shared process-wide so concurrent flows
    never bind the same destination port. Ports are handed out from a
    bounded range and released by each flow when it finishes.
    """

    def __init__(
        self,
        base_port: int = _IPERF3_PORT_RANGE_START,
        max_port: int = _IPERF3_PORT_RANGE_END,
    ) -> None:
        if not 1 <= base_port <= max_port <= 65535:
            raise ValueError(
                f"Invalid iperf3 port range {base_port}-{max_port}; "
                "expected 1 <= base_port <= max_port <= 65535"
            )
        self._base_port = base_port
        self._max_port = max_port
        self._next_port = base_port
        self._in_use: set[int] = set()
        self._lock = threading.Lock()

    def allocate(self) -> int:
        """Reserve and return a free port from the configured range.

        Scans forward from the last handed-out port and wraps around once.

        Raises:
            RuntimeError: If every port in the range is already in use.
        """
        with self._lock:
            span = self._max_port - self._base_port + 1
            for _ in range(span):
                port = self._next_port
                self._next_port = (
                    self._base_port
                    if port >= self._max_port
                    else port + 1
                )
                if port not in self._in_use:
                    self._in_use.add(port)
                    return port
        raise RuntimeError(
            f"no free iperf3 port in range "
            f"{self._base_port}-{self._max_port}"
        )

    def release(self, port: int) -> None:
        """Return *port* to the pool. Idempotent for unknown ports."""
        with self._lock:
            self._in_use.discard(port)

    @property
    def in_use_ports(self) -> set[int]:
        """Return a snapshot of the currently reserved ports."""
        with self._lock:
            return set(self._in_use)


def _port_range_from_env() -> tuple[int, int]:
    """Return the port range from SATGONETEM_IPERF3_PORT_RANGE, or the default.

    Raises:
        ValueError: If the variable is set but not "first-last".
    """
    raw = os.environ.get(_IPERF3_PORT_RANGE_ENV, "").strip()
    if not raw:
        return _IPERF3_PORT_RANGE_START, _IPERF3_PORT_RANGE_END
    try:
        first, last = (int(part) for part in raw.split("-"))
    except ValueError:
        raise ValueError(
            f"{_IPERF3_PORT_RANGE_ENV} must be 'first-last' (e.g. 5201-6200), "
            f"got {raw!r}"
        ) from None
    return first, last


_IPERF3_PORT_ALLOCATOR = _Iperf3PortAllocator(*_port_range_from_env())


def set_iperf3_port_range(base_port: int, max_port: int) -> None:
    """Replace the process-wide iperf3 server port pool.

    The pool holds one port per concurrent flow: size it for the largest
    number of iperf3 flows the process runs at once. Call it before the
    flows start.

    Raises:
        ValueError: If the range is invalid.
        RuntimeError: If flows currently hold ports from the current pool.
    """
    global _IPERF3_PORT_ALLOCATOR
    new_allocator = _Iperf3PortAllocator(base_port, max_port)
    in_use = _IPERF3_PORT_ALLOCATOR.in_use_ports
    if in_use:
        raise RuntimeError(
            f"cannot change the iperf3 port range while {len(in_use)} "
            "flow(s) hold ports"
        )
    _IPERF3_PORT_ALLOCATOR = new_allocator


def get_iperf3_port_range() -> tuple[int, int]:
    """Return the (first, last) ports of the process-wide iperf3 pool."""
    return _IPERF3_PORT_ALLOCATOR._base_port, _IPERF3_PORT_ALLOCATOR._max_port
