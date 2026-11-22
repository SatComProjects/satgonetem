"""Shell-injection regression tests for generated traffic commands.

Both builders and the satellite log helper interpolate caller-supplied strings
into a command executed through ``sh -c`` inside a container.  Every such value
must be a single shell token, so a ``;`` or whitespace cannot start a second
command.
"""

from __future__ import annotations

import shlex

import pytest

from satgonetem.traffic.hping3_types import Hping3Config
from satgonetem.traffic.iperf3_config import Iperf3Config

HOSTILE = "10.0.0.2; touch /tmp/pwned"
NORMAL_OUTPUT = "/tmp/out.json"


class TestIperf3ShellQuoting:
    def test_hostile_server_and_bind_ips_stay_single_tokens(self):
        cfg = Iperf3Config()

        command = cfg.build_client_command(HOSTILE, HOSTILE, NORMAL_OUTPUT)
        tokens = shlex.split(command)

        assert tokens[tokens.index("-c") + 1] == HOSTILE
        assert tokens[tokens.index("-B") + 1] == HOSTILE
        assert f"-c {HOSTILE}" not in command
        assert f"-B {HOSTILE}" not in command

    @pytest.mark.parametrize(
        ("field", "flag"),
        [
            ("window_size", "-w"),
            ("congestion_control", "-C"),
            ("length", "-l"),
            ("affinity", "--affinity"),
        ],
    )
    def test_hostile_configured_values_stay_single_tokens(self, field, flag):
        cfg = Iperf3Config(**{field: HOSTILE})

        command = cfg.build_client_command("10.0.0.2", "10.0.0.1", NORMAL_OUTPUT)
        tokens = shlex.split(command)

        assert tokens[tokens.index(flag) + 1] == HOSTILE
        assert f"{flag} {HOSTILE}" not in command

    @pytest.mark.parametrize(
        ("field", "flag", "value"),
        [
            ("window_size", "-w", "256K"),
            ("congestion_control", "-C", "bbr"),
            ("length", "-l", "1400"),
            ("affinity", "--affinity", "0,1"),
        ],
    )
    def test_normal_values_are_preserved(self, field, flag, value):
        cfg = Iperf3Config(**{field: value})

        command = cfg.build_client_command("10.0.0.2", "10.0.0.1", NORMAL_OUTPUT)

        assert f"{flag} {value}" in command


class TestHping3ShellQuoting:
    def test_hostile_destination_stays_a_single_token(self):
        command = Hping3Config().build_command(HOSTILE, "10.0.0.1")
        tokens = shlex.split(command)

        assert tokens[1] == HOSTILE
        assert f"hping3 {HOSTILE}" not in command

    def test_hostile_spoof_source_stays_a_single_token(self):
        command = Hping3Config(spoof_src=HOSTILE).build_command("10.0.0.2", "10.0.0.1")
        tokens = shlex.split(command)

        assert tokens[tokens.index("-a") + 1] == HOSTILE
        assert f"-a {HOSTILE}" not in command

    def test_hostile_bind_ip_stays_a_single_token(self):
        command = Hping3Config().build_command("10.0.0.2", HOSTILE)
        tokens = shlex.split(command)

        assert tokens[tokens.index("-a") + 1] == HOSTILE
        assert f"-a {HOSTILE}" not in command

    def test_hostile_interval_stays_a_single_token(self):
        command = Hping3Config(interval=HOSTILE).build_command("10.0.0.2", "10.0.0.1")
        tokens = shlex.split(command)

        assert tokens[tokens.index("-i") + 1] == HOSTILE
        assert f"-i {HOSTILE}" not in command

    def test_normal_values_are_preserved(self):
        command = Hping3Config(interval="u10000", spoof_src="10.0.0.1").build_command(
            "10.0.0.2", "10.0.0.1"
        )

        assert "-i u10000" in command
        assert "-a 10.0.0.1" in command
        assert "hping3 10.0.0.2 -c 100" in command
