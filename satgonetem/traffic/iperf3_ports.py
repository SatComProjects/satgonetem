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

# Serializes allocation, release, and pool reconfiguration. A reconfiguration
# can therefore never swap the pool between the "is anything in use?" check
# and the swap while another thread is handing out a port from the old pool.
_RECONFIG_LOCK = threading.RLock()


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
                    self._base_port if port >= self._max_port else port + 1
                )
                if port not in self._in_use:
                    self._in_use.add(port)
                    return port
        raise RuntimeError(
            f"no free iperf3 port in range {self._base_port}-{self._max_port}"
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


class _Iperf3PortPool:
    """Stable process-wide handle for the configurable iperf3 port pool.

    The module-level reference never changes in production, so an allocation
    always resolves the current inner allocator while holding the
    reconfiguration lock. A range change swaps that inner allocator only when
    no port is in use, and no allocation can interleave with the swap.
    """

    def __init__(self) -> None:
        self._allocator: _Iperf3PortAllocator | None = None

    def _current(self) -> _Iperf3PortAllocator:
        """Return the active allocator, parsing the environment lazily."""
        if self._allocator is None:
            self._allocator = _Iperf3PortAllocator(*_port_range_from_env())
        return self._allocator

    def allocate(self) -> int:
        """Reserve a port from the currently configured pool."""
        with _RECONFIG_LOCK:
            return self._current().allocate()

    def release(self, port: int) -> None:
        """Return *port* to the currently configured pool."""
        with _RECONFIG_LOCK:
            self._current().release(port)

    @property
    def in_use_ports(self) -> set[int]:
        """Return a snapshot of the currently reserved ports."""
        with _RECONFIG_LOCK:
            return self._current().in_use_ports

    @property
    def _base_port(self) -> int:
        with _RECONFIG_LOCK:
            return self._current()._base_port

    @property
    def _max_port(self) -> int:
        with _RECONFIG_LOCK:
            return self._current()._max_port

    def set_range(self, base_port: int, max_port: int) -> None:
        """Replace the inner allocator atomically.

        A malformed environment is never parsed here: setting an explicit
        range must work even when the environment override is invalid.
        """
        with _RECONFIG_LOCK:
            current = self._allocator
            in_use = current.in_use_ports if current is not None else set()
            if in_use:
                raise RuntimeError(
                    f"cannot change the iperf3 port range while {len(in_use)} "
                    "flow(s) hold ports"
                )
            self._allocator = _Iperf3PortAllocator(base_port, max_port)


_IPERF3_PORT_ALLOCATOR: _Iperf3PortPool | _Iperf3PortAllocator = _Iperf3PortPool()


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
    current = _IPERF3_PORT_ALLOCATOR
    if isinstance(current, _Iperf3PortPool):
        current.set_range(base_port, max_port)
        return

    # Tests and embedders may install a plain allocator. Preserve that
    # replacement path while keeping the check-and-swap atomic.
    new_allocator = _Iperf3PortAllocator(base_port, max_port)
    with _RECONFIG_LOCK:
        in_use = current.in_use_ports
        if in_use:
            raise RuntimeError(
                f"cannot change the iperf3 port range while {len(in_use)} "
                "flow(s) hold ports"
            )
        _IPERF3_PORT_ALLOCATOR = new_allocator


def get_iperf3_port_range() -> tuple[int, int]:
    """Return the (first, last) ports of the process-wide iperf3 pool."""
    return _IPERF3_PORT_ALLOCATOR._base_port, _IPERF3_PORT_ALLOCATOR._max_port
