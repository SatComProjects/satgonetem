"""Tests for the pure static-route helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.models.interface import Interface
from satgonetem.routing.static_daemon import StaticRoutingDaemon
from satgonetem.routing.static_route_ops import route_cache_signature


def _outgoing_interface(src_name, peer_id, peer_ip):
    """Build an interface whose peer has the given IPv4 address."""
    iface = Interface(name=f"{src_name}.{peer_id}")
    peer = Interface(name=f"peer.{peer_id}")
    peer.ipv4 = peer_ip
    iface.peer = peer
    return iface


class TestRouteCacheSignature:
    """Direct contract tests for the pure route-cache signature."""

    def test_records_destination_gateway_and_next_hop_in_order(self):
        dst_a = SimpleNamespace(id=2)
        dst_b = SimpleNamespace(id=4)
        iface = _outgoing_interface("Gnd0", 3, "10.9.0.3")

        signature = route_cache_signature(
            src_id=1,
            src_ifaces={3: iface},
            dst_array=[dst_a, dst_b],
            next_hops={2: {1: 3}, 4: {1: None}},
            loopback_ips={2: "10.0.0.2", 4: "10.0.0.4"},
        )

        assert signature == [
            (2, "10.0.0.2", "10.9.0.3", 3),
            (4, "10.0.0.4", "", None),
        ]

    def test_unreachable_destination_has_no_gateway_or_next_hop(self):
        dst = SimpleNamespace(id=2)

        signature = route_cache_signature(
            src_id=1,
            src_ifaces=None,
            dst_array=[dst],
            next_hops={2: {}},
            loopback_ips={2: "10.0.0.2"},
        )

        assert signature == [(2, "10.0.0.2", "", None)]

    def test_missing_interface_raises_value_error_with_source_and_destination(self):
        dst = SimpleNamespace(id=2)

        with pytest.raises(ValueError, match="Source 1.*next hop 3.*destination 2"):
            route_cache_signature(
                src_id=1,
                src_ifaces={},
                dst_array=[dst],
                next_hops={2: {1: 3}},
                loopback_ips={2: "10.0.0.2"},
            )

    def test_interface_without_peer_raises_value_error(self):
        dst = SimpleNamespace(id=2)
        iface = Interface(name="Gnd0.3")

        with pytest.raises(
            ValueError, match="Source 1.*'Gnd0.3'.*next hop 3.*destination 2.*no peer"
        ):
            route_cache_signature(
                src_id=1,
                src_ifaces={3: iface},
                dst_array=[dst],
                next_hops={2: {1: 3}},
                loopback_ips={2: "10.0.0.2"},
            )

    def test_daemon_delegates_to_pure_helper(self):
        daemon = StaticRoutingDaemon(MagicMock())
        dst = SimpleNamespace(id=2)

        with patch(
            "satgonetem.routing.static_daemon.route_cache_signature",
            return_value=[("sentinel",)],
        ) as helper:
            result = daemon._route_cache_signature(
                src_id=1,
                src_ifaces={},
                dst_array=[dst],
                next_hops={2: {}},
                loopback_ips={2: "10.0.0.2"},
            )

        helper.assert_called_once_with(1, {}, [dst], {2: {}}, {2: "10.0.0.2"})
        assert result == [("sentinel",)]
