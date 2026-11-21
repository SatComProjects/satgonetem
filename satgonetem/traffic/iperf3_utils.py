"""Iperf3 compatibility facade.

The implementation lives in ``iperf3_ports``, ``iperf3_config``,
``iperf3_results``, ``iperf3_io``, ``iperf3_flow``, ``iperf3_runner``, and
``iperf3_plots``. Existing imports from
``satgonetem.traffic.iperf3_utils`` keep working.
"""

from satgonetem.traffic.iperf3_config import Iperf3Config
from satgonetem.traffic.iperf3_flow import FlowStatus, Iperf3Flow
from satgonetem.traffic.iperf3_io import (
    _extract_iperf3_json as _extract_iperf3_json,
)
from satgonetem.traffic.iperf3_io import (
    _is_iperf3_server_listening as _is_iperf3_server_listening,
)
from satgonetem.traffic.iperf3_io import (
    _text_outside_iperf3_json as _text_outside_iperf3_json,
)
from satgonetem.traffic.iperf3_io import (
    _wait_for_iperf3_server as _wait_for_iperf3_server,
)
from satgonetem.traffic.iperf3_plots import generate_iperf3_plots
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_PORT_ALLOCATOR as _IPERF3_PORT_ALLOCATOR,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_PORT_RANGE_END as _IPERF3_PORT_RANGE_END,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_PORT_RANGE_START as _IPERF3_PORT_RANGE_START,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_RESULT_POLL_INTERVAL as _IPERF3_RESULT_POLL_INTERVAL,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_RESULT_WAIT_TIMEOUT as _IPERF3_RESULT_WAIT_TIMEOUT,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_SERVER_READY_POLL_INTERVAL as _IPERF3_SERVER_READY_POLL_INTERVAL,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_SERVER_READY_TIMEOUT as _IPERF3_SERVER_READY_TIMEOUT,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_SERVER_START_FALLBACK as _IPERF3_SERVER_START_FALLBACK,
)
from satgonetem.traffic.iperf3_ports import (
    _Iperf3PortAllocator as _Iperf3PortAllocator,
)
from satgonetem.traffic.iperf3_ports import (
    _port_range_from_env as _port_range_from_env,
)
from satgonetem.traffic.iperf3_ports import (
    get_iperf3_port_range,
    set_iperf3_port_range,
)
from satgonetem.traffic.iperf3_results import Iperf3Results
from satgonetem.traffic.iperf3_runner import run_iperf3_flow

__all__ = [
    "FlowStatus",
    "Iperf3Config",
    "Iperf3Flow",
    "Iperf3Results",
    "generate_iperf3_plots",
    "get_iperf3_port_range",
    "run_iperf3_flow",
    "set_iperf3_port_range",
]
