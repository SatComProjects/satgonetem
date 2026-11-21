"""Characterization tests for the GraphSharedMemory shared-memory layout.

Each test uses a unique segment name and the fixture closes and unlinks the
segment so the test suite leaves no POSIX shared-memory objects behind.
"""

from __future__ import annotations

import os
from multiprocessing import shared_memory
from uuid import uuid4

import pytest

from satgonetem.utils.graph_shared_memory import (
    EDGE_DTYPE,
    HEADER_DTYPE,
    GraphSharedMemory,
    ip_to_int,
)

pytestmark = pytest.mark.unit


MAX_EDGES = 4


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


@pytest.fixture
def graph_shm():
    shm = GraphSharedMemory(unique_name(), max_edges=MAX_EDGES, create=True)
    try:
        yield shm
    finally:
        shm.close()
        shm.unlink()


def test_create_initializes_header_and_record_layout(graph_shm):
    header = graph_shm.header[0]

    assert header["version"] == 0
    assert header["num_edges"] == 0
    assert header["max_edges"] == MAX_EDGES
    assert header["schema_version"] == GraphSharedMemory.SCHEMA_VERSION == 1

    assert graph_shm.header.dtype == HEADER_DTYPE
    assert graph_shm.edges.dtype == EDGE_DTYPE
    assert graph_shm.edges.shape == (MAX_EDGES,)
    assert graph_shm.header_size == HEADER_DTYPE.itemsize


def test_write_edges_persists_converted_edge_and_metadata(graph_shm):
    graph_shm.write_edges([make_edge()])

    header = graph_shm.header[0]
    assert header["num_edges"] == 1
    assert header["version"] == 1

    edge = graph_shm.edges[0]
    assert edge["src_ip"] == ip_to_int("10.0.0.4") == 167772164
    assert edge["dst_ip"] == ip_to_int("10.0.0.5") == 167772165
    assert edge["src_name"] == 7
    assert edge["dst_name"] == 9
    assert edge["src_mac"] == 11
    assert edge["dst_mac"] == 13
    assert edge["src_port"] == 2
    assert edge["dst_port"] == 3
    assert edge["flag_gateway"] == 1
    assert edge["weight"] == pytest.approx(1.5)


def test_write_edges_increments_version_for_each_call(graph_shm):
    graph_shm.write_edges([make_edge()])
    graph_shm.write_edges([make_edge(src_name=1), make_edge(src_name=2)])

    header = graph_shm.header[0]
    assert header["num_edges"] == 2
    assert header["version"] == 2
    assert graph_shm.edges[0]["src_name"] == 1
    assert graph_shm.edges[1]["src_name"] == 2


def test_write_edges_clears_records_left_by_a_previous_write(graph_shm):
    graph_shm.write_edges([make_edge(src_name=1), make_edge(src_name=2)])

    graph_shm.write_edges([make_edge(src_name=3)])

    header = graph_shm.header[0]
    assert header["num_edges"] == 1
    assert header["version"] == 2

    stale = graph_shm.edges[1]
    assert stale["src_ip"] == 0
    assert stale["dst_ip"] == 0
    assert stale["src_name"] == 0
    assert stale["dst_name"] == 0
    assert stale["src_mac"] == 0
    assert stale["dst_mac"] == 0
    assert stale["src_port"] == 0
    assert stale["dst_port"] == 0
    assert stale["flag_gateway"] == 0
    assert stale["weight"] == pytest.approx(-1.0)


def test_clear_zeroes_records_without_resetting_header(graph_shm):
    graph_shm.write_edges([make_edge()])

    graph_shm.clear()

    edge = graph_shm.edges[0]
    assert edge["src_ip"] == 0
    assert edge["dst_ip"] == 0
    assert edge["src_name"] == 0
    assert edge["dst_name"] == 0
    assert edge["src_mac"] == 0
    assert edge["dst_mac"] == 0
    assert edge["src_port"] == 0
    assert edge["dst_port"] == 0
    assert edge["flag_gateway"] == 0
    assert edge["weight"] == pytest.approx(-1.0)

    header = graph_shm.header[0]
    assert header["num_edges"] == 1
    assert header["version"] == 1


def test_write_edges_rejects_more_edges_than_capacity(graph_shm):
    with pytest.raises(ValueError, match="Too many edges 5"):
        graph_shm.write_edges([make_edge() for _ in range(MAX_EDGES + 1)])

    header = graph_shm.header[0]
    assert header["num_edges"] == 0
    assert header["version"] == 0
    assert graph_shm.edges[0]["weight"] == pytest.approx(-1.0)


def test_close_and_unlink_release_the_segment():
    name = unique_name()
    shm = GraphSharedMemory(name, max_edges=1, create=True)

    shm.close()
    shm.unlink()

    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name)
