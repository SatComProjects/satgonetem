"""
QoSCapableMixin - Token Bucket Filter (TBF) QoS concern for network nodes.

Provides the initialise_global_qos method that was previously duplicated
between Satellite and GroundStation. Depends on the host class supplying:
  - self.name (str)
  - self.command_output (str): populated by execute_command
  - self.execute_command(command) method
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any


class QoSCapableMixin:
    """
    Mixin that adds TBF-based global QoS initialisation to any Node subclass.

    Expected host-class attributes (provided by Node):
        name (str): Node identifier used for log messages.
        command_output (str): Last command stdout, set by execute_command.
        execute_command (callable): Executes a shell command inside the container.
    """

    name: str
    command_output: str
    execute_command: Callable[..., Any]

    def initialise_global_qos(
        self,
        interface_name: str,
        num_of_flows: int,
        demand_bit_rate: int,
        isl_capacity: int,
        num_of_bytes: int,
        tbf_config: str,
        link: object,
    ) -> None:
        """
        Configure TBF queuing discipline parameters to minimise packet loss.

        Reads the existing qdisc settings from the container, computes the
        required burst/limit values based on traffic demand, and applies the
        updated TBF configuration.

        Args:
            interface_name: Name of the network interface to configure (e.g. 'eth0').
            num_of_flows: Number of concurrent traffic flows sharing the link.
            demand_bit_rate: Demanded bit rate per flow in Mbps.
            isl_capacity: Link capacity in Mbps.
            num_of_bytes: Total bytes to be sent (used to derive test duration T).
            tbf_config: TBF tuning mode. Supported values:
                'limit' - adjust only the TBF limit field.
                'burstlimit' - adjust both burst and limit fields.
            link: Link identifier included in log messages.

        Returns:
            None

        Raises:
            ValueError: If *demand_bit_rate* or *isl_capacity* is not positive.
                A non-positive demand would divide by zero when deriving the
                test duration, and a non-positive capacity cannot shape any
                traffic.
        """
        capacity_mbps = int(isl_capacity)
        demand_mbps = int(demand_bit_rate)
        if demand_mbps <= 0:
            raise ValueError(
                f"demand_bit_rate must be positive, got {demand_bit_rate!r}"
            )
        if capacity_mbps <= 0:
            raise ValueError(f"isl_capacity must be positive, got {isl_capacity!r}")

        capacity_bytes = capacity_mbps * 1_000_000 / 8
        rate_bytes = demand_mbps * 1_000_000 / 8
        num_flows = int(num_of_flows)
        mtu = 1500
        header_bytes = 42
        duration = num_of_bytes / rate_bytes

        self.execute_command(f"tc qdisc show dev {interface_name}")
        lines = [line for line in self.command_output.strip().splitlines() if line]

        tbf_pattern_latency = r"rate (\d+[GMK]?bit) burst (\d+b) lat (\d+(?:ms|us))"
        tbf_pattern_limit = r"rate (\d+[GMK]?bit) burst (\d+b) limit (\d+b)"

        tbf_rate: str | None = None
        tbf_burst: str | None = None
        for line in lines:
            match_latency = re.search(tbf_pattern_latency, line)
            match_limit = re.search(tbf_pattern_limit, line)
            if match_latency:
                tbf_rate = match_latency.group(1)
                tbf_burst = match_latency.group(2)
                break
            if match_limit:
                tbf_rate = match_limit.group(1)
                tbf_burst = match_limit.group(2)
                break

        if tbf_rate is None or tbf_burst is None:
            logging.error(
                "No TBF qdisc match found on %s interface %s",
                self.name,
                interface_name,
            )
            return

        burst_match = re.search(r"\d+", tbf_burst)
        if burst_match is None:
            logging.error(
                "Could not parse TBF burst %r on %s interface %s",
                tbf_burst,
                self.name,
                interface_name,
            )
            return
        tbf_burst_int = int(burst_match.group())

        wire_rate_multiplier = mtu / (mtu - header_bytes)
        total_wire_rate = num_flows * rate_bytes * wire_rate_multiplier
        excess_rate = total_wire_rate - capacity_bytes

        if excess_rate <= 0:
            # The offered traffic fits in the link capacity: any limit/burst
            # derived from the (negative) excess would be invalid for tc.
            logging.info(
                "No TBF shaping required on link %s; offered rate is below capacity.",
                link,
            )
            return

        if tbf_config == "limit":
            total_excess = excess_rate * duration
            required_limit = int(total_excess - tbf_burst_int)
            if required_limit <= 0:
                logging.info(
                    "No TBF limit change required on link %s; excess fits in burst.",
                    link,
                )
                return
            additional_latency = max(
                0.0, (required_limit - tbf_burst_int) / capacity_bytes * 1000
            )
            command = (
                f"tc qdisc change dev {interface_name} parent 1:1 handle 10: "
                f"tbf rate {tbf_rate} burst {tbf_burst} limit {required_limit}b"
            )
            self.execute_command(command)
            logging.info(
                "Configured TBF limit on link %s; additional latency %.2f ms.",
                link,
                additional_latency,
            )

        elif tbf_config == "burstlimit":
            total_excess = excess_rate * duration
            buffer_time = 0.072
            buffer_limit = capacity_bytes * buffer_time
            required_burst = int(total_excess - buffer_limit)
            if required_burst <= 0:
                logging.info(
                    "No TBF burst change required on link %s; excess fits in limit.",
                    link,
                )
                return
            command = (
                f"tc qdisc change dev {interface_name} parent 1:1 handle 10: "
                f"tbf rate {tbf_rate} burst {required_burst}b "
                f"limit {int(buffer_limit)}b"
            )
            self.execute_command(command)
            logging.info(
                "Configured TBF burst and limit on link %s; no additional latency.",
                link,
            )
