"""Tests for local link-operation ordering and thread safety."""

import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import grpc
import pytest

from satgonetem.models.interface import Interface
from satgonetem.proto import netem_pb2
from satgonetem.services.mixins.link_ops import LinkOpsMixin
from satgonetem.utils.constants import ROUTE_APPLY_WORKERS
from tests.support.factories import make_link, make_satellite


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

    @staticmethod
    def _build_link_key(source, target):
        return frozenset((source.name, target.name))


def _link_pending_removal():
    """Build a real /31 link with both node interfaces attached, marked stale."""
    source = make_satellite("Sat0")
    target = make_satellite("Sat1")
    link = make_link(source, target)
    source_interface = Interface(f"{source.name}.{target.id}")
    target_interface = Interface(f"{target.name}.{source.id}")
    source_interface.peer = target_interface
    target_interface.peer = source_interface
    source.interfaces.append(source_interface)
    target.interfaces.append(target_interface)
    link.peer_interfaces.extend([source_interface, target_interface])
    link.to_remove = True
    return link


def _topology_with_link(link, launcher, address_plan):
    return _FakeTopology(
        {frozenset((link.source.name, link.target.name)): link},
        launcher=launcher,
        address_plan=address_plan,
        interfaces=list(link.peer_interfaces),
        interface_version=0,
    )


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

    with (
        patch(
            "satgonetem.services.mixins.link_ops.ThreadPoolExecutor",
            return_value=pool,
        ),
        patch(
            "satgonetem.services.mixins.link_ops.as_completed",
            return_value=[add_future],
        ),
        patch(
            "satgonetem.services.mixins.link_ops.time.perf_counter",
            side_effect=[10.0, 10.4, 20.0, 20.1],
        ),
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
    # A per-link application failure keeps the retry flag set.
    assert failed.to_update is True
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

    routing_daemon.update.assert_called_once_with([], max_workers=ROUTE_APPLY_WORKERS)


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


def _run_delete_operations(link, launcher, address_plan):
    fake = _topology_with_link(link, launcher, address_plan)
    stats = LinkOpsMixin.bulk_link_operations(
        fake,
        to_add=False,
        to_update=False,
        links_to_add=[],
        links_to_update=[],
        links_to_remove=[link],
    )
    LinkOpsMixin._perform_local_link_operations(
        fake,
        to_add=False,
        to_update=False,
        links_to_add=[],
        links_to_update=[],
        links_to_remove=[link],
    )
    return fake, stats


def test_failed_backend_delete_retains_link_and_address_slot(caplog):
    link = _link_pending_removal()
    key = frozenset((link.source.name, link.target.name))
    launcher = MagicMock()
    launcher.delete_link.side_effect = RuntimeError("LinkDel failed")
    address_plan = MagicMock()

    with caplog.at_level(logging.ERROR):
        fake, stats = _run_delete_operations(link, launcher, address_plan)

    # Spec 0006: deleted_count reports attempts, not successes.
    assert stats["deleted_count"] == 1
    # The emulator still has the veth pair, so the manager keeps the link and
    # its /31 for a retry instead of forgetting state it cannot recreate.
    assert fake.links == {key: link}
    assert link.to_remove is True
    assert link.to_delete is True
    address_plan.release_link.assert_not_called()
    assert fake.interfaces == link.peer_interfaces
    assert link.peer_interfaces[0] in link.source.interfaces
    assert link.peer_interfaces[1] in link.target.interfaces
    assert "Unable to delete link Sat0--Sat1" in caplog.text


def test_delete_retry_cleans_up_once_backend_recovers():
    link = _link_pending_removal()
    key = frozenset((link.source.name, link.target.name))
    launcher = MagicMock()
    launcher.delete_link.side_effect = [RuntimeError("LinkDel failed"), None]
    address_plan = MagicMock()

    fake, _ = _run_delete_operations(link, launcher, address_plan)
    assert fake.links == {key: link}
    address_plan.release_link.assert_not_called()

    fake, _ = _run_delete_operations(link, launcher, address_plan)

    assert launcher.delete_link.call_count == 2
    assert fake.links == {}
    address_plan.release_link.assert_called_once_with(key)


def test_successful_backend_delete_releases_link_and_address_slot():
    link = _link_pending_removal()
    key = frozenset((link.source.name, link.target.name))
    source_interfaces = list(link.source.interfaces)
    target_interfaces = list(link.target.interfaces)
    launcher = MagicMock()
    address_plan = MagicMock()

    fake, _ = _run_delete_operations(link, launcher, address_plan)

    launcher.delete_link.assert_called_once_with(link)
    assert fake.links == {}
    assert link.to_delete is False
    address_plan.release_link.assert_called_once_with(key)
    assert fake.interfaces == []
    assert link.source.interfaces == [
        interface
        for interface in source_interfaces
        if interface not in link.peer_interfaces
    ]
    assert link.target.interfaces == [
        interface
        for interface in target_interfaces
        if interface not in link.peer_interfaces
    ]


def _run_add_operations(link, launcher, **kwargs):
    fake = _FakeTopology(
        {frozenset((link.source.name, link.target.name)): link},
        launcher=launcher,
        **kwargs,
    )
    stats = LinkOpsMixin.bulk_link_operations(
        fake,
        to_del=False,
        to_update=False,
        links_to_add=[link],
        links_to_update=[],
        links_to_remove=[],
    )
    LinkOpsMixin._perform_local_link_operations(
        fake,
        to_del=False,
        to_update=False,
        links_to_add=[link],
        links_to_update=[],
        links_to_remove=[],
    )
    return fake, stats


def test_failed_backend_add_keeps_link_retryable(caplog):
    link = make_link(make_satellite("Sat2"), make_satellite("Sat3"))
    link.to_add = True
    key = frozenset((link.source.name, link.target.name))
    launcher = MagicMock()
    launcher.add_link.side_effect = RuntimeError("LinkAdd failed")
    build_interfaces = MagicMock()

    with caplog.at_level(logging.ERROR):
        fake, stats = _run_add_operations(
            link, launcher, _build_interfaces_from_link=build_interfaces
        )

    # Spec 0006: added_count reports attempts, not successes.
    assert stats["added_count"] == 1
    assert fake.links == {key: link}
    assert link.to_add is True
    assert link.peer_interfaces == []
    build_interfaces.assert_not_called()
    assert "Unable to add link Sat2--Sat3" in caplog.text


def test_add_retry_builds_interfaces_once_backend_recovers():
    link = make_link(make_satellite("Sat4"), make_satellite("Sat5"))
    link.to_add = True
    launcher = MagicMock()
    launcher.add_link.side_effect = [RuntimeError("LinkAdd failed"), None]
    build_interfaces = MagicMock()

    fake, _ = _run_add_operations(
        link, launcher, _build_interfaces_from_link=build_interfaces
    )
    assert fake.links
    assert link.to_add is True
    build_interfaces.assert_not_called()

    fake, _ = _run_add_operations(
        link, launcher, _build_interfaces_from_link=build_interfaces
    )

    assert launcher.add_link.call_count == 2
    assert link.to_add is False
    build_interfaces.assert_called_once_with(link, set_ip=True, sync_to_node=True)


def test_local_build_failure_retry_does_not_resend_backend_add(caplog):
    """The backend add must run once even while the local build is retried."""
    link = make_link(make_satellite("Sat10"), make_satellite("Sat11"))
    link.to_add = True
    key = frozenset((link.source.name, link.target.name))
    launcher = MagicMock()
    build_interfaces = MagicMock(
        side_effect=[RuntimeError("address push failed"), None]
    )
    fake = _FakeTopology(
        {key: link},
        launcher=launcher,
        _build_interfaces_from_link=build_interfaces,
    )

    def run_round():
        LinkOpsMixin.bulk_link_operations(
            fake,
            to_del=False,
            to_update=False,
            links_to_add=[link],
            links_to_update=[],
            links_to_remove=[],
        )
        LinkOpsMixin._perform_local_link_operations(
            fake,
            to_del=False,
            to_update=False,
            links_to_add=[link],
            links_to_update=[],
            links_to_remove=[],
        )

    with caplog.at_level(logging.ERROR):
        run_round()

    assert launcher.add_link.call_count == 1
    assert link.to_add is True
    assert "address push failed" in caplog.text

    run_round()

    assert launcher.add_link.call_count == 1
    assert build_interfaces.call_count == 2
    assert link.to_add is False


def test_successful_backend_add_builds_interfaces_and_clears_flag():
    link = make_link(make_satellite("Sat6"), make_satellite("Sat7"))
    link.to_add = True
    launcher = MagicMock()
    build_interfaces = MagicMock()

    _run_add_operations(link, launcher, _build_interfaces_from_link=build_interfaces)

    launcher.add_link.assert_called_once_with(link)
    build_interfaces.assert_called_once_with(link, set_ip=True, sync_to_node=True)
    assert link.to_add is False


def test_failed_link_update_is_retried_and_cleared_after_success():
    link = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )
    launcher = MagicMock()
    launcher.update_links.return_value = _update_response(
        [netem_pb2.ERROR], error="tc exploded"
    )
    fake = _FakeTopology({0: link}, launcher=launcher)

    LinkOpsMixin.bulk_link_operations(
        fake, to_add=False, to_del=False, links_to_update=[link]
    )
    assert link.to_update is True

    # The local phase must not clear the flag either.
    LinkOpsMixin._perform_local_link_operations(
        fake,
        to_add=False,
        to_del=False,
        links_to_add=[],
        links_to_update=[link],
        links_to_remove=[],
    )
    assert link.to_update is True

    link.to_update = True
    launcher.update_links.return_value = _update_response([netem_pb2.OK])
    LinkOpsMixin.bulk_link_operations(
        fake, to_add=False, to_del=False, links_to_update=[link]
    )
    LinkOpsMixin._perform_local_link_operations(
        fake,
        to_add=False,
        to_del=False,
        links_to_add=[],
        links_to_update=[link],
        links_to_remove=[],
    )
    assert link.to_update is False


