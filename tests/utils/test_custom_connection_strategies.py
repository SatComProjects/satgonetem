"""Tests that custom connection strategies are registered on import."""

import satgonetem.utils  # noqa: F401  (registers the built-in strategies)
from sat_com_connectivity.ground_object_to_space_connectivity.ground_connection_strategy_registry import (
    GROUND_TO_SPACE_CONNECTION_STRATEGY_REGISTRY,
    create_strategy,
)
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
