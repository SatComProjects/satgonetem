"""TrafficTestingMixin for TopologyManager."""

from __future__ import annotations

from typing import List, Optional, Union
from satgonetem.traffic import (
    Hping3Config,
    Hping3Flow,
    Hping3Results,
    Hping3Status,
    Iperf3Config,
    Iperf3Flow,
    Iperf3Results,
    PingConfig,
    PingFlow,
    PingResults,
)
from satgonetem.traffic.ping_utils import PingStatus
from satgonetem.traffic.iperf3_utils import FlowStatus

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.services.topology_satcom import TopologyManager
    from satgonetem.models.node import Node


class TrafficTestingMixin:
    """TrafficTesting functionality."""

    def run_hping3(
        self,
        source: Union[Node, List[Node]],
        destination: Union[Node, List[Node]],
        config: Optional[Hping3Config] = None,
    ) -> List[Hping3Flow]:
        """Launch hping3 flows for all (source, destination) combinations.

        Accepts a single Node or a list of Nodes for each of source and
        destination. Creates one Hping3Flow per (source, destination) pair,
        starts each in a background thread, and returns the full list
        immediately.

        All nodes must already be running containers (i.e., the topology
        must have been started via start_gonetem before calling this method).

        Args:
            source: Node or list of Nodes that will run hping3. Each must be
                present in this topology.
            destination: Node or list of Nodes that are the hping3 targets.
                Each must be present in this topology.
            config: hping3 run configuration. Defaults to Hping3Config() if
                not provided. The same config is reused for every flow.

        Returns:
            A list of started Hping3Flow objects, one per (source, destination)
            pair. Poll each flow.status() for Hping3Status.DONE or
            Hping3Status.ERROR, then call flow.results() to get Hping3Results.

        Example::

            config = Hping3Config(proto="tcp", dport=80, count=50, flags=["S"])
            flows = topology_manager.run_hping3([src1, src2], [dst1, dst2], config)
            for flow in flows:
                while flow.status() == Hping3Status.RUNNING:
                    time.sleep(0.2)
                flow.results().print_summary()
        """
        if config is None:
            config = Hping3Config()
        sources = source if isinstance(source, list) else [source]
        destinations = destination if isinstance(destination, list) else [destination]
        flows: List[Hping3Flow] = []
        for src in sources:
            for dst in destinations:
                f = Hping3Flow(src, dst, config)
                f.start()
                flows.append(f)
        return flows

    def run_iperf3(
        self,
        source: Union[Node, List[Node]],
        destination: Union[Node, List[Node]],
        config: Iperf3Config,
    ) -> List[Iperf3Flow]:
        """Launch iperf3 flows for all (source, destination) combinations.

        Accepts a single Node or a list of Nodes for each of source and
        destination. Creates one Iperf3Flow per (source, destination) pair,
        starts each in a background thread, and returns the full list
        immediately.

        Both nodes must already be running containers (i.e., the topology
        must have been started via start_gonetem before calling this method).

        Args:
            source: Node or list of Nodes that will run the iperf3 client.
                Each must be a Satellite or GroundStation present in this
                topology.
            destination: Node or list of Nodes that will run the iperf3
                server. Each must be a Satellite or GroundStation present in
                this topology.
            config: Full iperf3 run configuration. The same config is reused
                for every flow. Construct with Iperf3Config to set protocol,
                bandwidth, congestion control, window size, and all other
                options.

        Returns:
            A list of started Iperf3Flow objects, one per (source, destination)
            pair. Poll each flow.status() for FlowStatus.DONE or
            FlowStatus.ERROR, then call flow.results() to get Iperf3Results.

        Raises:
            RuntimeError: If any flow cannot be started.

        Example::

            config = Iperf3Config(protocol="UDP", duration=30, bandwidth_mbps=50)
            flows = topology_manager.run_iperf3([src1, src2], [dst1, dst2], config)
            for flow in flows:
                while flow.status() == FlowStatus.RUNNING:
                    time.sleep(0.5)
                flow.results().print_summary()
        """
        sources = source if isinstance(source, list) else [source]
        destinations = destination if isinstance(destination, list) else [destination]
        flows: List[Iperf3Flow] = []
        for src in sources:
            for dst in destinations:
                f = Iperf3Flow(src, dst, config)
                f.start()
                flows.append(f)
        return flows

    def ping(
        self,
        source: Union[Node, List[Node]],
        destination: Union[Node, List[Node]],
        config: Optional[PingConfig] = None,
    ) -> List[PingFlow]:
        """Ping all (source, destination) combinations and return PingFlows.

        Accepts a single Node or a list of Nodes for each of source and
        destination. Creates one PingFlow per (source, destination) pair,
        starts each in a background thread, and returns the full list
        immediately.

        All nodes must already be running containers.

        Args:
            source: Node or list of Nodes that send ICMP echo requests.
            destination: Node or list of Nodes that are the ping targets.
            config: PingConfig controlling count, timeout, and interval.
                Defaults to PingConfig() when None. The same config is reused
                for every flow.

        Returns:
            A list of started PingFlow objects, one per (source, destination)
            pair. Poll each flow.status() for PingStatus.DONE or
            PingStatus.ERROR, then call flow.results() to retrieve PingResults.

        Example::

            flows = topology_manager.ping([gnd1, gnd2], [gnd3, gnd4])
            for flow in flows:
                while flow.status() == PingStatus.RUNNING:
                    time.sleep(0.2)
                flow.results().print_summary()
        """
        resolved_config = config or PingConfig()
        sources = source if isinstance(source, list) else [source]
        destinations = destination if isinstance(destination, list) else [destination]
        flows: List[PingFlow] = []
        for src in sources:
            for dst in destinations:
                f = PingFlow(src, dst, resolved_config)
                f.start()
                flows.append(f)
        return flows
