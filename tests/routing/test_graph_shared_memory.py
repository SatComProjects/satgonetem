"""Regression tests for re-openable graph shared-memory reads (audit M17).

A consumer that maps the segment once keeps the mapping it opened.  When the
owner grows the graph it unlinks the old segment and creates a new one under
the same name; an existing mapping still points at the old object and can
never observe the new version.  Consumers must re-open by name for each read.
``GraphSharedMemory.read_snapshot`` provides that read path and retries the
short window in which the owner has removed the old name but has not finished
publishing the replacement.
"""

from __future__ import annotations

import os
from multiprocessing import shared_memory
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from satgonetem.routing import graph_shared_memory as graph_shm_module
from satgonetem.routing.graph_shared_memory import GraphSharedMemory

pytestmark = pytest.mark.unit


def unique_name() -> str:
    return f"satgonetem-test-{os.getpid()}-{uuid4().hex}"


def make_edge(**overrides):
    edge = {
        "src_ip": "10.0.0.4",
        "dst_ip": "10.0.0.5",
        "src_name": 7,
        "dst_name": 9,
        "src_mac": 11,
        "dst_mac": 13,
        "src_port": 2,
        "dst_port": 3,
        "flag_gateway": 1,
        "weight": 1.5,
    }
    edge.update(overrides)
    return edge


def test_read_snapshot_sees_recreated_segment_and_ignores_stale_capacity() -> None:
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=1, create=True)
    old_reader = None
    try:
        owner.write_edges([make_edge(src_name=1)])
        # A consumer that maps once keeps this view even after the owner
        # recreates the segment (documented limitation).
        old_reader = GraphSharedMemory(name, max_edges=1, create=False)

        owner.ensure_capacity(3)
        owner.write_edges(
            [
                make_edge(src_name=1),
                make_edge(src_name=2),
                make_edge(src_name=3),
            ]
        )

        snapshot = GraphSharedMemory.read_snapshot(name)

        assert snapshot.num_edges == 3
        assert snapshot.max_edges >= 3
        assert [int(edge["src_name"]) for edge in snapshot.edges] == [1, 2, 3]

        assert old_reader.header[0]["version"] == 1
        assert old_reader.header[0]["num_edges"] == 1
        assert int(old_reader.edges[0]["src_name"]) == 1
    finally:
        if old_reader is not None:
            old_reader.close()
        owner.close()
        owner.unlink()


def test_read_snapshot_retries_while_the_owner_is_recreating(monkeypatch) -> None:
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=2, create=True)
    try:
        owner.write_edges([make_edge(src_name=5)])

        real_shared_memory = shared_memory.SharedMemory
        attempts = 0

        def flaky_shared_memory(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise FileNotFoundError(name)
            return real_shared_memory(*args, **kwargs)

        monkeypatch.setattr(
            graph_shm_module.shared_memory,
            "SharedMemory",
            flaky_shared_memory,
        )

        snapshot = GraphSharedMemory.read_snapshot(name, retries=2, retry_delay=0)

        assert attempts == 3
        assert snapshot.num_edges == 1
        assert int(snapshot.edges[0]["src_name"]) == 5
    finally:
        owner.close()
        owner.unlink()


def test_read_snapshot_retries_while_version_is_zero(monkeypatch) -> None:
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=2, create=True)
    try:
        sleeps = []

        def fake_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == 1:
                owner.write_edges([make_edge(src_name=8)])

        monkeypatch.setattr(graph_shm_module.time, "sleep", fake_sleep)

        snapshot = GraphSharedMemory.read_snapshot(name, retries=2, retry_delay=0.01)

        assert sleeps == [0.01]
        assert snapshot.version == 1
        assert int(snapshot.edges[0]["src_name"]) == 8
    finally:
        owner.close()
        owner.unlink()


def test_read_snapshot_gives_up_after_bounded_version_zero_retries(monkeypatch):
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=1, create=True)
    try:
        monkeypatch.setattr(graph_shm_module.time, "sleep", MagicMock())

        with pytest.raises(TimeoutError, match="version 0"):
            GraphSharedMemory.read_snapshot(name, retries=2, retry_delay=0)
    finally:
        owner.close()
        owner.unlink()
