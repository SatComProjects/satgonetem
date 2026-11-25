"""Tests for the TBF QoS mixin.

The host class is a minimal fake: ``execute_command`` records the commands and
returns canned ``tc qdisc show`` output, so all arithmetic and parsing paths
can be asserted without Docker, root, or ``tc``.
"""

from __future__ import annotations

import logging

import pytest

from satgonetem.models.mixins.qos_mixin import QoSCapableMixin

MTU = 1500
HEADER_BYTES = 42


class _FakeQoSHost(QoSCapableMixin):
    def __init__(self, show_output: str) -> None:
        self.name = "Sat0"
        self.command_output = ""
        self._show_output = show_output
        self.commands: list[str] = []

    def execute_command(self, command: str, **_kwargs) -> str:
        self.commands.append(command)
        self.command_output = self._show_output
        return self._show_output


def _run(
    show_output: str,
    *,
    tbf_config: str = "limit",
    demand_bit_rate: int = 10,
    isl_capacity: int = 8,
    num_of_flows: int = 1,
    num_of_bytes: int = 1_000_000,
) -> _FakeQoSHost:
    host = _FakeQoSHost(show_output)
    host.initialise_global_qos(
        interface_name="eth0",
        num_of_flows=num_of_flows,
        demand_bit_rate=demand_bit_rate,
        isl_capacity=isl_capacity,
        num_of_bytes=num_of_bytes,
        tbf_config=tbf_config,
        link="Sat0-Sat1",
    )
    return host


LATENCY_OUTPUT = "qdisc tbf 10: root refcnt 2 rate 8Mbit burst 1600b lat 100ms"
LIMIT_OUTPUT = "qdisc tbf 10: root refcnt 2 rate 8Mbit burst 1600b limit 72000b"


class TestQosOutputParsing:
    def test_single_line_latency_output_is_parsed(self):
        # ``tc qdisc show`` commonly prints a single line; the old code read
        # lines[1] and raised IndexError.
        host = _run(LATENCY_OUTPUT)

        assert len(host.commands) == 2
        assert "rate 8Mbit burst 1600b" in host.commands[1]

    def test_single_line_limit_output_is_parsed(self):
        host = _run(LIMIT_OUTPUT)

        assert len(host.commands) == 2
        assert "rate 8Mbit burst 1600b" in host.commands[1]

    def test_tbf_line_after_non_tbf_line_is_found(self):
        output = "qdisc noqueue 0: root refcnt 2\n" + LATENCY_OUTPUT
        host = _run(output)

        assert len(host.commands) == 2
        assert "rate 8Mbit" in host.commands[1]

    def test_empty_output_logs_and_skips_change(self, caplog):
        with caplog.at_level(logging.ERROR):
            host = _run("")

        assert host.commands == ["tc qdisc show dev eth0"]
        assert "No TBF qdisc match found on Sat0 interface eth0" in caplog.text

    def test_output_without_tbf_logs_and_skips_change(self, caplog):
        with caplog.at_level(logging.ERROR):
            host = _run("qdisc noqueue 0: root refcnt 2")

        assert host.commands == ["tc qdisc show dev eth0"]
        assert "No TBF qdisc match found on Sat0 interface eth0" in caplog.text


class TestQosDemandValidation:
    def test_zero_demand_bit_rate_raises_instead_of_dividing_by_zero(self):
        with pytest.raises(ValueError, match="demand_bit_rate"):
            _run(LATENCY_OUTPUT, demand_bit_rate=0)

    def test_negative_demand_bit_rate_raises(self):
        with pytest.raises(ValueError, match="demand_bit_rate"):
            _run(LATENCY_OUTPUT, demand_bit_rate=-5)

    def test_non_positive_capacity_raises(self):
        with pytest.raises(ValueError, match="isl_capacity"):
            _run(LATENCY_OUTPUT, isl_capacity=0)


class TestQosNoShapingNeeded:
    def test_limit_mode_skips_change_when_offered_rate_below_capacity(self, caplog):
        # 8 Mbps capacity, 8 Mbps offered: the wire overhead still exceeds
        # capacity slightly, so use a clearly lower demand.
        with caplog.at_level(logging.INFO):
            host = _run(LATENCY_OUTPUT, demand_bit_rate=5, isl_capacity=10)

        assert host.commands == ["tc qdisc show dev eth0"]
        assert "No TBF shaping required" in caplog.text

    def test_burstlimit_mode_skips_negative_burst_when_no_excess(self, caplog):
        with caplog.at_level(logging.INFO):
            host = _run(LIMIT_OUTPUT, tbf_config="burstlimit", demand_bit_rate=2)

        assert host.commands == ["tc qdisc show dev eth0"]
        assert "No TBF shaping required" in caplog.text

    def test_limit_command_is_never_negative(self):
        host = _run(LIMIT_OUTPUT, demand_bit_rate=5, isl_capacity=10)

        for command in host.commands:
            assert "-" not in command

    def test_burstlimit_command_has_positive_burst_and_limit(self):
        host = _run(
            LIMIT_OUTPUT,
            tbf_config="burstlimit",
            demand_bit_rate=10,
            isl_capacity=8,
            num_of_bytes=10_000_000,
        )

        assert len(host.commands) == 2
        assert "burst " in host.commands[1]
        assert "limit " in host.commands[1]
        for token in host.commands[1].split():
            if token.endswith("b"):
                assert float(token[:-1]) > 0


