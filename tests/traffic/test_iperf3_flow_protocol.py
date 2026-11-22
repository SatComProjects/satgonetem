"""Iperf3Flow must satisfy the shared flow protocol used by FlowScheduler.

The scheduler interacts with flows through ``start``, ``join``, ``is_alive``,
``results`` and ``discard_result``. An Iperf3Flow that only happens to look
like a flow used to fail inside a scheduler worker with ``AttributeError``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.traffic.flow import Flow
from satgonetem.traffic.flow_scheduler import FlowScheduler, FlowSchedulerStatus
from satgonetem.traffic.iperf3_config import Iperf3Config
from satgonetem.traffic.iperf3_flow import FlowStatus, Iperf3Flow
from satgonetem.traffic.iperf3_results import Iperf3Results

pytestmark = pytest.mark.unit

_TCP_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "TCP", "duration": 1, "num_streams": 1}},
        "intervals": [
            {
                "streams": [],
                "sum": {
                    "start": 0.0,
                    "end": 1.0,
                    "bits_per_second": 1_000_000.0,
                    "bytes": 125_000,
                },
            }
        ],
        "end": {
            "sum_sent": {"bytes": 125_000},
            "sum_received": {"bytes": 125_000},
        },
    }
)


def _make_node(name: str, ipv4: str) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.loopback = MagicMock()
    node.loopback.ipv4 = ipv4
    return node


def _make_flow() -> Iperf3Flow:
    source = _make_node("gs0", "10.0.0.1")
    destination = _make_node("gs1", "10.0.0.2")

    def source_command(command, **_kwargs):
        return _TCP_JSON if command.startswith("cat ") else ""

    def destination_command(command, **_kwargs):
        return ""

    source.execute_command.side_effect = source_command
    destination.execute_command.side_effect = destination_command
    return Iperf3Flow(source, destination, Iperf3Config(duration=1))


def _run_through_scheduler(save_results: bool = True):
    flow = _make_flow()
    scheduler = FlowScheduler([flow], max_workers=1, save_results=save_results)
    with (
        patch(
            "satgonetem.traffic.iperf3_flow._wait_for_iperf3_server",
            return_value=True,
        ),
        patch("satgonetem.traffic.iperf3_flow.time.sleep"),
    ):
        scheduler.run()
        scheduler.join(timeout=5.0)
    return flow, scheduler


def test_iperf3_flow_satisfies_the_flow_protocol() -> None:
    flow = _make_flow()

    assert isinstance(flow, Flow)
    assert callable(flow.join)
    assert callable(flow.is_alive)
    assert callable(flow.discard_result)


def test_scheduler_executes_an_iperf3_flow_end_to_end() -> None:
    flow, scheduler = _run_through_scheduler()

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert scheduler.errors() == []
    assert flow.status() == FlowStatus.DONE
    result = scheduler.results(flow)
    assert isinstance(result, Iperf3Results)


def test_scheduler_save_results_false_discards_iperf3_result() -> None:
    flow, scheduler = _run_through_scheduler(save_results=False)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert scheduler.errors() == []
    assert flow.status() == FlowStatus.DONE
    assert flow._result is None
    with pytest.raises(RuntimeError, match="save_results=False"):
        scheduler.results(flow)
