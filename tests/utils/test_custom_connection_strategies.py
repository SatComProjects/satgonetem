"""Tests that custom connection strategies are registered on import."""

from sat_com_connectivity.ground_object_to_space_connectivity.ground_connection_strategy_registry import (
    GROUND_TO_SPACE_CONNECTION_STRATEGY_REGISTRY,
    create_strategy,
)

import satgonetem.utils  # noqa: F401  (registers the built-in strategies)
from satgonetem.utils.custom_connection_strategies import (
    LongestConnectionTimeStrategy,
    WeightedConnection,
)


def test_longest_connection_time_strategy_is_registered():
    assert (
        GROUND_TO_SPACE_CONNECTION_STRATEGY_REGISTRY.get(
            "longest-connection-time-strategy"
        )
        is LongestConnectionTimeStrategy
    )


def test_weighted_connection_strategy_is_registered():
    assert (
        GROUND_TO_SPACE_CONNECTION_STRATEGY_REGISTRY.get("weighted-connection")
        is WeightedConnection
    )


def test_registered_strategies_can_be_created():
    assert isinstance(
        create_strategy("longest-connection-time-strategy"),
        LongestConnectionTimeStrategy,
    )
    assert isinstance(create_strategy("weighted-connection"), WeightedConnection)


def test_weighted_connection_handles_90_deg_elevation_mask(monkeypatch):
    from types import SimpleNamespace

    from satgonetem.utils import custom_connection_strategies as ccs

    strategy = WeightedConnection(maximum_connected_stations=2)
    sat = SimpleNamespace(links=[])
    vis = SimpleNamespace(satellite=sat, elevation_above_horizon=90.0)
    gnd = SimpleNamespace(
        links=[],
        ground_object_domain=SimpleNamespace(
            elevation_above_horizon=90.0,
            maximum_connected_satellites=1,
        ),
    )
    monkeypatch.setattr(
        ccs, "get_visible_satellites", lambda satellites, ground_object: [vis]
    )
    monkeypatch.setattr(
        ccs,
        "GroundToSpaceConnection",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    result = strategy.compute_ground_object_connection_strategy(gnd, [sat])
    assert len(result.new_ground_object_connections) == 1
