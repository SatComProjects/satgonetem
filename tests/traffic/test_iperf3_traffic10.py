"""Regression tests for TRAFFIC-10.

An iperf3 flow must always reach a terminal status (DONE or ERROR), even
when setup fails before the transport commands run or when best-effort
cleanup raises. These tests mock the nodes; no real iperf3 or container is
used.
"""

import json
from unittest.mock import MagicMock, patch

from satgonetem.traffic.iperf3_utils import (
    FlowStatus,
    Iperf3Config,
    Iperf3Flow,
    Iperf3Results,
)

_VALID_TCP_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "TCP",
                "duration": 1,
                "num_streams": 1,
            }
        },
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


def _join(flow: Iperf3Flow, timeout: float = 3.0) -> FlowStatus:
    """Join the flow's background thread and return its final status."""
    assert flow._thread is not None
    flow._thread.join(timeout)
    assert not flow._thread.is_alive(), "flow thread did not finish"
    return flow.status()


def _kill_calls(node: MagicMock) -> list:
    return [
        call
        for call in node.execute_command.call_args_list
        if "pkill" in str(call) and "iperf3 -s -1" in str(call)
    ]


def test_missing_destination_loopback_sets_error_and_tries_server_kill():
    """Setup failure before the try block must not leave the flow RUNNING."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")
    del dst.loopback  # simulate an unset/missing destination loopback
    src.execute_command.return_value = None
    dst.execute_command.return_value = None

    flow = Iperf3Flow(src, dst, Iperf3Config(duration=1))

    with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
        flow.start()
        status = _join(flow)

    assert status == FlowStatus.ERROR
    assert status is not FlowStatus.RUNNING
    assert flow._error is not None
    assert _kill_calls(dst), "detached server kill was not attempted"


def test_cleanup_failure_does_not_prevent_done_result():
    """A failing cleanup rm must not discard a valid measurement."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")

    def source_command(cmd, detach=False):
        if cmd.startswith("cat "):
            return _VALID_TCP_JSON
        if cmd.startswith("rm -f"):
            raise RuntimeError("cleanup rm failed on source")
        return ""

    def destination_command(cmd, detach=False):
        if cmd.startswith("rm -f"):
            raise RuntimeError("cleanup rm failed on destination")
        return ""

    src.execute_command.side_effect = source_command
    dst.execute_command.side_effect = destination_command

    flow = Iperf3Flow(src, dst, Iperf3Config(duration=1))

    with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
        flow.start()
        status = _join(flow)

    assert status == FlowStatus.DONE
    result = flow.results()
    assert isinstance(result, Iperf3Results)
    assert result.protocol == "TCP"


def test_client_setup_failure_sets_error_and_kills_detached_server():
    """A failing client setup must terminate ERROR and reap the server."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")

    def source_command(cmd, detach=False):
        if "iperf3 -c" in cmd:
            raise RuntimeError("client setup failed")
        return ""

    src.execute_command.side_effect = source_command
    dst.execute_command.return_value = ""

    flow = Iperf3Flow(src, dst, Iperf3Config(duration=1, port=5201))

    with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
        flow.start()
        status = _join(flow)

    assert status == FlowStatus.ERROR
    assert flow._error is not None

    kills = _kill_calls(dst)
    assert kills, "detached server kill was not attempted"
    # The kill must be scoped to this flow's bind address and port.
    assert any(
        "iperf3 -s -1 -B 10.0.0.2 -p 5201" in str(call) for call in kills
    ), f"kill was not scoped to this flow: {kills!r}"
