"""Lightweight stage timing helpers for topology update paths.

The timings are intentionally small and dependency-free: they are collected
inside ``TopologyManager.update_simulation()`` and exposed through
``TopologyManager.get_last_stage_timings()`` for benchmarks and diagnostics.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from time import perf_counter
from typing import Iterator


class StageTimings:
    """Accumulate named durations for one unit of work, usually one tick.

    A stage can be measured with :meth:`measure` or recorded directly with
    :meth:`record`. Repeating a name accumulates its duration.
    """

    def __init__(self) -> None:
        self._durations: dict[str, float] = {}
        self._lock = threading.Lock()

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        """Measure the wall time of the enclosed block under ``name``."""
        started_at = perf_counter()
        try:
            yield
        finally:
            self.record(name, perf_counter() - started_at)

    def record(self, name: str, seconds: float) -> None:
        """Add ``seconds`` to ``name``; negative values are clamped to zero."""
        elapsed = max(0.0, float(seconds))
        with self._lock:
            self._durations[name] = self._durations.get(name, 0.0) + elapsed

    def as_dict(self) -> dict[str, float]:
        """Return a copy of the recorded durations, in seconds."""
        with self._lock:
            return dict(self._durations)

    def reset(self) -> None:
        """Drop every recorded duration."""
        with self._lock:
            self._durations.clear()


@contextmanager
def measure_stage(owner: object, name: str) -> Iterator[None]:
    """Measure a stage on ``owner`` when it exposes ``stage_timings``.

    Lightweight test harnesses that compose the mixins without a full
    ``TopologyManager`` do not have a timer; for those the context manager is a
    no-op.
    """
    timings = getattr(owner, "stage_timings", None)
    if timings is None:
        yield
        return
    with timings.measure(name):
        yield
