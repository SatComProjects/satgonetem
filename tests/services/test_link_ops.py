"""Tests for local link-operation ordering and thread safety."""

import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import grpc
import pytest

from satgonetem.proto import netem_pb2
from satgonetem.services.mixins.link_ops import LinkOpsMixin
from satgonetem.utils.constants import ROUTE_APPLY_WORKERS


def _update_response(codes, *, error=""):
    """Build a real LinkUpdateManyResponse with one result per status code."""
    response = netem_pb2.LinkUpdateManyResponse(
        status=netem_pb2.Status(code=netem_pb2.OK)
    )
    for code in codes:
        response.results.add(
            status=netem_pb2.Status(code=code, error=error),
            peer1="Sat0.0",
            peer2="Sat1.1",
        )
    response.updated = sum(1 for code in codes if code == netem_pb2.OK)
    return response


class _UnimplementedRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNIMPLEMENTED


class _FakeTopology(LinkOpsMixin):
    """Minimal topology host for exercising LinkOpsMixin methods."""

    def __init__(self, links, **kwargs):
        self.links = links
        self.hil_manager = kwargs.pop("hil_manager", None)
        self.launcher = kwargs.pop("launcher", None)
        override_update = kwargs.pop("_execute_link_update", None)
        if override_update is not None:
            self._execute_link_update = override_update
        for name, value in kwargs.items():
            setattr(self, name, value)

    def get_status(self):
        return True



def test_interface_removals_run_on_the_calling_thread():
    removal_threads = []
    fake = SimpleNamespace(
        _remove_link_interfaces=lambda link: removal_threads.append(
            threading.current_thread()
        ),
        _update_link_flags=MagicMock(),
        _add_link_interfaces=MagicMock(),
        _cleanup_removed_links_sequential=MagicMock(),
    )

    LinkOpsMixin._perform_local_link_operations_parallel(
        fake,
        links_to_remove=[object(), object()],
        links_to_update=[],
        links_to_add=[],
        max_workers=4,
    )

    assert removal_threads == [threading.main_thread(), threading.main_thread()]
    fake._cleanup_removed_links_sequential.assert_called_once()


def test_bulk_link_operations_issues_one_batch_update():
    update = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )
    launcher = MagicMock()
    launcher.update_links.return_value = _update_response([netem_pb2.OK])
    fake = _FakeTopology({1: update}, launcher=launcher)

    stats = LinkOpsMixin.bulk_link_operations(fake, max_workers=8)

    launcher.update_links.assert_called_once_with([update])
    assert update.to_update is False
    assert stats["updated_count"] == 1
    assert stats["update_error_count"] == 0


def test_bulk_link_operations_falls_back_on_unimplemented(caplog):
    updates = [
        SimpleNamespace(
            to_add=False,
            to_update=True,
            to_remove=False,
            source=SimpleNamespace(name=f"Sat{i}"),
            target=SimpleNamespace(name=f"Sat{i + 1}"),
        )
        for i in range(2)
    ]
    launcher = MagicMock()
    launcher.update_links.side_effect = _UnimplementedRpcError()
    execute = MagicMock()
    fake = _FakeTopology(
        {i: link for i, link in enumerate(updates)},
        launcher=launcher,
        _execute_link_update=execute,
    )

    with caplog.at_level(logging.WARNING):
        LinkOpsMixin.bulk_link_operations(
            fake, to_add=False, to_del=False, links_to_update=updates
        )
        for link in updates:
            link.to_update = True
        LinkOpsMixin.bulk_link_operations(
            fake, to_add=False, to_del=False, links_to_update=updates
        )

    assert execute.call_count == 4
    assert launcher.update_links.call_count == 1
    warnings = [
        record
        for record in caplog.records
        if "falling back to per-link" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_execute_link_update_logs_per_link_at_debug_level(caplog):
    fake = SimpleNamespace(hil_manager=None, launcher=MagicMock())
    link = SimpleNamespace(
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
        delay=10,
    )

    with caplog.at_level(logging.DEBUG):
        LinkOpsMixin._execute_link_update(fake, link)

    matching = [
        record
        for record in caplog.records
        if "Updated link Sat0 -- Sat1" in record.getMessage()
    ]
    assert matching
    assert all(record.levelno == logging.DEBUG for record in matching)
    assert not any(record.levelno == logging.INFO for record in matching)


class _ExplodingLinks(dict):
    def values(self):
        raise AssertionError("link operations should not rescan self.links")


def test_bulk_link_operations_uses_precomputed_lists_without_scan():
    update = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )
    launcher = MagicMock()
    launcher.update_links.return_value = _update_response([netem_pb2.OK])
    fake = _FakeTopology(_ExplodingLinks(), launcher=launcher)

    stats = LinkOpsMixin.bulk_link_operations(
        fake,
        to_add=False,
        to_del=False,
        links_to_add=[],
        links_to_update=[update],
        links_to_remove=[],
        max_workers=1,
    )

    assert stats["updated_count"] == 1
    launcher.update_links.assert_called_once_with([update])


