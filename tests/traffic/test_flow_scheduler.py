"""Tests for satgonetem.traffic.flow_scheduler."""

from unittest.mock import MagicMock

import pytest

from satgonetem.traffic.flow_scheduler import FlowScheduler, FlowSchedulerStatus


def _make_flow(name: str, delay: float = 0.0) -> MagicMock:
    """Build a minimal mock flow that looks like a scheduler flow."""
    flow = MagicMock()
    flow.delay = delay
    flow.source.name = name
    flow.destination.name = "dst"
    flow._thread = None
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

        scheduler = FlowScheduler([flow], max_workers=1, save_results=False)
        scheduler.run()
        scheduler.join(timeout=5.0)

        assert scheduler.status() == FlowSchedulerStatus.DONE
        with pytest.raises(RuntimeError, match="save_results=False"):
            scheduler.results(flow)
        assert scheduler.errors() == []

    def test_save_results_false_still_records_errors(self):
        flow = _make_flow("flow0")
        flow.start.side_effect = RuntimeError("boom")

        scheduler = FlowScheduler([flow], max_workers=1, save_results=False)
        scheduler.run()
        scheduler.join(timeout=5.0)

        assert scheduler.status() == FlowSchedulerStatus.DONE
        assert len(scheduler.errors()) == 1
        assert "boom" in str(scheduler.errors()[0])
