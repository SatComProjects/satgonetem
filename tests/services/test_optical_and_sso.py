"""Integration tests for optical ISL metrics and mixed SSO/Walker projects."""

from __future__ import annotations

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.utils.project_builder import (
    create_sso_walker_project,
    create_test_project,
)

START = "01/01/2024 00:00:00"


def _manager(project) -> TopologyManager:
    return TopologyManager.from_satcom(
        project,
        NetworkConfig(update_time=5),
    )


def test_basic_optical_model_has_no_metrics():
    manager = _manager(
        create_test_project(
            start_date=START,
            optical_inter_satellite_link_model="basic",
        )
    )
    link = next(
        link
        for link in manager.links.values()
        if link.type == "InterSatelliteLink"
    )

    assert link.optical_link_model == "basic"
    assert link.optical_metrics["source"] is None
    assert link.optical_metrics["destination"] is None


def test_advanced_optical_model_populates_metrics():
    manager = _manager(
        create_test_project(
            start_date=START,
            optical_inter_satellite_link_model="advanced",
        )
    )
    link = next(
        link
        for link in manager.links.values()
        if link.type == "InterSatelliteLink"
    )

    assert link.optical_link_model == "advanced"
    assert link.optical_metrics["source"] is not None
    assert link.optical_metrics["destination"] is not None
    source = link.optical_metrics["source"]
    assert source["azimuth_deg"] is not None
    assert source["relative_azimuth_deg"] is not None
    assert source["elevation_deg"] is not None
    assert source["angular_velocity_deg_s"] is not None
    assert source["range_km"] is not None
    assert source["los_min_altitude_km"] is not None

    metrics = manager.get_link_optical_metrics(link)
    assert metrics["model"] == "advanced"
    assert metrics["source"]["azimuth_deg"] == source["azimuth_deg"]
    assert manager.get_topology_summary()["advanced_optical_links"] > 0


def test_sso_walker_project_creates_inter_shell_links():
    manager = _manager(create_sso_walker_project(start_date=START))

    assert len(manager.simulation_manager.get_sso_shells()) == 1
    assert len(manager.simulation_manager.get_walker_shells()) == 1
    inter_shell = [
        link for link in manager.links.values() if link.direction == "InterShell"
    ]
    assert inter_shell

    graph = manager.get_current_graph()
    for link in inter_shell:
        assert graph.has_edge(
            link.source.satcom_object.topology_uniq_id,
            link.target.satcom_object.topology_uniq_id,
        )