def test_failed_per_link_update_fallback_keeps_retry_flag():
    link = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
        delay=10,
    )
    launcher = SimpleNamespace(
        update_link=MagicMock(side_effect=RuntimeError("LinkUpdate failed"))
    )
    fake = _FakeTopology({0: link}, launcher=launcher)

    LinkOpsMixin.bulk_link_operations(
        fake, to_add=False, to_del=False, links_to_update=[link]
    )

    launcher.update_link.assert_called_once_with(link)
    assert link.to_update is True


def test_failed_hil_update_keeps_retry_flag():
    link = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Gnd0"),
    )
    hil_manager = MagicMock()
    hil_manager.is_hil_link.return_value = True
    hil_manager.update_link.return_value = False
    launcher = MagicMock()
    fake = _FakeTopology({0: link}, launcher=launcher, hil_manager=hil_manager)

    LinkOpsMixin.bulk_link_operations(
        fake, to_add=False, to_del=False, links_to_update=[link]
    )

    hil_manager.update_link.assert_called_once_with(link)
    launcher.update_links.assert_not_called()
    assert link.to_update is True


def test_hil_update_success_clears_retry_flag():
    link = SimpleNamespace(
        to_add=False,
        to_update=True,
        to_remove=False,
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Gnd0"),
    )
    hil_manager = MagicMock()
    hil_manager.is_hil_link.return_value = True
    hil_manager.update_link.return_value = True
    fake = _FakeTopology({0: link}, launcher=MagicMock(), hil_manager=hil_manager)

    LinkOpsMixin.bulk_link_operations(
        fake, to_add=False, to_del=False, links_to_update=[link]
    )

    assert link.to_update is False