class TestQosArithmetic:
    def test_limit_mode_uses_excess_bytes_as_limit(self, capsys):
        demand_mbps = 10
        capacity_mbps = 8
        num_bytes = 1_000_000
        capacity_bytes = capacity_mbps * 1_000_000 / 8
        rate_bytes = demand_mbps * 1_000_000 / 8
        duration = num_bytes / rate_bytes
        total_wire_rate = rate_bytes * MTU / (MTU - HEADER_BYTES)
        expected_limit = int((total_wire_rate - capacity_bytes) * duration - 1600)

        host = _run(
            LATENCY_OUTPUT,
            tbf_config="limit",
            demand_bit_rate=demand_mbps,
            isl_capacity=capacity_mbps,
            num_of_bytes=num_bytes,
        )

        assert host.commands[1] == (
            "tc qdisc change dev eth0 parent 1:1 handle 10: "
            f"tbf rate 8Mbit burst 1600b limit {expected_limit}b"
        )
        assert expected_limit > 0
        assert capsys.readouterr().out == ""

    def test_burstlimit_mode_uses_capacity_buffer_as_limit(self, capsys):
        demand_mbps = 10
        capacity_mbps = 8
        num_bytes = 10_000_000
        capacity_bytes = capacity_mbps * 1_000_000 / 8
        rate_bytes = demand_mbps * 1_000_000 / 8
        duration = num_bytes / rate_bytes
        total_wire_rate = rate_bytes * MTU / (MTU - HEADER_BYTES)
        required_limit = capacity_bytes * 0.072
        expected_burst = int(
            (total_wire_rate - capacity_bytes) * duration - required_limit
        )

        host = _run(
            LIMIT_OUTPUT,
            tbf_config="burstlimit",
            demand_bit_rate=demand_mbps,
            isl_capacity=capacity_mbps,
            num_of_bytes=num_bytes,
        )

        assert host.commands[1] == (
            "tc qdisc change dev eth0 parent 1:1 handle 10: "
            f"tbf rate 8Mbit burst {expected_burst}b "
            f"limit {int(required_limit)}b"
        )
        assert expected_burst > 0
        assert capsys.readouterr().out == ""

    def test_limit_mode_logs_additional_latency(self, caplog):
        with caplog.at_level(logging.INFO):
            _run(
                LATENCY_OUTPUT,
                tbf_config="limit",
                demand_bit_rate=10,
                isl_capacity=8,
                num_of_bytes=1_000_000,
            )

        assert any(
            "Configured TBF limit on link Sat0-Sat1" in record.getMessage()
            for record in caplog.records
        )

    def test_parses_kb_burst_and_decimal_rate_latency(self):
        kb_output = "qdisc tbf 10: root refcnt 2 rate 8.5Mbit burst 16Kb lat 50.0ms"
        host = _run(
            kb_output,
            tbf_config="limit",
            demand_bit_rate=10,
            isl_capacity=8,
            num_of_bytes=1_000_000,
        )

        assert len(host.commands) == 2
        capacity_bytes = 8 * 1_000_000 / 8
        rate_bytes = 10 * 1_000_000 / 8
        duration = 1_000_000 / rate_bytes
        total_wire_rate = rate_bytes * MTU / (MTU - HEADER_BYTES)
        expected_limit = int((total_wire_rate - capacity_bytes) * duration - 16 * 1024)
        assert host.commands[1] == (
            "tc qdisc change dev eth0 parent 1:1 handle 10: "
            f"tbf rate 8.5Mbit burst 16Kb limit {expected_limit}b"
        )

    def test_unknown_tbf_config_raises_value_error(self):
        with pytest.raises(ValueError, match="tbf_config"):
            _run(LATENCY_OUTPUT, tbf_config="unknown")

    def test_limit_mode_additional_latency_does_not_double_subtract_burst(self, caplog):
        demand_mbps = 10
        capacity_mbps = 8
        num_bytes = 1_000_000
        capacity_bytes = capacity_mbps * 1_000_000 / 8
        rate_bytes = demand_mbps * 1_000_000 / 8
        duration = num_bytes / rate_bytes
        total_wire_rate = rate_bytes * MTU / (MTU - HEADER_BYTES)
        required_limit = int((total_wire_rate - capacity_bytes) * duration - 1600)
        expected_latency_ms = required_limit / capacity_bytes * 1000

        with caplog.at_level(logging.INFO):
            _run(
                LATENCY_OUTPUT,
                tbf_config="limit",
                demand_bit_rate=demand_mbps,
                isl_capacity=capacity_mbps,
                num_of_bytes=num_bytes,
            )

        assert f"additional latency {expected_latency_ms:.2f} ms." in caplog.text
