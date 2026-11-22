"""Regression tests for the blocking iperf3 runner failure paths.

The synchronous runner must never leak temporary files or a detached one-shot
server when a step fails, must tolerate iperf3 warnings printed around its
JSON object, and must still fall back to the client JSON when the server-side
JSON cannot be read. These tests mock the nodes; no real iperf3 or container
is used.
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.traffic import iperf3_ports
from satgonetem.traffic.iperf3_utils import Iperf3Config, Iperf3Results, run_iperf3_flow

# Distinct loss markers tell which side's JSON produced the result.
_CLIENT_UDP_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "UDP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {
            "sum": {
                "bytes": 1_000_000,
                "packets": 1000,
                "jitter_ms": 0.1,
                "lost_packets": 1,
                "lost_percent": 0.1,
            }
        },
    }
)

_SERVER_UDP_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "UDP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {
            "sum": {
                "bytes": 2_000_000,
                "packets": 2000,
                "jitter_ms": 0.2,
                "lost_packets": 2,
                "lost_percent": 0.2,
            }
        },
    }
)

_CLIENT_TCP_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "TCP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {"sum_sent": {"bytes": 111_000}, "sum_received": {"bytes": 110_000}},
    }
)

_SERVER_TCP_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "TCP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {"sum_sent": {"bytes": 999_000}, "sum_received": {"bytes": 998_000}},
    }
)


# Bidirectional UDP: the client JSON carries both directions' receiver
# summaries. The server prints before exchanging results, so its reverse
# summary is empty even though the forward direction is complete.
_BIDIR_CLIENT_UDP_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "UDP",
                "duration": 1,
                "num_streams": 1,
                "bidir": 1,
            }
        },
        "intervals": [],
        "end": {
            "sum_received": {
                "bytes": 2_523_136,
                "packets": 77,
                "jitter_ms": 0.05,
                "lost_packets": 0,
            },
            "sum_received_bidir_reverse": {
                "bytes": 2_523_136,
                "packets": 77,
                "jitter_ms": 0.08,
                "lost_packets": 0,
            },
        },
    }
)

_BIDIR_SERVER_UDP_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "UDP",
                "duration": 1,
                "num_streams": 1,
                "bidir": 1,
            }
        },
        "intervals": [],
        "end": {
            "sum_received": {
                "bytes": 2_523_136,
                "packets": 77,
                "jitter_ms": 0.05,
                "lost_packets": 0,
            },
            "sum_received_bidir_reverse": {
                "bytes": 0,
                "packets": 0,
                "jitter_ms": 0.0,
                "lost_packets": 0,
            },
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


def _commands(node: MagicMock, marker: str) -> list[str]:
    return [
        call.args[0]
        for call in node.execute_command.call_args_list
        if call.args and marker in call.args[0]
    ]


def _allocated_port(destination: MagicMock) -> int:
    server_cmd = _commands(destination, "iperf3 -s")[0]
    match = re.search(r"-p (\d+)", server_cmd)
    assert match is not None
    return int(match.group(1))


def _run_udp(src: MagicMock, dst: MagicMock) -> Iperf3Results:
    return run_iperf3_flow(src, dst, Iperf3Config(protocol="UDP", duration=1))


def test_udp_falls_back_to_client_json_when_server_read_fails():
    """A failing server-side cat must not abort the flow."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")

    def source_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            return _CLIENT_UDP_JSON
        return ""

    def destination_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            raise RuntimeError("cat: server json missing")
        return ""

    src.execute_command.side_effect = source_command
    dst.execute_command.side_effect = destination_command

    result = _run_udp(src, dst)

    assert result.total_lost_packets == 1


def test_udp_parses_warning_prefixed_server_json():
    """Warnings printed before the JSON object must not force a fallback."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")
    warning = "warning: UDP block size 9000 exceeds TCP MSS 1400\n"

    def source_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            return _CLIENT_UDP_JSON
        return ""

    def destination_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            return warning + _SERVER_UDP_JSON + "\nconnection closed"
        return ""

    src.execute_command.side_effect = source_command
    dst.execute_command.side_effect = destination_command

    result = _run_udp(src, dst)

    assert result.total_lost_packets == 2


def test_tcp_parses_warning_prefixed_client_json():
    """Warnings before the preferred TCP client JSON must be skipped."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")

    def source_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            return "warning: something harmless\n" + _CLIENT_TCP_JSON
        return ""

    def destination_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            return _SERVER_TCP_JSON
        return ""

    src.execute_command.side_effect = source_command
    dst.execute_command.side_effect = destination_command

    result = run_iperf3_flow(src, dst, Iperf3Config(duration=1))

    assert result.total_bytes_sent == 111_000


def test_temp_files_are_removed_when_reading_json_fails():
    """Both temp files must be removed even when every read raises."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")

    def failing_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            raise RuntimeError("cat failed")
        return ""

    src.execute_command.side_effect = failing_command
    dst.execute_command.side_effect = failing_command

    with pytest.raises(RuntimeError, match="Could not retrieve iperf3 JSON"):
        _run_udp(src, dst)

    client_rm = _commands(src, "rm -f")
    server_rm = _commands(dst, "rm -f")
    assert any("iperf3_cli_" in cmd for cmd in client_rm)
    assert any("iperf3_srv_" in cmd for cmd in server_rm)


def test_detached_server_is_killed_when_the_client_fails():
    """A failed client must not leave the one-shot server holding the port."""
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")

    def source_command(cmd, **_kwargs):
        if "iperf3 -c" in cmd:
            raise RuntimeError("client setup failed")
        return ""

    src.execute_command.side_effect = source_command
    dst.execute_command.return_value = ""

    with pytest.raises(RuntimeError, match="client setup failed"):
        _run_udp(src, dst)

    port = _allocated_port(dst)
    kills = _commands(dst, "pkill")
    # The bracketed prefix keeps pkill -f from matching the wrapper `sh -c`
    # command line, which itself contains the kill pattern.
    bracketed = f"[i]perf3 -s -1 -B 10.0.0.2 -p {port}"
    assert any(bracketed in cmd for cmd in kills), (
        f"server kill was not scoped to this run: {kills!r}"
    )
    assert all("iperf3 -s" not in cmd for cmd in kills), (
        f"kill pattern must not match its own command line: {kills!r}"
    )


def test_udp_bidir_reads_the_client_json_with_both_directions():
    """Server-side bidir JSON is one-sided; the runner must prefer the client.

    iperf3 prints the server's JSON before exchanging results, so its
    ``sum_received_bidir_reverse`` is empty. Reading the server file reports one
    direction's bytes/packets and drops reverse jitter/loss, which is why the
    client JSON must be used for bidirectional runs.
    """
    src = _make_node("gs0", "10.0.0.1")
    dst = _make_node("gs1", "10.0.0.2")

    def source_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            return _BIDIR_CLIENT_UDP_JSON
        return ""

    def destination_command(cmd, **_kwargs):
        if cmd.startswith("cat "):
            return _BIDIR_SERVER_UDP_JSON
        return ""

    src.execute_command.side_effect = source_command
    dst.execute_command.side_effect = destination_command

    result = run_iperf3_flow(
        src, dst, Iperf3Config(protocol="UDP", duration=1, bidir=True)
    )

    assert result.total_bytes == 5_046_272
    assert result.total_packets == 154
