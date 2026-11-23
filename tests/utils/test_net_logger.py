"""Contract tests for the per-satellite link-usage sampler helpers."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import time

import pytest

from satgonetem.utils.net_logger import (
    NET_LOGGER_DIR,
    NET_LOGGER_INTERVAL_S,
    build_net_logger_script,
    build_start_command,
    build_stop_command,
    net_logger_path,
    net_logger_token,
)


def test_defaults_match_the_monitor_contract() -> None:
    assert NET_LOGGER_DIR == "/tmp/interfaces"
    assert NET_LOGGER_INTERVAL_S == 1.0


def test_net_logger_path_uses_project_and_node() -> None:
    assert net_logger_path("proj", "Sat3") == "/tmp/interfaces/proj.Sat3"
    assert net_logger_path("proj", "Sat3", "/tmp/custom/") == "/tmp/custom/proj.Sat3"
    assert net_logger_path("proj", "Sat3", "/") == "/proj.Sat3"


@pytest.mark.parametrize("project_name", ["", "   ", None])
def test_net_logger_path_rejects_invalid_project_name(project_name) -> None:
    with pytest.raises(ValueError, match="project_name"):
        net_logger_path(project_name, "Sat0")


@pytest.mark.parametrize("node_name", ["", "   ", None])
def test_net_logger_path_rejects_invalid_node_name(node_name) -> None:
    with pytest.raises(ValueError, match="node_name"):
        net_logger_path("proj", node_name)


@pytest.mark.parametrize("path", ["", "   ", None])
def test_net_logger_path_rejects_invalid_path(path) -> None:
    with pytest.raises(ValueError, match="path"):
        net_logger_path("proj", "Sat0", path)


def test_token_is_deterministic_and_shell_safe() -> None:
    destination = "/tmp/interfaces/proj.Sat0"

    token = net_logger_token(destination)

    assert token == net_logger_token(destination)
    assert token.startswith("sgnl-")
    assert token.isascii()
    assert all(character.isalnum() or character in "-_" for character in token)


def test_token_is_unique_per_destination() -> None:
    assert net_logger_token("/tmp/interfaces/proj.Sat0") != net_logger_token(
        "/tmp/interfaces/proj.Sat1"
    )


def test_script_samples_eth_counters_and_appends_jsonl() -> None:
    script = build_net_logger_script()

    assert script.startswith("#!/usr/bin/env bash")
    assert 'for path in "$sysfs_net"/eth*; do' in script
    assert "SGNL_SYSFS_NET:-/sys/class/net" in script
    assert "statistics/rx_bytes" in script
    assert "statistics/tx_bytes" in script
    assert "/proc/uptime" in script
    assert 'line="${line}${sep}' in script
    assert "prev_rx" in script
    assert "prev_tx" in script
    assert '>> "$out"' in script
    assert ': > "$out"' in script
    assert '"${line}}"' in script


def _write_counters(sysfs_net, iface: str, rx: int, tx: int) -> None:
    statistics = sysfs_net / iface / "statistics"
    statistics.mkdir(parents=True, exist_ok=True)
    (statistics / "rx_bytes").write_text(str(rx))
    (statistics / "tx_bytes").write_text(str(tx))


def _start_sampler(output, sysfs_net, interval: float = 0.05):
    environment = dict(os.environ, SGNL_SYSFS_NET=str(sysfs_net))
    process = subprocess.Popen(
        ["bash", "-s", "--", str(output), str(interval), "sgnl-test"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    assert process.stdin is not None
    process.stdin.write(build_net_logger_script().encode("utf-8"))
    process.stdin.close()
    return process


def _wait_for_samples(
    path, count: int, timeout: float = 2.0, predicate=None
) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            lines = [line for line in path.read_text().splitlines() if line]
            if len(lines) >= count and (predicate is None or predicate(lines)):
                return lines
        time.sleep(0.01)
    raise AssertionError(f"{path} did not reach {count} sample(s) within {timeout}s")


def test_sampler_emits_zero_rows_and_truncates_old_samples(tmp_path) -> None:
    sysfs_net = tmp_path / "net"
    _write_counters(sysfs_net, "eth0", 1000, 2000)
    output = tmp_path / "proj.Sat0"
    output.write_text('{"stale": 99}\n')

    process = _start_sampler(output, sysfs_net)
    try:
        lines = _wait_for_samples(
            output, 1, predicate=lambda samples: "stale" not in "\n".join(samples)
        )
    finally:
        process.terminate()
        process.wait(timeout=2)

    assert lines == ['{"eth0":0}']


def test_sampler_reports_nonzero_usage_after_counter_growth(tmp_path) -> None:
    sysfs_net = tmp_path / "net"
    _write_counters(sysfs_net, "eth0", 1000, 1000)
    output = tmp_path / "proj.Sat0"

    process = _start_sampler(output, sysfs_net)
    try:
        _wait_for_samples(output, 1)
        _write_counters(sysfs_net, "eth0", 1000, 6000)
        lines = _wait_for_samples(output, 2)
    finally:
        process.terminate()
        process.wait(timeout=2)

    usage = json.loads(lines[-1])
    assert usage["eth0"] > 0


def test_start_command_kills_stale_sampler_truncates_and_launches_script() -> None:
    destination = "/tmp/interfaces/proj.Sat0"
    token = net_logger_token(destination)

    command = build_start_command(destination, 1.5, token)
    tokens = shlex.split(command)

    assert "pkill" in command
    assert 'pattern="[s]${token#s}"' in command
    assert "base64 -d" in command
    assert token not in command
    assert base64.b64encode(token.encode("utf-8")).decode("ascii") in command
    assert tokens[-6:] == ["bash", "-s", "--", destination, "1.5", "$token"]

    encoded = re.findall(r"printf %s (\S+) \| base64 -d", command)
    assert len(encoded) == 2
    assert base64.b64decode(encoded[-1]).decode("utf-8") == build_net_logger_script()


def test_start_command_quotes_a_hostile_destination() -> None:
    destination = "/tmp/logs; touch /tmp/pwned/proj.Sat0"
    token = net_logger_token(destination)

    command = build_start_command(destination, 1, token)
    tokens = shlex.split(command)

    assert destination in tokens
    assert f"rm -f {shlex.quote(destination)}" in command


def test_stop_command_kills_the_same_token_and_removes_the_file() -> None:
    destination = "/tmp/interfaces/proj.Sat0"
    token = net_logger_token(destination)

    command = build_stop_command(destination, token)

    assert "pkill" in command
    assert 'pattern="[s]${token#s}"' in command
    assert token not in command
    assert base64.b64encode(token.encode("utf-8")).decode("ascii") in command
    assert f"rm -f {shlex.quote(destination)}" in command
    assert command.endswith("rm -f " + shlex.quote(destination))


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_start_command_rejects_invalid_intervals(interval) -> None:
    destination = "/tmp/interfaces/proj.Sat0"

    with pytest.raises(ValueError, match="interval_s"):
        build_start_command(destination, interval, net_logger_token(destination))


@pytest.mark.parametrize("interval", ["1", None])
def test_start_command_rejects_non_numeric_intervals(interval) -> None:
    destination = "/tmp/interfaces/proj.Sat0"

    with pytest.raises(TypeError, match="interval_s"):
        build_start_command(destination, interval, net_logger_token(destination))


@pytest.mark.parametrize("destination", ["", None])
def test_start_command_rejects_invalid_destination(destination) -> None:
    with pytest.raises(ValueError, match="destination"):
        build_start_command(destination, 1, "sgnl-token")


@pytest.mark.parametrize("token", ["", None])
def test_stop_command_rejects_invalid_token(token) -> None:
    with pytest.raises(ValueError, match="token"):
        build_stop_command("/tmp/interfaces/proj.Sat0", token)
