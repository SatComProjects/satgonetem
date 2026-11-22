"""Tests for the static routing RouteInstall batch installer."""

from unittest.mock import MagicMock

import grpc

from satgonetem.models.node import Node
from satgonetem.proto import netem_pb2
from satgonetem.routing.route_batch_installer import (
    RouteBatchInstaller,
    _BatchOutcome,
    _percentile,
)
from satgonetem.routing.static_route_ops import RouteOperation


class _UnimplementedRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNIMPLEMENTED


def _node(name, client=None, project_id="prj"):
    node = Node(name)
    node.grpc_client = client
    node.project_id = project_id
    return node


def _ops(dst="10.0.0.0/16", gw="192.0.2.1", dev="eth1"):
    return [RouteOperation("replace", dst, gw, dev)]


def _response(request, error_nodes=(), missing=()):
    response = netem_pb2.RouteInstallResponse()
    response.status.code = netem_pb2.StatusCode.OK
    for name in request.nodes:
        if name in missing:
            continue
        result = response.results[name]
        if name in error_nodes:
            result.status.code = netem_pb2.StatusCode.ERROR
            result.status.error = "boom"
            continue
        result.status.code = netem_pb2.StatusCode.OK
        result.applied = len(request.nodes[name].routes)
        result.durationMicros = 1000
    return response


