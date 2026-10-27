"""Tests for local link-operation ordering and thread safety."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from satgonetem.services.mixins.link_ops import LinkOpsMixin


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
