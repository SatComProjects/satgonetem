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

        assert old_reader.header[0]["version"] == 2
        assert old_reader.header[0]["num_edges"] == 1
        assert int(old_reader.edges[0]["src_name"]) == 1
    finally:
        if old_reader is not None:
            old_reader.close()
        owner.close()
        owner.unlink()


def test_ensure_capacity_does_not_publish_old_version_with_empty_edges() -> None:
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=1, create=True)
    try:
        owner.write_edges([make_edge(src_name=1)])
        owner.ensure_capacity(3)

        assert int(owner.header[0]["version"]) == 0
        with pytest.raises(TimeoutError):
            GraphSharedMemory.read_snapshot(name, retries=0)

        owner.write_edges(
            [make_edge(src_name=1), make_edge(src_name=2), make_edge(src_name=3)]
        )
        snapshot = GraphSharedMemory.read_snapshot(name)
        assert snapshot.num_edges == 3
        assert snapshot.version > 0
    finally:
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
        assert snapshot.version == 2
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


def test_read_snapshot_retries_on_odd_in_progress_version(monkeypatch) -> None:
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=2, create=True)
    try:
        owner.write_edges([make_edge(src_name=1)])
        # Simulate a writer mid-update (odd seqlock version).
        owner.header[0]["version"] = 3

        sleeps: list[float] = []

        def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            if len(sleeps) == 1:
                owner.write_edges([make_edge(src_name=2)])

        monkeypatch.setattr(graph_shm_module.time, "sleep", fake_sleep)

        snapshot = GraphSharedMemory.read_snapshot(name, retries=2, retry_delay=0.01)

        assert sleeps == [0.01]
        assert snapshot.version % 2 == 0
        assert int(snapshot.edges[0]["src_name"]) == 2
    finally:
        owner.close()
        owner.unlink()


def test_read_snapshot_times_out_when_version_stays_odd(monkeypatch) -> None:
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=1, create=True)
    try:
        owner.write_edges([make_edge(src_name=1)])
        owner.header[0]["version"] = 3
        monkeypatch.setattr(graph_shm_module.time, "sleep", MagicMock())

        with pytest.raises(TimeoutError, match="version 3"):
            GraphSharedMemory.read_snapshot(name, retries=2, retry_delay=0)
    finally:
        owner.close()
        owner.unlink()


def test_write_edges_preserves_seqlock_parity_after_mid_write_exception() -> None:
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=2, create=True)
    try:
        owner.write_edges([make_edge(src_name=1)])
        assert int(owner.header[0]["version"]) % 2 == 0

        # Trigger an exception mid-write on the second edge.
        with pytest.raises(ValueError):
            owner.write_edges([make_edge(src_name=2), make_edge(src_ip="not-an-ip")])

        # Interrupted write must leave an odd version so readers retry/timeout.
        assert int(owner.header[0]["version"]) % 2 == 1
        with pytest.raises(TimeoutError):
            GraphSharedMemory.read_snapshot(name, retries=0)

        # A subsequent valid write must still publish an even version.
        owner.write_edges([make_edge(src_name=3)])
        assert int(owner.header[0]["version"]) % 2 == 0
        snapshot = GraphSharedMemory.read_snapshot(name, retries=0)
        assert snapshot.version % 2 == 0
        assert snapshot.num_edges == 1
        assert int(snapshot.edges[0]["src_name"]) == 3
    finally:
        owner.close()
        owner.unlink()


def test_write_edges_clears_trailing_edges_before_publishing_end_version() -> None:
    name = unique_name()
    owner = GraphSharedMemory(name, max_edges=2, create=True)
    try:
        owner.write_edges([make_edge(src_name=1), make_edge(src_name=2)])
        real_header = owner.header
        observed_trailing_weights: list[float] = []

        class _HeaderRowProxy:
            def __getitem__(self, key: str):
                return real_header[0][key]

            def __setitem__(self, key: str, value) -> None:
                if key == "version" and int(value) % 2 == 0:
                    observed_trailing_weights.append(float(owner.edges[1]["weight"]))
                real_header[0][key] = value

        owner.header = [_HeaderRowProxy()]  # type: ignore[assignment]
        owner.write_edges([make_edge(src_name=10)])
        owner.header = real_header

        assert observed_trailing_weights == [-1.0]
    finally:
        owner.close()
        owner.unlink()