def test_hil_add_failure_keeps_link_pending_and_skips_local_interfaces():
    link = make_link(make_satellite("Sat8"), make_satellite("Sat9"))
    link.to_add = True
    key = frozenset((link.source.name, link.target.name))
    hil_manager = MagicMock()
    hil_manager.is_hil_link.return_value = True
    hil_manager.setup_link.return_value = False
    build_interfaces = MagicMock()
    fake = _FakeTopology(
        {key: link},
        launcher=MagicMock(),
        hil_manager=hil_manager,
        _build_interfaces_from_link=build_interfaces,
    )

    LinkOpsMixin.bulk_link_operations(
        fake, to_del=False, to_update=False, links_to_add=[link]
    )
    LinkOpsMixin._perform_local_link_operations(
        fake, to_del=False, to_update=False, links_to_add=[link]
    )

    assert link.to_add is True
    build_interfaces.assert_not_called()
    assert key in fake._link_add_failures()

    hil_manager.setup_link.return_value = True
    LinkOpsMixin.bulk_link_operations(
        fake, to_del=False, to_update=False, links_to_add=[link]
    )
    LinkOpsMixin._perform_local_link_operations(
        fake, to_del=False, to_update=False, links_to_add=[link]
    )

    hil_manager.setup_link.assert_called_with(link)
    build_interfaces.assert_called_once_with(link, set_ip=True, sync_to_node=True)
    assert link.to_add is False


def test_hil_teardown_failure_keeps_link_and_local_state():
    link = _link_pending_removal()
    key = frozenset((link.source.name, link.target.name))
    hil_manager = MagicMock()
    hil_manager.is_hil_link.return_value = True
    hil_manager.teardown_link.return_value = False
    fake = _FakeTopology(
        {key: link},
        launcher=MagicMock(),
        hil_manager=hil_manager,
        address_plan=MagicMock(),
        interfaces=list(link.peer_interfaces),
        interface_version=0,
    )

    LinkOpsMixin.bulk_link_operations(
        fake, to_add=False, to_update=False, links_to_remove=[link]
    )
    LinkOpsMixin._perform_local_link_operations(
        fake, to_add=False, to_update=False, links_to_remove=[link]
    )

    hil_manager.teardown_link.assert_called_once_with(link)
    assert link.to_delete is True
    assert fake.links == {key: link}
    fake.address_plan.release_link.assert_not_called()
    assert fake.interfaces == link.peer_interfaces
