"""Tests for satgonetem.traffic.flow_scheduler."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.traffic.flow_scheduler import FlowScheduler, FlowSchedulerStatus


def _make_flow(name: str, delay: float = 0.0) -> MagicMock:
    """Build a minimal mock flow that looks like a scheduler flow."""
    flow = MagicMock()
    flow.delay = delay
    flow.source.name = name
    flow.destination.name = "dst"
    flow._thread = None
    flow.is_alive.return_value = False
    return flow


class TestFlowSchedulerSaveResults:
    """Tests for the save_results memory-saving flag."""

    def test_default_saves_results(self):
        flow = _make_flow("flow0")
        flow.results.return_value = "result0"

        scheduler = FlowScheduler([flow], max_workers=1)
        scheduler.run()
        scheduler.join(timeout=5.0)

        assert scheduler.status() == FlowSchedulerStatus.DONE
        assert scheduler.results(flow) == "result0"
        assert scheduler.errors() == []

    def test_save_results_false_does_not_retain_results(self):
        flow = _make_flow("flow0")
        flow.results.return_value = "result0"
        flow._result = "result0"

        scheduler = FlowScheduler([flow], max_workers=1, save_results=False)
        scheduler.run()
        scheduler.join(timeout=5.0)

        assert scheduler.status() == FlowSchedulerStatus.DONE
        with pytest.raises(RuntimeError, match="save_results=False"):
            scheduler.results(flow)
        assert scheduler.errors() == []
        flow.discard_result.assert_called_once_with()

    def test_save_results_false_still_records_errors(self):
        flow = _make_flow("flow0")
        flow.start.side_effect = RuntimeError("boom")

        scheduler = FlowScheduler([flow], max_workers=1, save_results=False)
        scheduler.run()
        scheduler.join(timeout=5.0)

        assert scheduler.status() == FlowSchedulerStatus.DONE
        assert len(scheduler.errors()) == 1
        assert "boom" in str(scheduler.errors()[0])


class _ThreadFactory:
    """Return a fake thread for the debug progress printer only."""

    def __init__(self) -> None:
        self.progress_threads: list[MagicMock] = []

    def __call__(self, *args, **kwargs):
        target = kwargs.get("target")
        if getattr(target, "__name__", "") == "_progress_printer":
            thread = MagicMock()
            self.progress_threads.append(thread)
            return thread
        return threading.Thread(*args, **kwargs)


class _FailingExecutor:
    """Executor whose first submit fails after the progress thread starts."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def submit(self, *args, **kwargs):
        raise RuntimeError("submit failed")


def test_scheduler_error_stops_the_debug_progress_thread():
    """A fatal scheduler error must not leave the daemon printer running."""
    flow = _make_flow("flow0")
    scheduler = FlowScheduler([flow], max_workers=1, debug=True)
    progress_event = MagicMock()
    thread_factory = _ThreadFactory()

    with (
        patch(
            "satgonetem.traffic.flow_scheduler.threading",
            SimpleNamespace(Event=lambda: progress_event, Thread=thread_factory),
        ),
        patch(
            "satgonetem.traffic.flow_scheduler.ThreadPoolExecutor",
            _FailingExecutor,
        ),
    ):
        scheduler.run()
        scheduler.join(timeout=5.0)

    assert scheduler.status() == FlowSchedulerStatus.ERROR
    assert "submit failed" in str(scheduler._scheduler_error)
    assert thread_factory.progress_threads, "progress thread was not started"
    progress_event.set.assert_called_once_with()
    thread_factory.progress_threads[0].join.assert_called_once_with(timeout=2.0)
