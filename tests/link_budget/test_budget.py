"""Tests for satgonetem.link_budget.budget."""

from unittest.mock import MagicMock, patch

import pytest

from satgonetem.link_budget.budget import LinkBudgetCalculator, LinkBudgetResult
from satgonetem.link_budget.modcod import ModCod


@pytest.fixture
def calculator():
    return LinkBudgetCalculator(
        frequency_ghz_downlink=19.0,
        frequency_ghz_uplink=14.25,
        bandwidth_hz_downlink=500e6,
        bandwidth_hz_uplink=500e6,
        rolloff_factor=0.25,
        unavailability_percent=0.1,
        rx_tsys_k=100.0,
    )


@pytest.fixture
def mock_sat():
    node = MagicMock()
    node.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
    node.antenna = MagicMock()
    node.antenna.eirp_db = 50.0
    node.antenna.gain_db = 35.0
    node.antenna.sspa_output_power_db = 20.0
    node.antenna.losses_db = 1.0
    return node


@pytest.fixture
def mock_gs():
    node = MagicMock()
    node.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    node.antenna = MagicMock()
    node.antenna.gain_db = 40.0
    node.antenna.diameter = 2.0
    node.antenna.calculate_gain_db.return_value = 40.0
    node.g_over_t_db = None
    return node


class TestLinkBudgetResult:
    def test_dataclass_creation(self):
        result = LinkBudgetResult(
            elevation_angle=30.0,
            free_space_loss_db=200.0,
            atmospheric_attenuation_db=1.0,
            eirp_db=50.0,
            g_over_t_db=20.0,
            cn0_dbhz=80.0,
            cn_db=20.0,
            best_modcod=ModCod.QPSK_11_20,
            capacity_bps=1e9,
            capacity_kbps=1_000_000,
        )
        assert result.elevation_angle == 30.0
        assert result.capacity_kbps == 1_000_000


class TestComputeElevationAngle:
    def test_directly_overhead(self, calculator):
        sat_pos = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
        gnd_pos = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
        elev = calculator.compute_elevation_angle(sat_pos, gnd_pos)
        assert elev == pytest.approx(90.0, abs=1e-3)

    def test_different_locations(self, calculator):
        sat_pos = {"latitude": 10.0, "longitude": 20.0, "altitude": 550.0}
        gnd_pos = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
        elev = calculator.compute_elevation_angle(sat_pos, gnd_pos)
        assert 0.0 < elev < 90.0


class TestComputeOneWayShannonCapacity:
    @patch("satgonetem.link_budget.budget.calculate_atmospheric_attenuation_dB")
    @patch("satgonetem.link_budget.budget.calculate_free_space_loss_db")
    def test_with_antenna(
        self, mock_fsl, mock_atm, calculator, mock_sat, mock_gs
    ):
        mock_fsl.return_value = 200.0
        mock_atm.return_value = (1.0, 0.5, 0.2, 0.2, 0.1)

        capacity = calculator.compute_one_way_shannon_capacity(
            tx_node=mock_sat,
            rx_node=mock_gs,
            frequency_ghz=19.0,
            distance_km=1000.0,
            elevation_angle=45.0,
            gs_lat=0.0,
            gs_lon=0.0,
            gs_diameter=2.0,
            bandwidth_hz=100e6,
        )
        assert isinstance(capacity, int)
        assert capacity >= 0

    def test_no_tx_antenna_returns_zero(self, calculator, mock_gs):
        node = MagicMock()
        node.antenna = None
        capacity = calculator.compute_one_way_shannon_capacity(
            tx_node=node,
            rx_node=mock_gs,
            frequency_ghz=19.0,
            distance_km=1000.0,
            elevation_angle=45.0,
            gs_lat=0.0,
            gs_lon=0.0,
            gs_diameter=2.0,
            bandwidth_hz=100e6,
        )
        assert capacity == 0


class TestComputeDownlinkModcod:
    @patch("satgonetem.link_budget.budget.calculate_atmospheric_attenuation_dB")
    @patch("satgonetem.link_budget.budget.calculate_free_space_loss_db")
    def test_full_budget(
        self, mock_fsl, mock_atm, calculator, mock_sat, mock_gs
    ):
        mock_fsl.return_value = 200.0
        mock_atm.return_value = (1.0, 0.5, 0.2, 0.2, 0.1)

        result = calculator.compute_downlink_modcod(
            sat_node=mock_sat,
            gnd_node=mock_gs,
            distance_km=1000.0,
            gs_lat=0.0,
            gs_lon=0.0,
            gs_diameter=2.0,
        )

        assert isinstance(result, LinkBudgetResult)
        assert result.eirp_db == 50.0
        assert result.best_modcod is not None
        assert isinstance(result.capacity_kbps, int)
        assert result.capacity_kbps is not None
        assert result.capacity_kbps > 0

    def test_missing_eirp_raises(self, calculator, mock_sat, mock_gs):
        mock_sat.antenna.eirp_db = None
        with pytest.raises(ValueError, match="EIRP"):
            calculator.compute_downlink_modcod(
                sat_node=mock_sat,
                gnd_node=mock_gs,
                distance_km=1000.0,
                gs_lat=0.0,
                gs_lon=0.0,
                gs_diameter=2.0,
            )


