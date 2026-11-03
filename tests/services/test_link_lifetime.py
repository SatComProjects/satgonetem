"""Tests for link-lifetime APIs and opt-in expiry routing."""

from __future__ import annotations

from types import SimpleNamespace

from satgonetem.services.topology_satcom import TopologyManager


def _manager() -> TopologyManager:
    manager = TopologyManager.from_file("topology_files/test_topology.json")
    manager.preference = "hops_no_preference"
    return manager


def _gsl_with_lifetime(manager: TopologyManager):
    return next(
        link
        for link in manager.links.values()
        if link.type == "GroundStationLink"
        and link.estimated_disconnection_time is not None
    )


def test_link_lifetime_report_contains_v5_metadata():
    manager = _manager()
    link = _gsl_with_lifetime(manager)

    lifetime = manager.get_link_lifetime(link)

    assert lifetime["estimated_disconnection_time"] is not None
    assert lifetime["remaining_duration_s"] is not None
    assert lifetime["remaining_duration_s"] >= 0.0
    assert lifetime["elevation_deg"] is not None


def test_expiring_links_filters_by_horizon():
    manager = _manager()
    link = _gsl_with_lifetime(manager)

    assert manager.get_expiring_links(0.0) == []
    assert link in manager.get_expiring_links(1_000_000.0)


def test_expiry_penalty_increases_edge_weight():
    manager = _manager()
    link = _gsl_with_lifetime(manager)
    source_id = link.source.satcom_object.topology_uniq_id
    target_id = link.target.satcom_object.topology_uniq_id

    baseline = manager.get_current_graph()
    baseline_weight = baseline[source_id][target_id]["weight"]

    manager.link_expiry_horizon_s = 1_000_000.0
    manager.link_expiry_penalty = 50.0
    penalized = manager.get_current_graph()

    assert penalized[source_id][target_id]["weight"] > baseline_weight
    assert "remaining_duration_s" in penalized[source_id][target_id]


def test_path_lifetime_and_flow_guard():
    manager = _manager()
    link = _gsl_with_lifetime(manager)
    flow = SimpleNamespace(source=link.source, destination=link.target)

    remaining = manager.get_path_min_link_lifetime_s(link.source, link.target)
    assert remaining is not None

    assert manager.make_flow_guard(remaining + 1.0)(flow) is False
    assert manager.make_flow_guard(max(1.0, remaining - 1.0))(flow) is True


def test_topology_summary_includes_lifetime_counts():
    manager = _manager()

    summary = manager.get_topology_summary()

    assert summary["links_with_known_lifetime"] > 0
    assert summary["min_remaining_link_duration_s"] is not None


def test_polling_tick_refreshes_ground_link_lifetime():
    manager = _manager()
    link = _gsl_with_lifetime(manager)

    manager.next_step()

    assert link.estimated_duration_s is not None
    assert link.estimated_disconnection_time is not None
    assert link.elevation_deg is not None
