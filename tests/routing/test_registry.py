"""Tests for the routing daemon registry and context boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import networkx as nx
import pytest

from satgonetem.routing import (
    ISISBirdSRDaemon,
    OSPFDaemon,
    RoutingContext,
    RoutingDaemon,
    SRMPLSDaemon,
    StaticRoutingDaemon,
)
from satgonetem.routing.registry import (
    available_methods,
    create_daemon,
    is_known_method,
)
from tests.support import DummyTopology

pytestmark = pytest.mark.unit


class _DummyDaemon(RoutingDaemon):
    def __init__(self, topology):
        super().__init__(topology)
        self.calls = []

    def init(self, max_workers=4):
        self.calls.append(("init", max_workers))
        return True

    def update(self, new_links, max_workers=4):
        self.calls.append(("update", list(new_links), max_workers))

    def remove(self, node=None, max_workers=4):
        self.calls.append(("remove", node, max_workers))


def _context() -> DummyTopology:
    topology = DummyTopology(status=True, use_file_routes=False, project_name="p")
    topology.current_time_step = 0
    topology.get_satellites = lambda: list(topology.satellites.values())
    topology.get_ground_stations = lambda: list(topology.ground_stations.values())
    topology.get_current_graph = nx.Graph
    topology._get_interface_to_peer = lambda node, peer_id: None
    return topology


def test_builtin_methods_resolve_without_manager_dispatch():
    topology = _context()

    daemon = create_daemon("static", topology)

    assert isinstance(daemon, StaticRoutingDaemon)
    assert set(available_methods()) == {
        "static",
        "dynamic-ospf",
        "dynamic-isis",
        "sr-mpls",
    }
    assert all(is_known_method(name) for name in available_methods())


def test_custom_factory_takes_precedence_over_builtin():
    topology = _context()
    created = []

    def factory(host):
        daemon = _DummyDaemon(host)
        created.append(daemon)
        return daemon

    daemon = create_daemon("static", topology, custom={"static": factory})

    assert daemon is created[0]


def test_unknown_method_returns_none():
    assert create_daemon("does-not-exist", _context()) is None
    assert is_known_method("does-not-exist") is False


def test_builtin_classes_satisfy_the_daemon_contract():
    for daemon_class in (
        StaticRoutingDaemon,
        OSPFDaemon,
        ISISBirdSRDaemon,
        SRMPLSDaemon,
    ):
        assert issubclass(daemon_class, RoutingDaemon)
        daemon = daemon_class(_context())
        assert hasattr(daemon, "init")
        assert hasattr(daemon, "update")
        assert hasattr(daemon, "remove")


def test_routing_modules_do_not_import_services():
    routing_dir = Path(__file__).resolve().parents[2] / "satgonetem" / "routing"
    violations = []
    for path in routing_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("satgonetem.services"):
                    violations.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("satgonetem.services"):
                        violations.append(f"{path.name}:{node.lineno}")
    assert violations == []


def test_routing_context_is_runtime_checkable_protocol():
    assert isinstance(_context(), RoutingContext)