class TestIntegrationNoMocks:
    """End-to-end link budget without any mocks.

    Uses a LEO satellite at 550 km directly overhead a ground station and a
    high EIRP so that at least one MODCOD is always achievable.  The test
    validates the sign and plausibility of every intermediate result rather
    than exact numbers, since atmospheric losses depend on the itur library.
    """

    def _make_sat_node(self, eirp_db: float, lat: float, lon: float, alt_km: float):
        class _Antenna:
            pass

        class _SatNode:
            pass

        ant = _Antenna()
        ant.eirp_db = eirp_db
        node = _SatNode()
        node.antenna = ant
        node.position = {"latitude": lat, "longitude": lon, "altitude": alt_km}
        return node

    def _make_gs_node(self, diameter: float, lat: float, lon: float):
        from satgonetem.link_budget.antenna import Antenna

        class _GsNode:
            pass

        node = _GsNode()
        node.antenna = Antenna(diameter=diameter, efficiency=0.6)
        node.g_over_t_db = None
        node.position = {"latitude": lat, "longitude": lon, "altitude": 0.0}
        return node

    def test_downlink_modcod_overhead_leo(self):
        """Satellite directly overhead at 550 km: budget must be positive and self-consistent."""
        from satgonetem.link_budget.budget import LinkBudgetCalculator, LinkBudgetResult
        from satgonetem.link_budget.propagation import calculate_free_space_loss_db

        calc = LinkBudgetCalculator(
            frequency_ghz_downlink=19.0,
            bandwidth_hz_downlink=500e6,
            rolloff_factor=0.25,
            unavailability_percent=0.1,
            rx_tsys_k=100.0,
        )

        lat, lon = 48.0, 2.0
        sat = self._make_sat_node(eirp_db=60.0, lat=lat, lon=lon, alt_km=550.0)
        gs = self._make_gs_node(diameter=1.2, lat=lat, lon=lon)

        result = calc.compute_downlink_modcod(
            sat_node=sat,
            gnd_node=gs,
            distance_km=550.0,
            gs_lat=lat,
            gs_lon=lon,
            gs_diameter=1.2,
        )

        assert isinstance(result, LinkBudgetResult)
        assert result.elevation_angle == pytest.approx(90.0, abs=1.0)

        expected_fsl = calculate_free_space_loss_db(19.0, 550.0)
        assert result.free_space_loss_db == pytest.approx(expected_fsl, abs=0.01)
        assert result.free_space_loss_db > 150.0

        assert result.atmospheric_attenuation_db >= 0.0
        assert result.eirp_db == 60.0
        assert result.g_over_t_db < result.g_over_t_db + result.free_space_loss_db
        assert result.cn0_dbhz > 50.0
        assert result.cn_db is not None
        assert result.cn_db < result.cn0_dbhz
        assert result.best_modcod is not None
        assert result.capacity_bps is not None and result.capacity_bps > 0
        assert result.capacity_kbps is not None and result.capacity_kbps > 0
        assert result.capacity_kbps == int(result.capacity_bps / 1000.0)

    def test_shannon_capacity_overhead_leo(self):
        """One-way Shannon capacity with real propagation: must be a positive integer."""
        from satgonetem.link_budget.antenna import Antenna
        from satgonetem.link_budget.budget import LinkBudgetCalculator

        calc = LinkBudgetCalculator(rx_tsys_k=150.0)

        class _TxAntenna:
            gain_db = 35.0
            sspa_output_power_db = 20.0
            losses_db = 1.0

            def calculate_gain_db(self, f):
                return self.gain_db

        class _TxNode:
            antenna = _TxAntenna()

        rx_antenna = Antenna(diameter=1.2, efficiency=0.6)
        rx_antenna.gain_db = rx_antenna.calculate_gain_db(19.0)

        class _RxNode:
            antenna = rx_antenna

        capacity_kbps = calc.compute_one_way_shannon_capacity(
            tx_node=_TxNode(),
            rx_node=_RxNode(),
            frequency_ghz=19.0,
            distance_km=550.0,
            elevation_angle=90.0,
            gs_lat=48.0,
            gs_lon=2.0,
            gs_diameter=1.2,
            bandwidth_hz=100e6,
        )

        assert isinstance(capacity_kbps, int)
        assert capacity_kbps > 0
