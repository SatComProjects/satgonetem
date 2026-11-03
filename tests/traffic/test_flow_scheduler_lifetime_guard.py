"""Tests for the FlowScheduler lifetime admission guard."""

from __future__ import annotations

from unittest.mock import MagicMock

from satgonetem.traffic.flow_scheduler import FlowScheduler, FlowSchedulerStatus


def _make_flow(name: str) -> MagicMock:
    flow = MagicMock()
    flow.delay = 0.0
    flow.source.name = name
    flow.destination.name = "dst"
    flow._thread = None
    return flow


def test_flow_guard_skips_rejected_flows():
    allowed = _make_flow("allowed")
    blocked = _make_flow("blocked")

    scheduler = FlowScheduler(
        [allowed, blocked],
        max_workers=1,
        flow_guard=lambda flow: flow is allowed,
    )
    scheduler.run()
    scheduler.join(timeout=5.0)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert scheduler.skipped_count() == 1
    assert scheduler.errors() == []
    allowed.start.assert_called_once()
    blocked.start.assert_not_called()


def test_flow_guard_errors_are_recorded_and_other_flows_run():
    allowed = _make_flow("allowed")
    failing = _make_flow("failing")

    def guard(flow):
        if flow is failing:
            raise RuntimeError("lifetime lookup failed")
        return True

    scheduler = FlowScheduler([allowed, failing], max_workers=1, flow_guard=guard)
    scheduler.run()
    scheduler.join(timeout=5.0)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert scheduler.skipped_count() == 0
    assert len(scheduler.errors()) == 1
    assert "lifetime lookup failed" in str(scheduler.errors()[0])
    allowed.start.assert_called_once()
    failing.start.assert_not_called()
