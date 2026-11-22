"""Tests for the optional BackendControlMixin manager surface."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from satgonetem.models.interface import Interface
from satgonetem.proto import netem_pb2
from satgonetem.services.mixins.backend_control import BackendControlMixin
from tests.support.factories import add_interface, make_link, make_satellite

pytestmark = pytest.mark.unit


class _ControlHost(BackendControlMixin):
    """Minimal TopologyManager host with loop and lock state."""

    def __init__(self, launcher=None):
        self.launcher = launcher
        self.satellites = {}
        self.ground_stations = {}
        self.links = {}
        self.running = False
        self.stop_calls = []
        self.lock_free_during_stop = None
        self._simulation_lock = threading.RLock()

    def add_satellite(self, name="Sat0"):
        node = make_satellite(name)
        self.satellites[node.id] = node
        return node

    def get_running(self):
        return self.running

    def _get_simulation_lock(self):
        return self._simulation_lock

    def stop(self):
        acquired = []

        def probe():
            lock = self._get_simulation_lock()
            got = lock.acquire(blocking=False)
            acquired.append(got)
            if got:
                lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        self.lock_free_during_stop = acquired == [True]
        self.running = False
        self.stop_calls.append("stop")


def _ok_update_response():
    response = netem_pb2.LinkUpdateManyResponse(
        status=netem_pb2.Status(code=netem_pb2.OK)
    )
    response.results.add(status=netem_pb2.Status(code=netem_pb2.OK))
    response.updated = 1
    return response


def _link_with_interfaces():
    source = make_satellite("Sat0")
    target = make_satellite("Sat1")
    link = make_link(source, target)
    source_interface = add_interface(source, f"Sat0.{target.id}")
    target_interface = add_interface(target, f"Sat1.{source.id}")
    source_interface.peer = target_interface
    target_interface.peer = source_interface
    link.peer_interfaces.extend([source_interface, target_interface])
    return link, source_interface, target_interface


# restart_node


def test_restart_node_calls_backend_and_clears_container_state():
    node = make_satellite("Sat0")
    node.container = "container-handle"
    node.container_pid = 4321
    launcher = SimpleNamespace(restart_node=MagicMock())
    host = _ControlHost(launcher)
    host.satellites[node.id] = node

    host.restart_node("Sat0")

    launcher.restart_node.assert_called_once_with("Sat0")
    assert node.container is None
    assert node.container_pid is None


def test_restart_node_clears_container_state_when_rpc_fails():
    node = make_satellite("Sat0")
    node.container = "container-handle"
    node.container_pid = 4321
    launcher = SimpleNamespace(
        restart_node=MagicMock(side_effect=RuntimeError("restart failed"))
    )
    host = _ControlHost(launcher)
    host.satellites[node.id] = node

    with pytest.raises(RuntimeError, match="restart failed"):
        host.restart_node("Sat0")

    assert node.container is None
    assert node.container_pid is None


def test_restart_node_rejects_unknown_node_before_rpc():
    launcher = SimpleNamespace(restart_node=MagicMock())
    host = _ControlHost(launcher)

    with pytest.raises(ValueError, match="Unknown node"):
        host.restart_node("Gnd9")

    launcher.restart_node.assert_not_called()


def test_restart_node_requires_a_capable_launcher():
    host = _ControlHost(SimpleNamespace())

    with pytest.raises(RuntimeError, match="restart_node"):
        host.restart_node("Sat0")


# set_interface_state / ifIndex mapping


def test_set_interface_state_maps_eth_suffix_to_if_index():
    node = make_satellite("Sat0")
    interface = add_interface(node, "Sat0.3")
    launcher = SimpleNamespace(set_interface_state=MagicMock())
    host = _ControlHost(launcher)
    host.satellites[node.id] = node

    host.set_interface_state("Sat0", interface, False)

    launcher.set_interface_state.assert_called_once_with("Sat0", 3, False)


def test_set_interface_state_accepts_interface_name():
    node = make_satellite("Sat0")
    add_interface(node, "Sat0.7")
    launcher = SimpleNamespace(set_interface_state=MagicMock())
    host = _ControlHost(launcher)
    host.satellites[node.id] = node

    host.set_interface_state("Sat0", "eth7", True)

    launcher.set_interface_state.assert_called_once_with("Sat0", 7, True)


def test_set_interface_state_rejects_loopback_and_unparseable_names():
    node = make_satellite("Sat0")
    bad_interface = Interface("Sat0.abc")
    launcher = SimpleNamespace(set_interface_state=MagicMock())
    host = _ControlHost(launcher)
    host.satellites[node.id] = node

    with pytest.raises(ValueError, match="eth<digits>"):
        host.set_interface_state("Sat0", node.loopback, True)
    with pytest.raises(ValueError, match="eth<digits>"):
        host.set_interface_state("Sat0", bad_interface, True)
    with pytest.raises(ValueError, match="eth<digits>"):
        host.set_interface_state("Sat0", "lo", True)

    launcher.set_interface_state.assert_not_called()


def test_set_interface_state_requires_bool_up_flag():
    node = make_satellite("Sat0")
    interface = add_interface(node, "Sat0.1")
    host = _ControlHost(SimpleNamespace(set_interface_state=MagicMock()))
    host.satellites[node.id] = node

    with pytest.raises(TypeError, match="up"):
        host.set_interface_state("Sat0", interface, "yes")


# set_link_state


def test_set_link_state_updates_admin_flag_and_both_endpoints():
    link, _source_interface, _target_interface = _link_with_interfaces()
    launcher = SimpleNamespace(set_interface_state=MagicMock())
    host = _ControlHost(launcher)
    host.satellites[link.source.id] = link.source
    host.satellites[link.target.id] = link.target

    host.set_link_state(link, False)

    assert link.admin_up is False
    assert launcher.set_interface_state.call_args_list == [
        call("Sat0", 1, False),
        call("Sat1", 0, False),
    ]


def test_set_link_state_up_restores_admin_flag_and_both_endpoints():
    link, source_interface, target_interface = _link_with_interfaces()
    link.admin_up = False
    launcher = SimpleNamespace(set_interface_state=MagicMock())
    host = _ControlHost(launcher)
    host.satellites[link.source.id] = link.source
    host.satellites[link.target.id] = link.target

    host.set_link_state(link, True)

    assert link.admin_up is True
    assert source_interface.is_active is True
    assert target_interface.is_active is True
    assert launcher.set_interface_state.call_args_list == [
        call("Sat0", 1, True),
        call("Sat1", 0, True),
    ]


def test_set_link_state_down_survives_a_topology_tick():
    link, source_interface, target_interface = _link_with_interfaces()
    launcher = SimpleNamespace(set_interface_state=MagicMock())
    host = _ControlHost(launcher)
    host.satellites[link.source.id] = link.source
    host.satellites[link.target.id] = link.target

    host.set_link_state(link, False)
    source_interface.is_active = True
    target_interface.is_active = True
    link.update_interfaces_state()

    assert source_interface.is_active is False
    assert target_interface.is_active is False


def test_set_link_state_raises_when_an_endpoint_interface_is_missing():
    source = make_satellite("Sat0")
    target = make_satellite("Sat1")
    link = make_link(source, target)
    add_interface(source, "Sat0.1")
    launcher = SimpleNamespace(set_interface_state=MagicMock())
    host = _ControlHost(launcher)
    host.satellites[source.id] = source
    host.satellites[target.id] = target

    with pytest.raises(RuntimeError, match="Sat1.0"):
        host.set_link_state(link, False)

    launcher.set_interface_state.assert_not_called()


def test_set_link_state_failure_leaves_previous_local_state_intact():
    """A failed endpoint RPC must not pin admin_up or either interface."""
    link, source_interface, target_interface = _link_with_interfaces()
    link.admin_up = True
    source_interface.is_active = True
    target_interface.is_active = True
    calls = []

    def set_state(node_name, if_index, up):
        calls.append((node_name, if_index, up))
        if len(calls) == 2:
            raise RuntimeError("second endpoint failed")

    launcher = SimpleNamespace(set_interface_state=MagicMock(side_effect=set_state))
    host = _ControlHost(launcher)
    host.satellites[link.source.id] = link.source
    host.satellites[link.target.id] = link.target

    with pytest.raises(RuntimeError, match="second endpoint failed"):
        host.set_link_state(link, False)

    assert calls == [
        ("Sat0", 1, False),
        ("Sat1", 0, False),
        ("Sat0", 1, True),
    ]
    assert link.admin_up is True
    assert source_interface.is_active is True
    assert target_interface.is_active is True

    # A later topology tick must not propagate an admin-down that the backend
    # never confirmed.
    link.update_interfaces_state()
    assert source_interface.is_active is True
    assert target_interface.is_active is True


def test_set_link_state_holds_the_simulation_lock_across_both_backend_calls():
    link, _source_interface, _target_interface = _link_with_interfaces()
    entered = threading.Event()
    release = threading.Event()

    def set_state(*_args):
        entered.set()
        assert release.wait(timeout=2.0)

    launcher = SimpleNamespace(set_interface_state=MagicMock(side_effect=set_state))
    host = _ControlHost(launcher)
    host.satellites[link.source.id] = link.source
    host.satellites[link.target.id] = link.target

    worker = threading.Thread(target=host.set_link_state, args=(link, False))
    worker.start()
    try:
        assert entered.wait(timeout=2.0)
        # The mutation must be serialized against the loop/API lock, and the
        # local pin must not be visible before both endpoint RPCs finish.
        assert host._simulation_lock.acquire(blocking=False) is False
        assert link.admin_up is True
    finally:
        release.set()
        worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert link.admin_up is False


def test_concurrent_link_impairment_updates_do_not_overlap():
    link, _source_interface, _target_interface = _link_with_interfaces()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def update_links(_links):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with state_lock:
            active -= 1
        return _ok_update_response()

    launcher = SimpleNamespace(update_links=MagicMock(side_effect=update_links))
    host = _ControlHost(launcher)
    errors = []

    def worker(loss):
        try:
            host.set_link_impairment(link, loss_percent=loss)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(loss,)) for loss in (1.0, 2.0)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert max_active == 1
    assert link.to_update is False


# stop_all_nodes / start_all_nodes


def test_stop_all_nodes_stops_the_loop_first_without_holding_its_lock():
    order = []
    launcher = SimpleNamespace(
        stop_all_nodes=MagicMock(side_effect=lambda: order.append("backend"))
    )
    host = _ControlHost(launcher)
    host.running = True
    original_stop = host.stop

    def stop():
        order.append("stop")
        original_stop()

    host.stop = stop

    host.stop_all_nodes()

    assert order == ["stop", "backend"]
    assert host.lock_free_during_stop is True
    launcher.stop_all_nodes.assert_called_once_with()


def test_stop_all_nodes_skips_loop_stop_when_not_running():
    launcher = SimpleNamespace(stop_all_nodes=MagicMock())
    host = _ControlHost(launcher)

    host.stop_all_nodes()

    assert host.stop_calls == []
    launcher.stop_all_nodes.assert_called_once_with()


def test_start_all_nodes_delegates_to_backend():
    launcher = SimpleNamespace(start_all_nodes=MagicMock())
    host = _ControlHost(launcher)

    host.start_all_nodes()

    launcher.start_all_nodes.assert_called_once_with()


# capture_interface


def test_capture_interface_maps_interface_and_writes_output(tmp_path):
    node = make_satellite("Sat0")
    interface = add_interface(node, "Sat0.2")
    launcher = SimpleNamespace(capture_interface=MagicMock(return_value=b"pcap-bytes"))
    host = _ControlHost(launcher)
    host.satellites[node.id] = node
    output = tmp_path / "capture.pcap"

    captured = host.capture_interface("Sat0", interface, output=output, timeout_s=4.0)

    assert captured == b"pcap-bytes"
    assert output.read_bytes() == b"pcap-bytes"
    assert list(tmp_path.iterdir()) == [output]
    launcher.capture_interface.assert_called_once_with("Sat0", 2, timeout_s=4.0)


def test_capture_interface_returns_bytes_without_output(tmp_path):
    node = make_satellite("Sat0")
    interface = add_interface(node, "Sat0.1")
    launcher = SimpleNamespace(capture_interface=MagicMock(return_value=b"pcap"))
    host = _ControlHost(launcher)
    host.satellites[node.id] = node

    assert host.capture_interface("Sat0", interface) == b"pcap"


# status / snapshots


def test_get_backend_status_returns_json_serializable_dict():
    payload = {"running": True, "nodes": [{"name": "Sat0", "interfaces": []}]}
    launcher = SimpleNamespace(get_backend_status=MagicMock(return_value=payload))
    host = _ControlHost(launcher)

    status = host.get_backend_status()

    assert status is payload
    json.dumps(status)
    launcher.get_backend_status.assert_called_once_with()


def test_save_snapshot_writes_project_archive_atomically(tmp_path):
    launcher = SimpleNamespace(save_project=MagicMock(return_value=b"gnet-archive"))
    host = _ControlHost(launcher)
    snapshot = tmp_path / "snapshot.gnet"

    host.save_snapshot(snapshot)

    assert snapshot.read_bytes() == b"gnet-archive"
    assert list(tmp_path.iterdir()) == [snapshot]


def test_save_snapshot_rejects_empty_backend_output(tmp_path):
    launcher = SimpleNamespace(save_project=MagicMock(return_value=b""))
    host = _ControlHost(launcher)
    snapshot = tmp_path / "snapshot.gnet"

    with pytest.raises(RuntimeError, match="no project data"):
        host.save_snapshot(snapshot)

    assert not snapshot.exists()


# set_link_impairment


def test_set_link_impairment_updates_fields_and_clears_pending_flag():
    link, _source_interface, _target_interface = _link_with_interfaces()
    launcher = SimpleNamespace(
        update_links=MagicMock(return_value=_ok_update_response())
    )
    host = _ControlHost(launcher)

    host.set_link_impairment(link, loss_percent=2.5, jitter_ms=15)

    assert link.loss == 2.5
    assert link.jitter == 15
    assert link.to_update is False
    launcher.update_links.assert_called_once_with([link])


def test_set_link_impairment_keeps_pending_flag_on_per_link_failure():
    link, _source_interface, _target_interface = _link_with_interfaces()
    response = netem_pb2.LinkUpdateManyResponse(
        status=netem_pb2.Status(code=netem_pb2.StatusCode.OK)
    )
    response.results.add(
        status=netem_pb2.Status(code=netem_pb2.StatusCode.ERROR, error="qdisc refused")
    )
    launcher = SimpleNamespace(update_links=MagicMock(return_value=response))
    host = _ControlHost(launcher)

    with pytest.raises(RuntimeError, match="qdisc refused"):
        host.set_link_impairment(link, loss_percent=5.0)

    assert link.loss == 5.0
    assert link.to_update is True


def test_set_link_impairment_keeps_pending_flag_on_top_level_failure():
    link, _source_interface, _target_interface = _link_with_interfaces()
    response = netem_pb2.LinkUpdateManyResponse(
        status=netem_pb2.Status(code=netem_pb2.StatusCode.ERROR, error="batch rejected")
    )
    launcher = SimpleNamespace(update_links=MagicMock(return_value=response))
    host = _ControlHost(launcher)

    with pytest.raises(RuntimeError, match="batch rejected"):
        host.set_link_impairment(link, jitter_ms=3)

    assert link.to_update is True


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"loss_percent": -1.0}, ValueError),
        ({"loss_percent": 101.0}, ValueError),
        ({"loss_percent": float("nan")}, ValueError),
        ({"jitter_ms": -1}, ValueError),
        ({"jitter_ms": 1.5}, TypeError),
    ],
)
def test_set_link_impairment_validates_before_touching_backend(kwargs, error):
    link, _source_interface, _target_interface = _link_with_interfaces()
    launcher = SimpleNamespace(update_links=MagicMock())
    host = _ControlHost(launcher)

    with pytest.raises(error):
        host.set_link_impairment(link, **kwargs)

    launcher.update_links.assert_not_called()
    assert link.to_update is False


def test_set_link_impairment_requires_at_least_one_field():
    link, _source_interface, _target_interface = _link_with_interfaces()
    host = _ControlHost(SimpleNamespace(update_links=MagicMock()))

    with pytest.raises(ValueError, match="loss_percent or jitter_ms"):
        host.set_link_impairment(link)

    assert link.to_update is False


def test_set_link_impairment_requires_update_links_capability():
    link, _source_interface, _target_interface = _link_with_interfaces()
    host = _ControlHost(SimpleNamespace())

    with pytest.raises(RuntimeError, match="update_links"):
        host.set_link_impairment(link, loss_percent=1.0)

    assert link.to_update is True
