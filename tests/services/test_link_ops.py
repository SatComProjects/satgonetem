"""Tests for local link-operation ordering and thread safety."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

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
