"""Tests for SR-MPLS label allocation and path planning."""

from __future__ import annotations

import pytest

from satgonetem.routing.srmpls_label_manager import (
    MPLS_LABEL_MAX,
    MPLS_LABEL_MIN,
    NodeSID,
    SegmentRoutingManager,
)

pytestmark = pytest.mark.unit


def test_node_sid_equality_and_hash_use_node_id():
    first = NodeSID(node_id=1, label=16001, node_name="Sat1")
    second = NodeSID(node_id=1, label=99999)
    other = NodeSID(node_id=2, label=16002)

    assert first == second
    assert first != other
    assert hash(first) == hash(second)


def test_node_sid_base_is_clamped_to_minimum():
    assert SegmentRoutingManager(node_sid_base=1).node_sid_base == MPLS_LABEL_MIN


def test_allocate_node_sid_is_deterministic_and_idempotent():
    manager = SegmentRoutingManager(node_sid_base=16000)

    first = manager.allocate_node_sid(3, "Sat3")
    again = manager.allocate_node_sid(3, "ignored")

    assert first.label == 16003
    assert again is first
    assert manager.get_node_sid(3) == 16003
    assert manager.get_node_sid(4) is None
    assert manager.get_node_for_sid(16003) == 3
    assert manager.get_node_for_sid(1) is None


def test_allocate_node_sid_rejects_label_overflow():
    manager = SegmentRoutingManager(node_sid_base=MPLS_LABEL_MAX)

    with pytest.raises(RuntimeError, match="exceeds maximum"):
        manager.allocate_node_sid(1)


@pytest.mark.parametrize(
    "path, error",
    [
        ([1], "at least 2"),
        ([2, 3], "start with source"),
        ([1, 3], "end with destination"),
    ],
)
def test_set_custom_path_validates_endpoints(path, error):
    manager = SegmentRoutingManager()

    with pytest.raises(ValueError, match=error):
        manager.set_custom_path(1, 2, path)


def test_custom_path_lifecycle_and_clear():
    manager = SegmentRoutingManager()

    manager.set_custom_path(1, 3, [1, 2, 3])
    assert manager.get_custom_path(1, 3) == [1, 2, 3]
    assert manager.clear_custom_path(1, 3) is True
    assert manager.get_custom_path(1, 3) is None
    assert manager.clear_custom_path(1, 3) is False


def test_label_stack_uses_transit_sids_and_php():
    manager = SegmentRoutingManager()
    for node_id in (1, 2, 3, 4):
        manager.allocate_node_sid(node_id)

    assert manager.get_label_stack_for_path([1, 2, 3, 4]) == [16002, 16003]
    assert manager.get_label_stack_for_path([1, 2, 3, 4], use_php=False) == [
        16002,
        16003,
        16004,
    ]
    assert manager.get_label_stack_for_path([1]) == []


def test_label_stack_requires_every_transit_sid():
    manager = SegmentRoutingManager()
    manager.allocate_node_sid(1)

    with pytest.raises(ValueError, match="does not have a Node SID"):
        manager.get_label_stack_for_path([1, 2, 3])


def test_route_prefers_custom_path_and_caches_default_stack():
    manager = SegmentRoutingManager()
    for node_id in (1, 2, 3):
        manager.allocate_node_sid(node_id)

    default_path, labels = manager.get_sr_route_for_destination(
        1, 3, default_path=[1, 2, 3]
    )
    assert default_path == [1, 2, 3]
    assert labels == [16002]
    assert manager.get_statistics()["cached_stacks"] == 1

    manager.set_custom_path(1, 3, [1, 3])
    path, labels = manager.get_sr_route_for_destination(1, 3, default_path=[1, 2, 3])
    assert path == [1, 3]
    assert labels == []


def test_route_returns_empty_for_missing_path():
    manager = SegmentRoutingManager()

    assert manager.get_sr_route_for_destination(1, 2) == ([], [])
    assert manager.get_sr_route_for_destination(1, 2, default_path=[1]) == ([], [])


def test_label_stack_cache_distinguishes_php():
    manager = SegmentRoutingManager()
    for node_id in (1, 2, 3):
        manager.allocate_node_sid(node_id)

    _, with_php = manager.get_sr_route_for_destination(
        1, 3, default_path=[1, 2, 3], use_php=True
    )
    _, without_php = manager.get_sr_route_for_destination(
        1, 3, default_path=[1, 2, 3], use_php=False
    )

    assert with_php == [16002]
    assert without_php == [16002, 16003]


def test_label_stack_cache_distinguishes_the_default_path():
    manager = SegmentRoutingManager()
    for node_id in (1, 2, 3, 4):
        manager.allocate_node_sid(node_id)

    _, first = manager.get_sr_route_for_destination(
        1, 3, default_path=[1, 2, 3], use_php=True
    )
    _, second = manager.get_sr_route_for_destination(
        1, 3, default_path=[1, 4, 3], use_php=True
    )

    assert first == [16002]
    assert second == [16004]


def test_custom_path_change_invalidates_every_cached_stack_for_the_pair():
    manager = SegmentRoutingManager()
    for node_id in (1, 2, 3, 4):
        manager.allocate_node_sid(node_id)
    manager.get_sr_route_for_destination(1, 3, default_path=[1, 2, 3], use_php=True)
    manager.get_sr_route_for_destination(1, 3, default_path=[1, 4, 3], use_php=False)
    assert manager.get_statistics()["cached_stacks"] == 2

    manager.set_custom_path(1, 3, [1, 3])

    assert manager.get_statistics()["cached_stacks"] == 0


class _MutatingKey(int):
    """An int that inserts a cache entry when the cache compares it."""

    __hash__ = int.__hash__

    def __new__(cls, value, cache):
        key = super().__new__(cls, value)
        key.cache = cache
        return key

    def __eq__(self, other):
        self.cache.setdefault((99, 99, True, (1,)), [])
        return super().__eq__(other)


def test_invalidating_a_pair_tolerates_inserts_while_iterating():
    manager = SegmentRoutingManager()
    cache = manager._label_stack_cache
    mutating = _MutatingKey(1, cache)
    cache[(mutating, 3, True, (2, 3))] = [16002]
    cache[(1, 4, True, (2, 4))] = [16003]

    manager._invalidate_pair_cache(1, 3)

    assert (mutating, 3, True, (2, 3)) not in cache
    assert (1, 4, True, (2, 4)) in cache
    assert (99, 99, True, (1,)) in cache, "the concurrent insert must be preserved"


def test_statistics_reset_cache_and_str():
    manager = SegmentRoutingManager()
    manager.allocate_node_sid(1, "Sat1")
    manager.set_custom_path(1, 2, [1, 2])
    manager.get_sr_route_for_destination(1, 2, default_path=[1, 2])

    stats = manager.get_statistics()
    assert stats["node_sids_allocated"] == 1
    assert stats["custom_paths"] == 1
    assert "1 Node SIDs" in str(manager)

    manager.clear_cache()
    assert manager.get_statistics()["cached_stacks"] == 0
    manager.reset()
    assert manager.get_statistics()["node_sids_allocated"] == 0
    assert manager.get_statistics()["custom_paths"] == 0
