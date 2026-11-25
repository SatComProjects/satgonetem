"""Tests for the typed connectivity matrix."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from satgonetem.connectivity import (
    ConnectivityMatrix,
    ConnectivityResult,
    build_connectivity_matrix,
)


@dataclass
class _Result:
    reachable: bool = True
    packets_transmitted: int = 10
    packets_received: int = 10
    packet_loss_percent: float = 0.0
    rtt_min_ms: float = 1.0
    rtt_avg_ms: float = 2.0
    rtt_max_ms: float = 3.0


class _Flow:
    def __init__(self, source, destination, config):
        self.source = source
        self.destination = destination
        self.config = config
        self.delay = 0.0


class _Scheduler:
    def __init__(self, flows, max_workers=1, debug=False, flow_timeout_sec=None):
        self.flows = flows
        self.run_called = False
        self.join_called = False

    def run(self):
        self.run_called = True

    def join(self):
        self.join_called = True

    def results(self, flow):
        if flow.source.name == "Sat1":
            raise RuntimeError("unreachable")
        return _Result()


def _nodes():
    return [SimpleNamespace(name="Sat0"), SimpleNamespace(name="Gnd0")]


def test_builds_one_result_per_ordered_pair():
    matrix = build_connectivity_matrix(
        _nodes(), flow_factory=_Flow, scheduler_factory=_Scheduler
    )

    assert isinstance(matrix, ConnectivityMatrix)
    assert len(matrix.results) == 2
    assert [(r.source, r.destination) for r in matrix.results] == [
        ("Sat0", "Gnd0"),
        ("Gnd0", "Sat0"),
    ]
    assert matrix.to_dict()["summary"] == {
        "pairs": 2,
        "reachable": 2,
        "unreachable": 0,
    }


def test_failed_flow_becomes_unreachable_result():
    class FailingScheduler(_Scheduler):
        def results(self, flow):
            if flow.source.name == "Gnd0":
                raise RuntimeError("timeout")
            return _Result()

    matrix = build_connectivity_matrix(
        _nodes(), flow_factory=_Flow, scheduler_factory=FailingScheduler
    )

    failed = matrix.results[1]
    assert failed.reachable is False
    assert failed.packet_loss_percent == 100.0
    assert failed.error == "timeout"


def test_deduplicates_nodes_and_handles_single_node():
    matrix = build_connectivity_matrix(
        [SimpleNamespace(name="Sat0"), SimpleNamespace(name="Sat0")],
        flow_factory=_Flow,
        scheduler_factory=_Scheduler,
    )
    assert matrix.results == []


def test_connectivity_result_serialization():
    result = ConnectivityResult(
        source="Sat0",
        destination="Gnd0",
        reachable=True,
        packets_transmitted=10,
        packets_received=9,
        packet_loss_percent=10.0,
        rtt_min_ms=1.0,
        rtt_avg_ms=2.0,
        rtt_max_ms=3.0,
    )
    assert result.to_dict()["packet_loss_percent"] == 10.0
    assert "Sat0" in ConnectivityMatrix([result]).to_json()


def test_failed_flow_preserves_underlying_flow_exception_instead_of_keyerror():
    class FlowWithResults(_Flow):
        def results(self):
            raise TimeoutError("ping timed out")

    class KeyErrorScheduler(_Scheduler):
        def results(self, flow):
            if flow.source.name == "Gnd0":
                raise KeyError("No result found for flow")
            return _Result()

    matrix = build_connectivity_matrix(
        _nodes(), flow_factory=FlowWithResults, scheduler_factory=KeyErrorScheduler
    )

    failed = matrix.results[1]
    assert failed.reachable is False
    assert failed.error == "ping timed out"


def test_scheduler_error_status_raises_instead_of_masking_as_keyerrors():
    import pytest

    from satgonetem.traffic.flow_scheduler import FlowSchedulerStatus

    class BrokenScheduler(_Scheduler):
        def status(self):
            return FlowSchedulerStatus.ERROR

        def errors(self):
            raise RuntimeError("scheduler crashed")

    with pytest.raises(RuntimeError, match="scheduler crashed"):
        build_connectivity_matrix(
            _nodes(), flow_factory=_Flow, scheduler_factory=BrokenScheduler
        )
