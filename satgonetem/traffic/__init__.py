"""Lazy public exports for the traffic package.

Importing ``satgonetem.traffic`` must not load pandas or matplotlib. Names are
resolved on first access from their implementation modules.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "FlowScheduler": "satgonetem.traffic.flow_scheduler",
    "FlowSchedulerStatus": "satgonetem.traffic.flow_scheduler",
    "Hping3Config": "satgonetem.traffic.hping3_utils",
    "Hping3Flow": "satgonetem.traffic.hping3_utils",
    "Hping3Results": "satgonetem.traffic.hping3_utils",
    "Hping3Status": "satgonetem.traffic.hping3_utils",
    "run_hping3": "satgonetem.traffic.hping3_utils",
    "FlowStatus": "satgonetem.traffic.iperf3_utils",
    "Iperf3Config": "satgonetem.traffic.iperf3_utils",
    "Iperf3Flow": "satgonetem.traffic.iperf3_utils",
    "Iperf3Results": "satgonetem.traffic.iperf3_utils",
    "run_iperf3_flow": "satgonetem.traffic.iperf3_utils",
    "get_iperf3_port_range": "satgonetem.traffic.iperf3_utils",
    "set_iperf3_port_range": "satgonetem.traffic.iperf3_utils",
    "PingConfig": "satgonetem.traffic.ping_utils",
    "PingFlow": "satgonetem.traffic.ping_utils",
    "PingResults": "satgonetem.traffic.ping_utils",
    "PingStatus": "satgonetem.traffic.ping_utils",
    "run_ping": "satgonetem.traffic.ping_utils",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
