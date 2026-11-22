"""Direction-aware JSON selection for the blocking iperf3 runner.

UDP metrics (loss, jitter) live on the receiver; TCP metrics (RTT, cwnd,
retransmits) live on the sender. A ``--reverse`` run swaps which endpoint is
the receiver, so the runner must mirror ``Iperf3Flow._json_side`` instead of
always preferring the server JSON for UDP and the client JSON for TCP.

These tests use fake nodes; no real iperf3 or container is used.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.traffic import iperf3_ports
from satgonetem.traffic.iperf3_utils import Iperf3Config, Iperf3Results, run_iperf3_flow

# The client is the receiver in a UDP --reverse run: its summary carries loss
# and jitter, while the server (sender) reports no receiver statistics.
_CLIENT_UDP_RECEIVER_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "UDP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {
            "sum": {
                "bytes": 2_000_000,
                "packets": 2000,
                "jitter_ms": 0.25,
                "lost_packets": 5,
                "lost_percent": 0.25,
            }
        },
    }
)

_SERVER_UDP_SENDER_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "UDP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {
            "sum": {
                "bytes": 2_000_000,
                "packets": 2005,
                "jitter_ms": 0.0,
                "lost_packets": 0,
                "lost_percent": 0.0,
            }
        },
    }
)

# The server is the sender in a TCP --reverse run: its summary carries
# retransmits, while the client only has receiver statistics.
_CLIENT_TCP_RECEIVER_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "TCP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {"sum_sent": {"bytes": 111_000}, "sum_received": {"bytes": 110_000}},
    }
)

_SERVER_TCP_SENDER_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "TCP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {
            "sum_sent": {"bytes": 999_000, "retransmits": 7},
            "sum_received": {"bytes": 998_000},
        },
    }
)


def _make_node(name: str, ipv4: str) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.loopback = MagicMock()
    node.loopback.ipv4 = ipv4
    return node


@pytest.fixture(autouse=True)
def _fresh_port_allocator(monkeypatch):
    """Give every test an isolated process-wide allocator."""
    monkeypatch.setattr(
        iperf3_ports,
        "_IPERF3_PORT_ALLOCATOR",
        iperf3_ports._Iperf3PortAllocator(),
    )


@pytest.fixture(autouse=True)
def _no_real_waits():
    """Keep the runner's readiness/settle waits out of the test runtime."""
    with (
        patch("satgonetem.traffic.iperf3_runner._wait_for_iperf3_server"),
        patch("satgonetem.traffic.iperf3_runner.time.sleep"),
    ):
        yield


def _make_node_serving(name: str, ipv4: str, json_text: str) -> MagicMock:
    """Return a fake node whose ``cat`` commands return ``json_text``."""

    def command(cmd, **_kwargs):
        return json_text if cmd.startswith("cat ") else ""

    node = _make_node(name, ipv4)
    node.execute_command.side_effect = command
    return node


def _run(config: Iperf3Config, client_json: str, server_json: str) -> Iperf3Results:
    src = _make_node_serving("gs0", "10.0.0.1", client_json)
    dst = _make_node_serving("gs1", "10.0.0.2", server_json)
    return run_iperf3_flow(src, dst, config)


def test_udp_reverse_prefers_the_client_receiver_json():
    """The server only sends in UDP reverse; jitter/loss live on the client."""
    result = _run(
        Iperf3Config(protocol="UDP", duration=1, reverse=True),
        client_json=_CLIENT_UDP_RECEIVER_JSON,
        server_json=_SERVER_UDP_SENDER_JSON,
    )

    assert result.total_lost_packets == 5
    assert result.avg_jitter_ms == pytest.approx(0.25)


def test_tcp_reverse_prefers_the_server_sender_json():
    """The server sends in TCP reverse; retransmits live on the server."""
    result = _run(
        Iperf3Config(duration=1, reverse=True),
        client_json=_CLIENT_TCP_RECEIVER_JSON,
        server_json=_SERVER_TCP_SENDER_JSON,
    )

    assert result.total_retransmits == 7
    assert result.total_bytes_sent == 999_000


def test_tcp_bidir_keeps_the_client_json_first():
    """Bidirectional output stays client-first regardless of the protocol."""
    result = _run(
        Iperf3Config(duration=1, bidir=True),
        client_json=_CLIENT_TCP_RECEIVER_JSON,
        server_json=_SERVER_TCP_SENDER_JSON,
    )

    assert result.total_bytes_sent == 111_000


def test_udp_reverse_falls_back_to_the_server_sender_json():
    """A missing client receiver file still falls back to the other side."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node_serving("gs1", "10.0.0.2", _SERVER_UDP_SENDER_JSON)

    def source_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            raise RuntimeError("cat: client json missing")
        return ""

    src.execute_command.side_effect = source_command

    result = run_iperf3_flow(
        src,
        dst,
        Iperf3Config(protocol="UDP", duration=1, reverse=True),
    )

    assert result.total_bytes == 2_000_000