class FakeClient:
    def __init__(self, error=None, response_factory=_response):
        self.requests = []
        self.error = error
        self.response_factory = response_factory

    def RouteInstall(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response_factory(request)


def _format(op):
    return f"{op.action} {op.destination}"


class TestBatchOutcome:
    def test_merge_unions_nodes_and_accumulates_totals(self):
        first = _BatchOutcome(
            ok_nodes={"a"}, error_nodes={"b"}, applied=2, durations=[10]
        )
        second = _BatchOutcome(
            ok_nodes={"c"}, error_nodes={"a"}, applied=3, durations=[20, 30]
        )

        first.merge(second)

        assert first.ok_nodes == {"a", "c"}
        assert first.error_nodes == {"a", "b"}
        assert first.applied == 5
        assert first.durations == [10, 20, 30]


class TestPercentile:
    def test_empty_values_are_zero(self):
        assert _percentile([], 95) == 0

    def test_nearest_rank_uses_sorted_values(self):
        assert _percentile([30, 10, 20], 50) == 20
        assert _percentile([10, 20, 30], 100) == 30


class TestBuildRouteBatch:
    def test_maps_delete_and_replace_actions(self):
        installer = RouteBatchInstaller(MagicMock())

        batch = installer.build_route_batch(
            [
                RouteOperation("del", "10.0.0.0/16", "192.0.2.1", "eth1"),
                RouteOperation("replace", "10.1.0.0/16", "", "eth2"),
            ]
        )

        specs = list(batch.routes)
        assert [spec.action for spec in specs] == [
            netem_pb2.ROUTE_DELETE,
            netem_pb2.ROUTE_REPLACE,
        ]
        assert [spec.destination for spec in specs] == [
            "10.0.0.0/16",
            "10.1.0.0/16",
        ]
        assert [spec.gateway for spec in specs] == ["192.0.2.1", ""]
        assert [spec.device for spec in specs] == ["eth1", "eth2"]


class TestPartitionRouteBatches:
    def test_groups_by_client_identity_and_project(self):
        client = object()
        installer = RouteBatchInstaller(MagicMock())

        groups, legacy = installer.partition_route_batches(
            [
                (_node("Sat0", client, "one"), _ops()),
                (_node("Sat1", client, "one"), _ops()),
                (_node("Sat2", object(), "two"), _ops()),
                (_node("Sat3"), _ops()),
            ]
        )

        assert [len(group[2]) for group in groups] == [2, 1]
        assert [node.name for node, _ops_ in legacy] == ["Sat3"]

    def test_node_without_client_is_legacy(self):
        installer = RouteBatchInstaller(MagicMock())

        groups, legacy = installer.partition_route_batches([(_node("Sat0"), _ops())])

        assert groups == []
        assert [node.name for node, _ops_ in legacy] == ["Sat0"]


class TestResolveRouteBatchNode:
    def test_resolves_name_from_topology_containers(self):
        node = _node("Sat0")
        topology = MagicMock()
        topology.ground_stations = {}
        topology.satellites = {1: node}
        installer = RouteBatchInstaller(topology)

        assert installer.resolve_route_batch_node("Sat0") is node

    def test_passes_object_keys_through(self):
        node = _node("Sat0")
        installer = RouteBatchInstaller(MagicMock())

        assert installer.resolve_route_batch_node(node) is node

    def test_unknown_name_returns_none(self):
        topology = MagicMock()
        topology.ground_stations = {}
        topology.satellites = {}
        installer = RouteBatchInstaller(topology)

        assert installer.resolve_route_batch_node("missing") is None


class TestNextRouteChunk:
    def test_limits_chunk_by_node_count(self):
        installer = RouteBatchInstaller(MagicMock())
        jobs = [(_node(f"Sat{i}"), _ops()) for i in range(5)]

        chunk, next_index = installer.next_route_chunk(jobs, 0, 2, 10**9)

        assert [node.name for node, _batch in chunk] == ["Sat0", "Sat1"]
        assert next_index == 2

    def test_limits_chunk_by_serialized_size(self):
        installer = RouteBatchInstaller(MagicMock())
        jobs = [(_node(f"Sat{i}"), _ops()) for i in range(3)]

        chunk, next_index = installer.next_route_chunk(jobs, 0, 100, 1)

        assert len(chunk) == 1
        assert next_index == 1


class TestInstallRouteBatches:
    def test_sends_one_request_and_returns_ok_nodes(self):
        client = FakeClient()
        sat0 = _node("Sat0", client)
        sat1 = _node("Sat1", client)
        installer = RouteBatchInstaller(MagicMock())

        outcome = installer.install_route_batches(
            {sat0: _ops(), sat1: _ops()},
            max_workers=1,
            exec_batch=MagicMock(),
            format_operation=_format,
        )

        assert len(client.requests) == 1
        assert outcome.ok_nodes == {"Sat0", "Sat1"}
        assert outcome.applied == 2

    def test_unimplemented_is_sticky_and_falls_back_once(self):
        client = FakeClient(error=_UnimplementedRpcError())
        node = _node("Sat0", client)
        exec_batch = MagicMock()
        installer = RouteBatchInstaller(MagicMock())

        first = installer.install_route_batches(
            {node: _ops()},
            max_workers=1,
            exec_batch=exec_batch,
            format_operation=_format,
        )
        second = installer.install_route_batches(
            {node: _ops()},
            max_workers=1,
            exec_batch=exec_batch,
            format_operation=_format,
        )

        assert len(client.requests) == 1
        assert installer.route_install_supported is False
        assert exec_batch.call_count == 2
        assert first.ok_nodes == {"Sat0"}
        assert second.ok_nodes == {"Sat0"}

    def test_unknown_node_key_is_an_error_without_a_request(self):
        client = FakeClient()
        installer = RouteBatchInstaller(MagicMock())

        outcome = installer.install_route_batches(
            {"missing": _ops()},
            max_workers=1,
            exec_batch=MagicMock(),
            format_operation=_format,
        )

        assert outcome.error_nodes == {"missing"}
        assert client.requests == []


class TestApplyLegacyBatches:
    def test_formats_and_executes_each_node(self):
        sat0 = _node("Sat0")
        sat1 = _node("Sat1")
        exec_batch = MagicMock()
        installer = RouteBatchInstaller(MagicMock())

        outcome = installer.apply_legacy_batches(
            [(sat0, _ops()), (sat1, _ops())],
            max_workers=2,
            exec_batch=exec_batch,
            format_operation=_format,
        )

        assert exec_batch.call_count == 2
        assert outcome.ok_nodes == {"Sat0", "Sat1"}
        assert outcome.applied == 2

    def test_exec_failure_marks_only_that_node(self):
        sat0 = _node("Sat0")
        sat1 = _node("Sat1")
        exec_batch = MagicMock(side_effect=[None, RuntimeError("boom")])
        installer = RouteBatchInstaller(MagicMock())

        outcome = installer.apply_legacy_batches(
            [(sat0, _ops()), (sat1, _ops())],
            max_workers=1,
            exec_batch=exec_batch,
            format_operation=_format,
        )

        assert outcome.ok_nodes == {"Sat0"}
        assert outcome.error_nodes == {"Sat1"}