def test_bulk_link_operations_reports_category_times_independently():
    update = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )
    addition = SimpleNamespace(to_add=True, to_update=False, to_remove=False)
    launcher = MagicMock()
    launcher.update_links.return_value = _update_response([netem_pb2.OK])
    fake = _FakeTopology(
        _ExplodingLinks(),
        launcher=launcher,
        _execute_link_add=MagicMock(),
    )

    add_future = MagicMock()
    add_future.result.return_value = 0.1

    executor = MagicMock()
    executor.submit.return_value = add_future
    pool = MagicMock()
    pool.__enter__.return_value = executor
    pool.__exit__.return_value = False

    with patch(
        "satgonetem.services.mixins.link_ops.ThreadPoolExecutor",
        return_value=pool,
    ), patch(
        "satgonetem.services.mixins.link_ops.as_completed",
        return_value=[add_future],
    ), patch(
        "satgonetem.services.mixins.link_ops.time.perf_counter",
        side_effect=[10.0, 10.4, 20.0, 20.1],
    ):
        stats = LinkOpsMixin.bulk_link_operations(
            fake,
            to_del=False,
            links_to_add=[addition],
            links_to_update=[update],
            links_to_remove=[],
            max_workers=2,
        )

    assert stats["update_time_total"] == pytest.approx(0.4)
    assert stats["update_time_wall"] == pytest.approx(0.4)
    assert stats["update_time_per_link"] == pytest.approx(0.4)
    assert stats["add_time_total"] == pytest.approx(0.1)
    assert stats["add_time_wall"] == pytest.approx(0.1)
    assert stats["add_time_per_link"] == pytest.approx(0.1)
    assert stats["update_time_total"] != stats["add_time_total"]


def test_bulk_link_operations_logs_per_link_errors_and_continues(caplog):
    ok = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )
    failed = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat2"),
        target=SimpleNamespace(name="Sat3"),
    )
    launcher = MagicMock()
    launcher.update_links.return_value = _update_response(
        [netem_pb2.OK, netem_pb2.ERROR], error="tc exploded"
    )
    fake = _FakeTopology({0: ok, 1: failed}, launcher=launcher)

    with caplog.at_level(logging.ERROR):
        stats = LinkOpsMixin.bulk_link_operations(
            fake, to_add=False, to_del=False, links_to_update=[ok, failed]
        )

    assert stats["updated_count"] == 2
    assert stats["update_error_count"] == 1
    assert ok.to_update is False
    assert failed.to_update is False
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.ERROR
    ]
    assert any("Sat2" in message and "Sat3" in message for message in errors)
    assert any("tc exploded" in message for message in errors)


def test_bulk_link_operations_rejects_missing_results():
    updates = [
        SimpleNamespace(
            to_add=False,
            to_update=True,
            to_remove=False,
            source=SimpleNamespace(name=f"Sat{i}"),
            target=SimpleNamespace(name=f"Sat{i + 1}"),
        )
        for i in range(2)
    ]
    launcher = MagicMock()
    launcher.update_links.return_value = _update_response([netem_pb2.OK])
    fake = _FakeTopology({0: updates[0], 1: updates[1]}, launcher=launcher)

    with pytest.raises(RuntimeError, match="1 result"):
        LinkOpsMixin.bulk_link_operations(
            fake, to_add=False, to_del=False, links_to_update=updates
        )


