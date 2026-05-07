"""Tests for satgonetem.link_budget.config."""


from satgonetem.link_budget.config import AntennaConfig, LinkBudgetConfig
from satgonetem.link_budget.antenna import Antenna


class TestLinkBudgetConfig:
    def test_default_values(self):
        cfg = LinkBudgetConfig()
        assert cfg.downlink_freq_ghz == 19.0
        assert cfg.uplink_freq_ghz == 14.25
        assert cfg.bandwidth_hz_downlink == 500e6
        assert cfg.bandwidth_hz_uplink == 500e6

    def test_custom_values(self):
        cfg = LinkBudgetConfig(
            downlink_freq_ghz=20.0,
            uplink_freq_ghz=15.0,
            bandwidth_hz_downlink=250e6,
            bandwidth_hz_uplink=100e6,
        )
        assert cfg.downlink_freq_ghz == 20.0
        assert cfg.uplink_freq_ghz == 15.0
        assert cfg.bandwidth_hz_downlink == 250e6
        assert cfg.bandwidth_hz_uplink == 100e6


class TestAntennaConfig:
    def test_default_values(self):
        cfg = AntennaConfig()
        assert cfg.diameter == 0.0
        assert cfg.efficiency == 0.6
        assert cfg.sspa_output_power_db == 0.0
        assert cfg.losses_db == 0.0
        assert cfg.eirp_db is None
        assert cfg.gain_db is None

    def test_to_antenna(self):
        cfg = AntennaConfig(
            diameter=1.5,
            efficiency=0.65,
            sspa_output_power_db=30.0,
            losses_db=1.0,
            eirp_db=50.0,
            gain_db=40.0,
        )
        ant = cfg.to_antenna()
        assert isinstance(ant, Antenna)
        assert ant.diameter == 1.5
        assert ant.efficiency == 0.65
        assert ant.sspa_output_power_db == 30.0
        assert ant.losses_db == 1.0
        assert ant.eirp_db == 50.0
        assert ant.gain_db == 40.0
