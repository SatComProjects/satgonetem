"""Direct contract tests for the extracted sat_com project helpers."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sat_com_application.simulation_manager import SimulationManager
from sat_com_application.time_managers import SimulationTimeManager

from satgonetem.services.satcom_project import load_simulation, require_tick_action
from satgonetem.services.topology_satcom import TopologyManager


@pytest.fixture(scope="module")
def topology():
    path = Path(__file__).resolve().parents[2] / "topology_files/test_topology.json"
    return TopologyManager.from_file(str(path))


class TestLoadSimulation:
    def test_loads_the_project_into_a_simulation_manager(self, topology):
        sim_manager = load_simulation(topology.simulation_property)

        assert isinstance(sim_manager, SimulationManager)
        assert [sat.satellite_name for sat in sim_manager.get_satellites()] == [
            sat.satellite_name for sat in topology.simulation_manager.get_satellites()
        ]


class TestRequireTickAction:
    def test_accepts_a_time_manager_with_a_tick_action(self, topology):
        require_tick_action(topology.simulation_manager)

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="Simulation manager is not set"):
            require_tick_action(None)

    def test_rejects_a_time_manager_without_a_tick_action(self):
        time_manager = SimpleNamespace(on_tick_actions=[])

        with pytest.raises(ValueError, match="no tick action"):
            require_tick_action(SimpleNamespace(time_manager=time_manager))

    def test_facade_delegate_keeps_the_original_errors(self):
        manager = TopologyManager.__new__(TopologyManager)

        with pytest.raises(ValueError, match="Simulation manager is not set"):
            manager._setup_update_actions()

        manager.simulation_manager = SimpleNamespace(
            time_manager=SimpleNamespace(on_tick_actions=[])
        )
        with pytest.raises(ValueError, match="no tick action"):
            manager._setup_update_actions()

    def test_real_time_manager_registers_a_tick_action(self):
        time_manager = SimulationTimeManager(datetime(2024, 1, 1), datetime(2024, 1, 2))
        time_manager.register_action(lambda: None)

        require_tick_action(SimpleNamespace(time_manager=time_manager))
