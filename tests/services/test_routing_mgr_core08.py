"""Regression tests for CORE-08: routing re-initialization lifecycle.

These tests exercise ``RoutingManagerMixin.init_routing`` with fake daemon
classes so no containers or network namespaces are required.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from satgonetem.services.mixins.routing_mgr import RoutingManagerMixin
from satgonetem.utils.constants import MAX_WORKERS

STATIC_DAEMON_PATH = "satgonetem.services.mixins.routing_mgr.StaticRoutingDaemon"
OSPF_DAEMON_PATH = "satgonetem.services.mixins.routing_mgr.OSPFDaemon"


class RecordingDaemon:
    """Minimal routing daemon that records lifecycle calls."""

    def __init__(
        self,
        topology,
        *,
        name,
        events=None,
        init_result=True,
        init_error=None,
        remove_error=None,
    ):
        self.topology = topology
        self.name = name
        self.events = events
        self.init_result = init_result
        self.init_error = init_error
        self.remove_error = remove_error
        self.init_workers = None
        self.remove_calls = []

    def init(self, max_workers=4):
        self.init_workers = max_workers
        if self.events is not None:
            self.events.append(f"init:{self.name}")
        if self.init_error is not None:
            raise self.init_error
        return self.init_result

    def remove(self, node=None, max_workers=4):
        self.remove_calls.append((node, max_workers))
        if self.events is not None:
            self.events.append(f"remove:{self.name}")
        if self.remove_error is not None:
            raise self.remove_error

    def update(self, new_links, max_workers=4):
        pass


class DaemonFactory:
    """Callable daemon factory that keeps hold of created instances."""

    def __init__(
        self,
        name,
        *,
        events=None,
        init_result=True,
        init_error=None,
        remove_error=None,
    ):
        self.name = name
        self.events = events
        self.init_result = init_result
        self.init_error = init_error
        self.remove_error = remove_error
        self.instances = []

    def __call__(self, topology):
        if self.events is not None:
            self.events.append(f"construct:{self.name}")
        daemon = RecordingDaemon(
            topology,
            name=self.name,
            events=self.events,
            init_result=self.init_result,
            init_error=self.init_error,
            remove_error=self.remove_error,
        )
        self.instances.append(daemon)
        return daemon


class DummyRoutingTopology(RoutingManagerMixin):
    """Mixin host with only the state needed by init_routing()."""

    def __init__(self, routing="static"):
        self.routing = routing
        self.routing_daemon = None
        self.routing_initiated = False
        self._daemon_registry = {}

    def get_routing_initiated(self):
        return self.routing_initiated

    def set_routing_initiated(self, value):
        self.routing_initiated = value


def test_override_does_not_mutate_configured_routing_method():
    topology = DummyRoutingTopology(routing="static")
    ospf_factory = DaemonFactory("ospf")

    with patch(OSPF_DAEMON_PATH, new=ospf_factory):
        elapsed = topology.init_routing(routing_method="dynamic-ospf", max_workers=3)

    assert topology.routing == "static"
    assert topology.routing_daemon is ospf_factory.instances[0]
    assert topology.routing_initiated is True
    assert elapsed >= 0

    # A later call without an override must use the original method, proving
    # the previous override was not persisted on the instance.
    static_factory = DaemonFactory("static")
    with patch(STATIC_DAEMON_PATH, new=static_factory):
        topology.init_routing(max_workers=2)

    assert topology.routing == "static"
    assert topology.routing_daemon is static_factory.instances[0]


def test_existing_daemon_removed_before_replacement_is_constructed():
    events = []
    topology = DummyRoutingTopology()
    old_daemon = MagicMock()
    old_daemon.remove.side_effect = lambda **kwargs: events.append("remove:old")
    topology.routing_daemon = old_daemon
    topology.routing_initiated = True

    factory = DaemonFactory("static", events=events)
    with patch(STATIC_DAEMON_PATH, new=factory):
        topology.init_routing(max_workers=5)

    old_daemon.remove.assert_called_once_with(max_workers=5)
    assert events[:2] == ["remove:old", "construct:static"]
    assert topology.routing_daemon is factory.instances[0]


def test_success_installs_new_daemon_and_sets_initiated_flag():
    topology = DummyRoutingTopology()
    factory = DaemonFactory("static")

    with patch(STATIC_DAEMON_PATH, new=factory):
        elapsed = topology.init_routing(max_workers=7)

    assert elapsed >= 0
    assert factory.instances[0].init_workers == 7
    assert topology.routing_daemon is factory.instances[0]
    assert topology.routing_initiated is True


def test_init_failure_clears_daemon_and_flag_and_returns_minus_one():
    topology = DummyRoutingTopology()
    old_daemon = MagicMock()
    topology.routing_daemon = old_daemon
    topology.routing_initiated = True
    factory = DaemonFactory("static", init_result=False)

    with patch(STATIC_DAEMON_PATH, new=factory):
        elapsed = topology.init_routing(max_workers=6)

    assert elapsed == -1.0
    assert topology.routing_daemon is None
    assert topology.routing_initiated is False
    assert factory.instances[0].remove_calls == [(None, 6)]


def test_old_daemon_teardown_error_is_logged_but_not_fatal(caplog):
    topology = DummyRoutingTopology()
    old_daemon = MagicMock()
    old_daemon.remove.side_effect = RuntimeError("teardown boom")
    topology.routing_daemon = old_daemon
    topology.routing_initiated = True
    factory = DaemonFactory("static")

    with caplog.at_level(logging.ERROR):
        with patch(STATIC_DAEMON_PATH, new=factory):
            elapsed = topology.init_routing(max_workers=4)

    assert elapsed >= 0
    assert "Failed to tear down existing routing daemon" in caplog.text
    assert topology.routing_daemon is factory.instances[0]
    assert topology.routing_initiated is True


def test_delete_routing_still_removes_the_installed_daemon():
    topology = DummyRoutingTopology()
    factory = DaemonFactory("static")

    with patch(STATIC_DAEMON_PATH, new=factory):
        topology.init_routing()

    elapsed = topology.delete_routing()

    assert elapsed >= 0
    assert factory.instances[0].remove_calls == [(None, MAX_WORKERS)]
    assert topology.routing_daemon is None
    assert topology.routing_initiated is False
