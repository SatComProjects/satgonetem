"""Contract tests for ``satgonetem.traffic.flow_scheduler.FlowScheduler``.

These tests pin the documented, externally observable behavior of the
scheduler so later refactors can be validated against it. Flows are built
from ``tests.support.RecordingFlow`` and never invoke external traffic tools.
"""

from __future__ import annotations

from typing import Any

import pytest

from satgonetem.traffic.flow_scheduler import FlowScheduler, FlowSchedulerStatus
from tests.support import RecordingFlow, make_ground_station, make_satellite

pytestmark = pytest.mark.unit

_JOIN_TIMEOUT_SEC = 5.0


class _StartRecordingFlow(RecordingFlow):
    """RecordingFlow that appends itself to a shared start-order log."""

    def __init__(
        self,
        source: Any,
        destination: Any,
        *,
        start_log: list[RecordingFlow] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(source, destination, **kwargs)
        self._start_log = start_log

    def start(self) -> None:
        if self._start_log is not None:
            self._start_log.append(self)
        super().start()


def _flow(
    name: str,
    *,
    delay: float = 0.0,
    duration: float = 0.0,
    result: Any = None,
    error: Exception | None = None,
    start_log: list[RecordingFlow] | None = None,
) -> RecordingFlow:
    """Build a deterministic recording flow between shared test nodes."""
    return _StartRecordingFlow(
        make_ground_station(name),
        make_satellite(f"Sat{name[3:]}"),
        delay=delay,
        duration=duration,
        result=result,
        error=error,
        start_log=start_log,
    )


@pytest.mark.parametrize("max_workers", [0, -1, -100])
def test_constructor_rejects_max_workers_below_one(max_workers: int) -> None:
    """max_workers must be a positive pool size."""
    with pytest.raises(ValueError) as excinfo:
        FlowScheduler([_flow("Gnd0")], max_workers=max_workers)

    assert str(excinfo.value) == f"max_workers must be >= 1, got {max_workers}"


@pytest.mark.parametrize("flow_timeout_sec", [0, 0.0, -0.01, -1])
def test_constructor_rejects_non_positive_flow_timeout(
    flow_timeout_sec: float,
) -> None:
    """An explicit flow timeout must be strictly positive."""
    with pytest.raises(ValueError) as excinfo:
        FlowScheduler([_flow("Gnd0")], flow_timeout_sec=flow_timeout_sec)

    assert str(excinfo.value) == (
        f"flow_timeout_sec must be > 0 when provided, got {flow_timeout_sec}"
    )


def test_constructor_is_idle_and_sorts_flows_by_delay() -> None:
    """Construction does not start work and orders flows by ascending delay."""
    late = _flow("Gnd0", delay=0.2)
    first = _flow("Gnd1", delay=0.0)
    middle = _flow("Gnd2", delay=0.1)

    scheduler = FlowScheduler([late, first, middle])

    assert scheduler.status() == FlowSchedulerStatus.IDLE
    assert scheduler._flows == [first, middle, late]
    assert scheduler._max_workers == 100
    assert scheduler._flow_timeout_sec is None


def test_run_after_completion_raises_runtime_error() -> None:
    """A scheduler is single-use; a completed run cannot be restarted."""
    scheduler = FlowScheduler([_flow("Gnd0", result="done")], max_workers=1)
    scheduler.run()
    scheduler.join(timeout=_JOIN_TIMEOUT_SEC)
    assert scheduler.status() == FlowSchedulerStatus.DONE

    with pytest.raises(RuntimeError, match="already been started"):
        scheduler.run()

    assert scheduler.status() == FlowSchedulerStatus.DONE


def test_results_before_run_raises_key_error() -> None:
    """No flow has a result until run() has executed it."""
    flow = _flow("Gnd0")
    scheduler = FlowScheduler([flow])

    with pytest.raises(KeyError, match="No result found"):
        scheduler.results(flow)


def test_running_scheduler_rejects_errors_results_and_second_run() -> None:
    """While RUNNING, errors()/results()/run() are guarded."""
    flow = _flow("Gnd0", duration=0.2, result="finished")
    scheduler = FlowScheduler([flow], max_workers=1)
    scheduler.run()
    try:
        assert scheduler.status() == FlowSchedulerStatus.RUNNING
        with pytest.raises(RuntimeError, match="still running"):
            scheduler.errors()
        with pytest.raises(RuntimeError, match="still running"):
            scheduler.results(flow)
        with pytest.raises(RuntimeError, match="already been started"):
            scheduler.run()
    finally:
        scheduler.join(timeout=_JOIN_TIMEOUT_SEC)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert scheduler.results(flow) == "finished"


def test_successful_run_executes_in_delay_order_and_stores_results() -> None:
    """Flows start at their delays and every successful result is retrievable."""
    start_log: list[RecordingFlow] = []
    last = _flow("Gnd0", delay=0.2, result="last", start_log=start_log)
    first = _flow("Gnd1", delay=0.0, result="first", start_log=start_log)
    middle = _flow("Gnd2", delay=0.1, result="middle", start_log=start_log)

    scheduler = FlowScheduler([last, first, middle], max_workers=3)
    scheduler.run()
    scheduler.join(timeout=_JOIN_TIMEOUT_SEC)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert start_log == [first, middle, last]
    assert scheduler.results(first) == "first"
    assert scheduler.results(middle) == "middle"
    assert scheduler.results(last) == "last"
    assert scheduler.errors() == []


def test_completed_count_tracks_flows_only_in_debug_mode() -> None:
    """Completion accounting hooks are wired only when debug=True."""
    plain_flows = [_flow("Gnd0"), _flow("Gnd1")]
    plain = FlowScheduler(plain_flows, max_workers=2)
    plain.run()
    plain.join(timeout=_JOIN_TIMEOUT_SEC)

    assert plain.status() == FlowSchedulerStatus.DONE
    assert plain._completed_count == 0

    debug_flows = [_flow("Gnd2"), _flow("Gnd3")]
    debug = FlowScheduler(debug_flows, max_workers=2, debug=True)
    debug.run()
    debug.join(timeout=_JOIN_TIMEOUT_SEC)

    assert debug.status() == FlowSchedulerStatus.DONE
    assert debug._completed_count == len(debug_flows)


def test_failed_flow_is_recorded_and_successful_results_remain() -> None:
    """A failing flow is reported without discarding other flows' results."""
    ok_result = object()
    failure = RuntimeError("boom")
    ok = _flow("Gnd0", result=ok_result)
    bad = _flow("Gnd1", error=failure)

    scheduler = FlowScheduler([bad, ok], max_workers=2)
    scheduler.run()
    scheduler.join(timeout=_JOIN_TIMEOUT_SEC)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert scheduler.errors() == [failure]
    assert scheduler.results(ok) is ok_result
    with pytest.raises(KeyError, match="No result found"):
        scheduler.results(bad)


def test_save_results_false_rejects_results_and_clears_flow_result() -> None:
    """save_results=False drops successful results but keeps flow errors."""
    ok = _flow("Gnd0", result="discarded")
    bad = _flow("Gnd1", error=RuntimeError("boom"))

    scheduler = FlowScheduler([ok, bad], max_workers=1, save_results=False)
    scheduler.run()
    scheduler.join(timeout=_JOIN_TIMEOUT_SEC)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    with pytest.raises(RuntimeError, match="save_results=False"):
        scheduler.results(ok)
    errors = scheduler.errors()
    assert len(errors) == 1
    assert str(errors[0]) == "boom"
    # The flow's own result reference is released after completion.
    assert ok._result is None


def test_timeout_stops_flow_and_records_timeout_error() -> None:
    """A flow outliving flow_timeout_sec is stopped and reported as failed."""
    flow = _flow("Gnd0", duration=0.2, result="too-late")
    scheduler = FlowScheduler([flow], max_workers=1, flow_timeout_sec=0.05)

    scheduler.run()
    scheduler.join(timeout=_JOIN_TIMEOUT_SEC)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert flow.stopped is True
    errors = scheduler.errors()
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)
    assert "Flow timed out after 0.05s" in str(errors[0])
    with pytest.raises(KeyError, match="No result found"):
        scheduler.results(flow)


def test_join_timeout_returns_and_status_is_stable() -> None:
    """join(timeout) returns promptly and does not alter lifecycle state."""
    flow = _flow("Gnd0", duration=0.05, result="done")
    scheduler = FlowScheduler([flow], max_workers=1)

    scheduler.join(timeout=0.01)
    assert scheduler.status() == FlowSchedulerStatus.IDLE

    scheduler.run()
    scheduler.join(timeout=_JOIN_TIMEOUT_SEC)
    assert scheduler.status() == FlowSchedulerStatus.DONE

    scheduler.join(timeout=0.01)
    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert scheduler.results(flow) == "done"