def test_bulk_link_operations_excludes_hil_links_from_batch():
    hil_link = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Gnd0"),
    )
    simulated_link = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat1"),
        target=SimpleNamespace(name="Sat2"),
    )
    hil_manager = MagicMock()
    hil_manager.is_hil_link.side_effect = lambda link: link is hil_link
    launcher = MagicMock()
    launcher.update_links.return_value = _update_response([netem_pb2.OK])
    fake = _FakeTopology(
        {0: hil_link, 1: simulated_link},
        launcher=launcher,
        hil_manager=hil_manager,
    )

    LinkOpsMixin.bulk_link_operations(
        fake, to_add=False, to_del=False, links_to_update=[hil_link, simulated_link]
    )

    launcher.update_links.assert_called_once_with([simulated_link])
    hil_manager.update_link.assert_called_once_with(hil_link)
    assert hil_link.to_update is False
    assert simulated_link.to_update is False


def test_bulk_link_operations_propagates_non_unimplemented_grpc_errors():
    class _InternalRpcError(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.INTERNAL

    update = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )
    launcher = MagicMock()
    launcher.update_links.side_effect = _InternalRpcError()
    fake = _FakeTopology({0: update}, launcher=launcher)

    with pytest.raises(_InternalRpcError):
        LinkOpsMixin.bulk_link_operations(
            fake, to_add=False, to_del=False, links_to_update=[update]
        )


def test_local_link_operations_use_precomputed_lists_without_scan():
    update = SimpleNamespace(to_add=False, to_update=True, to_remove=False)
    fake = SimpleNamespace(
        links=_ExplodingLinks(),
        _perform_local_link_operations_parallel=MagicMock(),
    )

    LinkOpsMixin._perform_local_link_operations(
        fake,
        to_add=False,
        to_del=False,
        links_to_add=[],
        links_to_update=[update],
        links_to_remove=[],
        max_workers=1,
    )

    fake._perform_local_link_operations_parallel.assert_called_once_with(
        [], [update], [], 1
    )


def test_updates_and_additions_still_run():
    fake = SimpleNamespace(
        _remove_link_interfaces=MagicMock(),
        _update_link_flags=MagicMock(),
        _add_link_interfaces=MagicMock(),
        _cleanup_removed_links_sequential=MagicMock(),
    )
    update = object()
    addition = object()

    LinkOpsMixin._perform_local_link_operations_parallel(
        fake,
        links_to_remove=[],
        links_to_update=[update],
        links_to_add=[addition],
        max_workers=2,
    )

    fake._update_link_flags.assert_called_once_with(update)
    fake._add_link_interfaces.assert_called_once_with(addition)


def test_update_routing_after_link_changes_uses_route_apply_workers():
    routing_daemon = MagicMock()
    fake = SimpleNamespace(
        get_routing_initiated=lambda: True,
        routing_daemon=routing_daemon,
        routing="static",
        _daemon_registry={},
    )

    LinkOpsMixin._update_routing_after_link_changes(fake, [])

    routing_daemon.update.assert_called_once_with(
        [], max_workers=ROUTE_APPLY_WORKERS
    )


def test_update_routing_after_link_changes_honors_explicit_workers():
    routing_daemon = MagicMock()
    fake = SimpleNamespace(
        get_routing_initiated=lambda: True,
        routing_daemon=routing_daemon,
        routing="static",
        _daemon_registry={},
    )

    LinkOpsMixin._update_routing_after_link_changes(fake, [], max_workers=3)

    routing_daemon.update.assert_called_once_with([], max_workers=3)


def test_remove_link_interfaces_bumps_interface_version():
    source = SimpleNamespace(remove_interface_connected_to_node=MagicMock())
    target = SimpleNamespace(remove_interface_connected_to_node=MagicMock())
    link = SimpleNamespace(source=source, target=target)
    fake = SimpleNamespace(interface_version=7)

    LinkOpsMixin._remove_link_interfaces(fake, link)

    assert fake.interface_version == 8
