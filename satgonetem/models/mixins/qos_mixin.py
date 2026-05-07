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


class QoSCapableMixin:
    """
    Mixin that adds TBF-based global QoS initialisation to any Node subclass.

    Expected host-class attributes (provided by Node):
        name (str): Node identifier used for log messages.
        command_output (str): Last command stdout, set by execute_command.
        execute_command (callable): Executes a shell command inside the container.
    """

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
        """
        capacity_bytes = int(isl_capacity) * 1_000_000 / 8
        rate_bytes = int(demand_bit_rate) * 1_000_000 / 8
        num_flows = int(num_of_flows)
        mtu = 1500
        header_bytes = 42
        duration = num_of_bytes / rate_bytes

        self.execute_command(f"tc qdisc show dev {interface_name}")
        lines = self.command_output.strip().split("\n")

        tbf_pattern_latency = r"rate (\d+[GMK]?bit) burst (\d+b) lat (\d+(?:ms|us))"
        tbf_pattern_limit = r"rate (\d+[GMK]?bit) burst (\d+b) limit (\d+b)"

        match_latency = re.search(tbf_pattern_latency, lines[1])
        match_limit = re.search(tbf_pattern_limit, lines[1])

        if match_latency:
            tbf_rate = match_latency.group(1)
            tbf_burst = match_latency.group(2)
            tbf_latency = match_latency.group(3)
        elif match_limit:
            tbf_rate = match_limit.group(1)
            tbf_burst = match_limit.group(2)
        else:
            logging.error(
                "No TBF qdisc match found on %s interface %s",
                self.name,
                interface_name,
            )
            return

        tbf_burst_int = int(re.search(r"\d+", tbf_burst).group())

        if match_latency:
            tbf_latency_int = int(re.search(r"\d+", tbf_latency).group())
            tbf_limit_int = int(tbf_latency_int * capacity_bytes) + tbf_burst_int
        else:
            tbf_limit_str = match_limit.group(3)
            tbf_limit_int = int(re.search(r"\d+", tbf_limit_str).group())

        wire_rate_multiplier = mtu / (mtu - header_bytes)
        total_wire_rate = num_flows * rate_bytes * wire_rate_multiplier
        excess_rate = total_wire_rate - capacity_bytes

        if tbf_config == "limit":
            total_excess = excess_rate * duration
            required_limit = total_excess - tbf_burst_int
            additional_latency = max(
                0, (required_limit - tbf_burst_int) / capacity_bytes * 1000
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
            print(
                f"Configured TBF limit on link: {link}, "
                f"which introduces an additional latency of {additional_latency} ms."
            )

        elif tbf_config == "burstlimit":
            total_excess = excess_rate * duration
            buffer_time = 0.072
            required_limit = capacity_bytes * buffer_time
            required_burst = total_excess - required_limit
            additional_latency = max(
                0, (required_limit - required_burst) / capacity_bytes * 1000
            )
            command = (
                f"tc qdisc change dev {interface_name} parent 1:1 handle 10: "
                f"tbf rate {tbf_rate} burst {required_burst}b limit {required_limit}b"
            )
            self.execute_command(command)
            logging.info(
                "Configured TBF burst and limit on link %s; no additional latency.",
                link,
            )
            print(
                f"Configured TBF burst and limit on link: {link}, "
                "without the introduction of additional latency."
            )
