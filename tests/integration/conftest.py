"""Fixtures for the live integration tier.

Every fixture here talks to real Docker/GoNetEm resources. Prerequisite
failures are hard failures with remediation, never skips.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.support.live import (
    LiveEnvironment,
    require_live_environment,
    teardown_live_topology,
    unique_project_name,
)

if TYPE_CHECKING:
    from satgonetem.services.topology_satcom import TopologyManager

_ROOT_COMMAND = (
    'sudo -E env PATH="$PATH" .venv/bin/python -m pytest tests/integration '
    '-q -m "integration and root"'
)
_CAP_NET_ADMIN = 12
_CAP_SYS_ADMIN = 21


@pytest.fixture(scope="session")
def live_env() -> Iterator[LiveEnvironment]:
    """Return the verified live environment or fail with every missing item."""
    environment = require_live_environment()
    try:
        yield environment
    finally:
        environment.docker.close()


def _read_tle(path: Path, count: int) -> list[dict[str, str]]:
    """Read the first *count* three-line TLE entries from *path*."""
    lines = path.read_text().splitlines()
    if len(lines) < count * 3:
        raise RuntimeError(f"{path} does not contain {count} TLE entries")
    return [
        {
            "name": lines[3 * index].strip(),
            "tle_line1": lines[3 * index + 1].strip(),
            "tle_line2": lines[3 * index + 2].strip(),
        }
        for index in range(count)
    ]


def _build_three_node_topology(repo_root: Path) -> TopologyManager:
    """Build two TLE satellites + Paris with one ISL and one GSL.

    Mirrors ``scripts/verify_connectivity.py`` without importing the untracked
    manual script.
    """
    from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
    from satgonetem.utils.project_builder import (
        add_custom_ground_station,
        create_and_load_simulation,
        create_custom_ground_station,
        create_custom_satellite,
        create_empty_project,
    )

    base_name = unique_project_name("LiveThreeNode")
    project = create_empty_project(
        simulation_name=base_name,
        start_date="03/11/2026 13:00:00",
        end_date="03/11/2026 13:10:00",
    )
    simulation = create_and_load_simulation(
        project.model_dump(), project.simulation_name
    )

    for tle in _read_tle(repo_root / "tle_files" / "oneweb.txt", 2):
        create_custom_satellite(tle, simulation)
    ground_station = create_custom_ground_station("Paris", 48.8566, 2.3522, 0.035)
    add_custom_ground_station(ground_station, simulation)
    simulation.update_ground_station_links()

    satcom_sats = simulation.get_satellites()
    satcom_gss = simulation.get_ground_stations()
    simulation.create_and_add_inter_satellite_link_connection(
        satcom_sats[0], satcom_sats[1]
    )
    simulation.create_and_add_ground_station_link_connection(
        satcom_sats[0], satcom_gss[0]
    )

    topology = TopologyManager.from_simulation_manager(
        simulation,
        NetworkConfig(update_time=1, project_name=base_name),
    )
    sat0 = topology.satellites[satcom_sats[0].topology_uniq_id]
    sat1 = topology.satellites[satcom_sats[1].topology_uniq_id]
    topology.set_addressable_satellites([sat0, sat1])
    return topology


@pytest.fixture(scope="module")
def live_topology(
    live_env: LiveEnvironment, repo_root: Path
) -> Iterator[TopologyManager]:
    """Start a three-node topology, configure it, and always tear it down."""
    topology = _build_three_node_topology(repo_root)
    project_id: str | None = None
    try:
        topology.start_gonetem()
        project_id = topology.project_id
        if not project_id:
            raise RuntimeError("GoNetEm returned an empty project id")
        topology.set_ip_addresses()
        topology.init_routing(routing_method="static")
        if not topology.get_routing_initiated():
            raise RuntimeError("static routing initialisation failed")
        yield topology
    finally:
        if project_id is None:
            project_id = topology.project_id
        teardown_live_topology(topology, live_env.docker, project_id)


def _effective_capabilities() -> int:
    """Return the effective capability bitmask from ``/proc/self/status``."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                return int(line.split()[1], 16)
    except (OSError, IndexError, ValueError):
        return 0
    return 0


@pytest.fixture
def require_root() -> None:
    """Fail with the exact sudo command unless root capabilities are present."""
    missing: list[str] = []
    if os.geteuid() != 0:
        missing.append(f"effective uid is {os.geteuid()}, not 0")
    capabilities = _effective_capabilities()
    if not capabilities & (1 << _CAP_NET_ADMIN):
        missing.append("CAP_NET_ADMIN is not in the effective capability set")
    if not capabilities & (1 << _CAP_SYS_ADMIN):
        missing.append("CAP_SYS_ADMIN is not in the effective capability set")

    if missing:
        pytest.fail(
            "Root capabilities are required for this test: "
            + "; ".join(missing)
            + ".\nThe root live tier needs root (or effective CAP_NET_ADMIN and "
            "CAP_SYS_ADMIN) to call setns and create veth pairs.\n"
            "Re-run it with:\n"
            f"  {_ROOT_COMMAND}",
            pytrace=False,
        )
