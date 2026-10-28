"""Tests for local link-operation ordering and thread safety."""

import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.services.mixins.link_ops import LinkOpsMixin
from satgonetem.utils.constants import ROUTE_APPLY_WORKERS


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


def test_bulk_link_operations_uses_requested_worker_count():
    update = SimpleNamespace(to_add=False, to_update=True, to_remove=False)
    fake = SimpleNamespace(
        links={1: update},
        get_status=lambda: True,
        _execute_link_update=MagicMock(),
    )

    future = MagicMock()
    executor = MagicMock()
    executor.submit.return_value = future
    pool = MagicMock()
    pool.__enter__.return_value = executor
    pool.__exit__.return_value = False

    with patch(
        "satgonetem.services.mixins.link_ops.ThreadPoolExecutor",
        return_value=pool,
    ) as executor_cls, patch(
        "satgonetem.services.mixins.link_ops.as_completed",
        return_value=[future],
    ):
        stats = LinkOpsMixin.bulk_link_operations(fake, max_workers=8)

    executor_cls.assert_called_once_with(max_workers=8)
    executor.submit.assert_called_once_with(fake._execute_link_update, update)
    assert stats["updated_count"] == 1


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
    update = SimpleNamespace(to_add=False, to_update=True, to_remove=False)
    fake = SimpleNamespace(
        links=_ExplodingLinks(),
        get_status=lambda: True,
        _execute_link_update=MagicMock(),
    )

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
    fake._execute_link_update.assert_called_once_with(update)


def test_bulk_link_operations_reports_category_times_independently():
    update = SimpleNamespace(to_add=False, to_update=True, to_remove=False)
    addition = SimpleNamespace(to_add=True, to_update=False, to_remove=False)
    fake = SimpleNamespace(
        links=_ExplodingLinks(),
        get_status=lambda: True,
        _execute_link_update=MagicMock(),
        _execute_link_add=MagicMock(),
    )

    update_future = MagicMock()
    update_future.result.return_value = 0.2
    add_future = MagicMock()
    add_future.result.return_value = 0.1

    executor = MagicMock()
    executor.submit.side_effect = [update_future, add_future]
    pool = MagicMock()
    pool.__enter__.return_value = executor
    pool.__exit__.return_value = False

    with patch(
        "satgonetem.services.mixins.link_ops.ThreadPoolExecutor",
        return_value=pool,
    ), patch(
        "satgonetem.services.mixins.link_ops.as_completed",
        return_value=[update_future, add_future],
    ):
        stats = LinkOpsMixin.bulk_link_operations(
            fake,
            to_del=False,
            links_to_add=[addition],
            links_to_update=[update],
            links_to_remove=[],
            max_workers=2,
        )

    assert stats["update_time_total"] == pytest.approx(0.2)
    assert stats["update_time_per_link"] == pytest.approx(0.2)
    assert stats["add_time_total"] == pytest.approx(0.1)
    assert stats["add_time_per_link"] == pytest.approx(0.1)
    assert stats["update_time_total"] != stats["add_time_total"]


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
