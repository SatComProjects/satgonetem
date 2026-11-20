"""Regression tests for TRAFFIC-05: hping3 runs must be finite.

``hping3 --flood`` ignores ``-c`` and a non-positive count makes hping3 send
forever, leaving a container flooding after the scheduler abandons the flow.
These tests pin down the validation that prevents such commands from ever
being built.
"""

import pytest

from satgonetem.traffic.hping3_utils import Hping3Config

_DST_IP = "10.0.0.2"
_BIND_IP = "10.0.0.1"


class TestCountValidation:
    """A finite hping3 run requires count >= 1."""

    @pytest.mark.parametrize("count", [0, -1, -100])
    def test_non_positive_count_rejected(self, count):
        with pytest.raises(ValueError, match=r"count must be >= 1"):
            Hping3Config(count=count)

    @pytest.mark.parametrize("count", [0, -1])
    def test_non_positive_count_rejected_on_mutation(self, count):
        cfg = Hping3Config(count=5)
        cfg.count = count
        with pytest.raises(ValueError, match=r"count must be >= 1"):
            cfg.build_command(_DST_IP, _BIND_IP)

    @pytest.mark.parametrize("count", [1, 10, 1000])
    def test_positive_counts_still_build_command(self, count):
        cfg = Hping3Config(count=count)
        cmd = cfg.build_command(_DST_IP, _BIND_IP)
        assert f"-c {count}" in cmd
        assert "--flood" not in cmd


class TestRateTypeValidation:
    """``flood`` is rejected and build_command can never emit --flood."""

    def test_flood_rejected_at_construction(self):
        with pytest.raises(ValueError, match="flood"):
            Hping3Config(rate_type="flood")

    def test_flood_rejected_before_command_building(self):
        cfg = Hping3Config(count=5)
        cfg.rate_type = "flood"
        with pytest.raises(ValueError, match="flood"):
            cfg.build_command(_DST_IP, _BIND_IP)

    @pytest.mark.parametrize("rate_type", ["interval", "fast", "faster"])
    def test_valid_rate_types_never_emit_flood(self, rate_type):
        cfg = Hping3Config(rate_type=rate_type)
        cmd = cfg.build_command(_DST_IP, _BIND_IP)
        assert "--flood" not in cmd

    @pytest.mark.parametrize("rate_type", ["", "turbo", "FAST", "flooding"])
    def test_invalid_rate_type_rejected(self, rate_type):
        with pytest.raises(ValueError, match="rate_type"):
            Hping3Config(rate_type=rate_type)


class TestProtoValidation:
    """Only tcp, udp, and icmp are valid hping3 protocols."""

    @pytest.mark.parametrize("proto", ["", "sctp", "tcp4", "icmp6", "raw"])
    def test_invalid_proto_rejected(self, proto):
        with pytest.raises(ValueError, match="proto"):
            Hping3Config(proto=proto)

    @pytest.mark.parametrize("proto", ["tcp", "udp", "icmp"])
    def test_valid_protos_build_finite_command(self, proto):
        cfg = Hping3Config(proto=proto, count=7)
        cmd = cfg.build_command(_DST_IP, _BIND_IP)
        assert "-c 7" in cmd
        assert "--flood" not in cmd


class TestFlagValidation:
    """TCP flags must come from the supported hping3 flag map."""

    @pytest.mark.parametrize(
        "flags",
        [
            ["X"],
            ["s"],
            ["S", "Z"],
            ["SYN"],
        ],
    )
    def test_invalid_flags_rejected(self, flags):
        with pytest.raises(ValueError, match="flags"):
            Hping3Config(proto="tcp", flags=flags)

    @pytest.mark.parametrize("flag", ["S", "A", "F", "R", "P", "U"])
    def test_valid_flags_accepted(self, flag):
        cfg = Hping3Config(proto="tcp", flags=[flag])
        cmd = cfg.build_command(_DST_IP, _BIND_IP)
        assert f"-{flag}" in cmd
        assert "--flood" not in cmd


class TestFiniteCommandContract:
    """Every accepted config builds a bounded, non-flood command."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"proto": "udp", "dport": 53, "count": 3},
            {"proto": "icmp", "count": 1},
            {"proto": "tcp", "flags": ["S"], "rate_type": "fast", "count": 42},
            {"rate_type": "faster", "interval": "u10000", "count": 9},
            {"rate_type": "interval", "interval": "1", "count": 2},
        ],
    )
    def test_valid_config_builds_bounded_command(self, kwargs):
        cfg = Hping3Config(**kwargs)
        cmd = cfg.build_command(_DST_IP, _BIND_IP)
        assert f"-c {cfg.count}" in cmd
        assert "--flood" not in cmd

    def test_default_config_is_finite(self):
        cfg = Hping3Config()
        cmd = cfg.build_command(_DST_IP, _BIND_IP)
        assert "-c 100" in cmd
        assert "--flood" not in cmd
