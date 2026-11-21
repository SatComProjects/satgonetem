"""Tests for pure topology link identity/diff helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from satgonetem.services.topology_link_plan import (
    build_link_key,
    removed_link_keys,
)
from tests.support import make_ground_station, make_satellite

pytestmark = pytest.mark.unit


def _with_uid(node, uid: int):
    node.satcom_object = SimpleNamespace(topology_uniq_id=uid)
    return node


def test_link_key_is_direction_independent_for_local_nodes():
    sat = _with_uid(make_satellite("Sat1"), 10)
    gnd = _with_uid(make_ground_station("Gnd2"), 20)

    assert build_link_key(sat, gnd) == frozenset({"Sat10", "Gnd20"})
    assert build_link_key(gnd, sat) == build_link_key(sat, gnd)


@pytest.mark.parametrize(
    "source, target, expected",
    [
        ("sat", "sat", {"Sat1", "Sat2"}),
        ("gnd", "gnd", {"Gnd1", "Gnd2"}),
        ("sat", "gnd", {"Sat1", "Gnd2"}),
    ],
)
def test_link_key_type_combinations(source, target, expected):
    nodes = {
        "sat": _with_uid(make_satellite("Sat1"), 1),
        "sat2": _with_uid(make_satellite("Sat2"), 2),
        "gnd": _with_uid(make_ground_station("Gnd1"), 1),
        "gnd2": _with_uid(make_ground_station("Gnd2"), 2),
    }
    left = nodes[source]
    right = nodes[target if target != source else source + "2"]
    if source == "sat" and target == "gnd":
        right = nodes["gnd2"]

    assert build_link_key(left, right) == frozenset(expected)


def test_link_key_rejects_unknown_types():
    with pytest.raises(ValueError, match="Satellite or GroundStation"):
        build_link_key(object(), object())


def test_removed_link_keys_returns_only_missing_keys():
    current = {frozenset({"Sat1", "Sat2"}), frozenset({"Sat2", "Sat3"})}
    new = {frozenset({"Sat1", "Sat2"})}

    assert removed_link_keys(current, new) == {frozenset({"Sat2", "Sat3"})}
