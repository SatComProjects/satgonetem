"""GoNetEm gRPC client and link-request helpers."""

from __future__ import annotations

import logging
from typing import Any

import satgonetem.proto.netem_pb2_grpc as netem_grpc
from satgonetem.launchers.gonetem_project import _tbf_qos
from satgonetem.proto import netem_pb2


class GoNetEmClientMixin:
    """Connection and protobuf request construction for GoNetEm."""

    _client: Any
    _connection: Any
    _channel: Any
    _server_address: str
    _client_factory: Any

    def _get_client(self) -> netem_grpc.NetemStub:
        if self._client is None:
            self._connection = self._client_factory.connect(self._server_address)
            self._channel = self._connection.channel
            self._client = self._connection.client
        return self._client

    def _link_request(self, link) -> netem_pb2.LinkRequest:  # type: ignore[name-defined]
        delay_ms = max(int(link.delay), 1)
        loss = float(getattr(link, "loss", 0) or 0)
        jitter = int(getattr(link, "jitter", 0) or 0)
        p1_kbps, p2_kbps = self._link_capacities_for(link)
        burst1, buffer1 = _tbf_qos(p1_kbps, delay_ms)
        burst2, buffer2 = _tbf_qos(p2_kbps, delay_ms)
        qos1 = netem_pb2.LinkConfig.QoSConfig(
            delay=delay_ms,
            rate=p1_kbps,
            loss=loss,
            jitter=jitter,
            buffer=buffer1,
            burst=burst1,
            limit=self.netem_limit,
            txqueuelen=self.txqueuelen,
        )
        qos2 = netem_pb2.LinkConfig.QoSConfig(
            delay=delay_ms,
            rate=p2_kbps,
            loss=loss,
            jitter=jitter,
            buffer=buffer2,
            burst=burst2,
            limit=self.netem_limit,
            txqueuelen=self.txqueuelen,
        )
        link_cfg = netem_pb2.LinkConfig(
            peer1=f"{link.source.name}.{link.target.id}",
            peer2=f"{link.target.name}.{link.source.id}",
            peer1qos=qos1,
            peer2qos=qos2,
        )
        return netem_pb2.LinkRequest(prjId=self._request.id, link=link_cfg)

    def _link_capacities_for(self, link) -> tuple[int, int]:
        use_budget = bool(getattr(link, "use_budget", False))
        p1_raw = getattr(link, "peer1_capacity", None)
        p2_raw = getattr(link, "peer2_capacity", None)
        p1 = int(p1_raw or 0)
        p2 = int(p2_raw or 0)
        fb = int(getattr(link, "capacity", 0) or 0)
        link_type = getattr(link, "type", "")
        if link_type == "GroundStationLink":
            default = self.gnd_capacity_kbps
        elif link_type == "GroundObjectLink":
            default = self.ground_object_capacity_kbps
        else:
            default = self.isl_capacity_kbps
        rate1 = p1 if (use_budget and p1_raw is not None) else (p1 or fb or default)
        rate2 = p2 if (use_budget and p2_raw is not None) else (p2 or fb or default)
        return rate1, rate2

    # Lifecycle

    def _close_transport(self) -> None:
        """Close the gRPC channel and clear client state."""
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception as exc:
                logging.warning(
                    "GoNetEmLauncher: failed to close gRPC connection: %s", exc
                )
        elif self._channel is not None:
            try:
                self._channel.close()
            except Exception as exc:
                logging.warning(
                    "GoNetEmLauncher: failed to close gRPC channel: %s", exc
                )
        self._connection = None
        self._channel = None
        self._client = None
