"""Utility for converting CICFlowMeter CSV exports into satgonetem flow objects.

Each row is classified into one of three flow types and mapped to a
corresponding config + flow instance:

- PingFlow: rows with no TCP flags and a small average packet size (< 100 B).
- Hping3Flow: rows labelled as an attack (DDoS, DoS, PortScan, etc.) or rows
  where SYN > 0 and ACK == 0, indicating a SYN scan or flood pattern.
- Iperf3Flow: all remaining rows, using TCP when any flag column is non-zero
  and UDP otherwise.

The CICFlowMeter ISCX format (used in CICIDS2017) does not carry per-row
source or destination IPs, so callers must supply nodes explicitly. Passing
a list of nodes causes a random node to be selected independently for each
row, which is useful when replaying traffic across a multi-node topology.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import TYPE_CHECKING, Sequence, Union

import pandas as pd

from satgonetem.traffic.hping3_utils import Hping3Config, Hping3Flow
from satgonetem.traffic.iperf3_utils import Iperf3Config, Iperf3Flow
from satgonetem.traffic.ping_utils import PingConfig, PingFlow

if TYPE_CHECKING:
    from satgonetem.models.node import Node

_ATTACK_KEYWORDS = ("DOS", "DDOS", "PORTSCAN", "BOTNET", "INFILTRATION", "BRUTEFORCE")
_TCP_FLAG_COLS = (
    "SYN Flag Count",
    "ACK Flag Count",
    "FIN Flag Count",
    "RST Flag Count",
    "PSH Flag Count",
    "URG Flag Count",
)
_SMALL_PACKET_BYTES = 100
_TCP_IP_HEADER_BYTES = 40
_MIN_DURATION_SEC = 1
_MIN_PING_INTERVAL_SEC = 0.2
_MIN_PING_TIMEOUT_SEC = 0.5
_DEFAULT_IPERF3_PORT = 5201


def _is_attack_label(label: str) -> bool:
    """Return True if the label string signals known attack traffic.

    Args:
        label: The raw label value from the CICFlowMeter 'Label' column.

    Returns:
        True when the uppercased label contains any keyword from
        _ATTACK_KEYWORDS.
    """
    upper = label.upper()
    return any(kw in upper for kw in _ATTACK_KEYWORDS)


def _has_tcp_flags(row: pd.Series) -> bool:
    """Return True if any TCP flag counter is non-zero in this row.

    Args:
        row: A single CICFlowMeter row as a pandas Series.

    Returns:
        True when at least one flag column holds a positive integer.
    """
    return any(int(row.get(col, 0)) > 0 for col in _TCP_FLAG_COLS)


def _active_flags(row: pd.Series) -> list[str]:
    """Return hping3 flag characters for every non-zero TCP flag column.

    Args:
        row: A single CICFlowMeter row as a pandas Series.

    Returns:
        A list of single-character flag strings (e.g. ['S', 'A']).
        Falls back to ['S'] when no flag column is non-zero, ensuring
        hping3 always has at least one flag to set.
    """
    mapping = [
        ("SYN Flag Count", "S"),
        ("ACK Flag Count", "A"),
        ("FIN Flag Count", "F"),
        ("RST Flag Count", "R"),
        ("PSH Flag Count", "P"),
        ("URG Flag Count", "U"),
    ]
    flags = [flag for col, flag in mapping if int(row.get(col, 0)) > 0]
    return flags if flags else ["S"]


def _classify_row(row: pd.Series) -> str:
    """Determine the flow type for a single CICFlowMeter row.

    Classification priority:
    1. Attack label or SYN-only pattern -> 'hping3'
    2. No TCP flags + small average packet size -> 'ping'
    3. Everything else -> 'iperf3'

    Args:
        row: A single CICFlowMeter row as a pandas Series.

    Returns:
        One of 'ping', 'hping3', or 'iperf3'.
    """
    label = str(row.get("Label", ""))
    if _is_attack_label(label):
        return "hping3"

    syn = int(row.get("SYN Flag Count", 0))
    ack = int(row.get("ACK Flag Count", 0))
    rst = int(row.get("RST Flag Count", 0))
    if (syn > 0 and ack == 0) or (rst > 0 and ack == 0 and syn == 0):
        return "hping3"

    avg_pkt = float(row.get("Average Packet Size", 0))
    if not _has_tcp_flags(row) and avg_pkt < _SMALL_PACKET_BYTES:
        return "ping"

    return "iperf3"


def _make_ping_config(row: pd.Series) -> PingConfig:
    """Build a PingConfig that approximates the packet behaviour in this row.

    Args:
        row: A single CICFlowMeter row as a pandas Series.

    Returns:
        A PingConfig instance derived from forward packet statistics.
    """
    count = max(1, int(row.get("Total Fwd Packets", 5)))
    fwd_iat_us = float(row.get("Fwd IAT Mean", 200_000))
    interval_sec = max(_MIN_PING_INTERVAL_SEC, fwd_iat_us / 1_000_000)
    flow_iat_max_us = float(row.get("Flow IAT Max", 500_000))
    timeout_sec = max(_MIN_PING_TIMEOUT_SEC, flow_iat_max_us / 1_000_000)
    pkt_size = max(1, int(float(row.get("Fwd Packet Length Mean", 64))))
    return PingConfig(
        count=count,
        interval_sec=round(interval_sec, 6),
        timeout_sec=round(timeout_sec, 6),
        packet_size=pkt_size,
    )


def _make_hping3_config(row: pd.Series) -> Hping3Config:
    """Build an Hping3Config that approximates the attack pattern in this row.

    The send rate is derived from 'Fwd Packets/s': flows above 10,000 pps
    use flood mode, above 1,000 use faster, above 100 use fast, and below
    100 use an explicit microsecond interval derived from 'Fwd IAT Mean'.

    Args:
        row: A single CICFlowMeter row as a pandas Series.

    Returns:
        An Hping3Config instance derived from forward packet statistics and
        TCP flag columns.
    """
    flags = _active_flags(row)
    pkt_per_sec = float(row.get("Fwd Packets/s", 0))

    if pkt_per_sec > 10_000:
        rate_type, interval = "flood", ""
    elif pkt_per_sec > 1_000:
        rate_type, interval = "faster", ""
    elif pkt_per_sec > 100:
        rate_type, interval = "fast", ""
    else:
        fwd_iat_us = float(row.get("Fwd IAT Mean", 10_000))
        rate_type = "interval"
        interval = f"u{max(1, int(fwd_iat_us))}"

    fwd_pkt_mean = float(row.get("Fwd Packet Length Mean", 40))
    payload = max(0, int(fwd_pkt_mean) - _TCP_IP_HEADER_BYTES)

    return Hping3Config(
        proto="tcp",
        dport=int(row.get("Destination Port", 0)),
        count=max(1, int(row.get("Total Fwd Packets", 100))),
        size=payload,
        flags=flags,
        rate_type=rate_type,
        interval=interval,
    )


def _make_iperf3_config(row: pd.Series) -> Iperf3Config:
    """Build an Iperf3Config that approximates the bulk flow in this row.

    Protocol is set to TCP when any TCP flag column is non-zero; UDP otherwise.
    Bandwidth is derived from 'Flow Bytes/s' converted to Mbps.

    Args:
        row: A single CICFlowMeter row as a pandas Series.

    Returns:
        An Iperf3Config instance derived from flow duration and byte rate.
    """
    protocol = "TCP" if _has_tcp_flags(row) else "UDP"
    duration_us = float(row.get("Flow Duration", 10_000_000))
    duration_sec = max(_MIN_DURATION_SEC, int(duration_us / 1_000_000))
    bw_bytes_per_sec = float(row.get("Flow Bytes/s", 0))
    bw_mbps = round(bw_bytes_per_sec * 8 / 1_000_000, 3)
    port = int(row.get("Destination Port", 0))

    return Iperf3Config(
        protocol=protocol,
        duration=duration_sec,
        bandwidth_mbps=bw_mbps,
        port=port if port > 0 else _DEFAULT_IPERF3_PORT,
    )


def _apply_sample_fraction(df: pd.DataFrame, fraction: float) -> pd.DataFrame:
    """Return a systematically sampled subset of df.

    Rows are retained by keeping 'fraction' of them and evenly distributing
    the skips throughout the dataset. For fraction=0.9, every 10th row is
    dropped; for fraction=0.5, every 2nd row is dropped, and so on. Row order
    is preserved.

    Args:
        df: Full CICFlowMeter DataFrame after loading.
        fraction: Proportion of rows to keep. Must be in the range (0.0, 1.0].
            1.0 returns df unchanged.

    Returns:
        A new DataFrame containing only the retained rows, index reset.

    Raises:
        ValueError: If fraction is not in (0.0, 1.0].
    """
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"sample_fraction must be in (0.0, 1.0], got {fraction}")
    if fraction == 1.0:
        return df.reset_index(drop=True)
    step = max(1, round(1.0 / fraction))
    return df.iloc[::step].reset_index(drop=True)


def flows_from_cicflowmeter(
    csv_path: Union[str, Path],
    source: Union["Node", Sequence["Node"]],
    destination: Union["Node", Sequence["Node"]],
    sample_fraction: float = 1.0,
) -> list[Union[Iperf3Flow, Hping3Flow, PingFlow]]:
    """Read a CICFlowMeter CSV and build a list of flow objects with delays.

    Each row is classified and converted into a PingFlow, Hping3Flow, or
    Iperf3Flow. The delay of each flow is set to the cumulative sum of all
    preceding flows' durations (in seconds), so that calling start() on all
    returned flows simultaneously produces a time-ordered replay.

    The CICFlowMeter ISCX format does not include per-row source or destination
    IP addresses. When a list of nodes is provided for source or destination a
    node is chosen at random for each row, enabling traffic to be spread across
    a multi-node topology.

    Args:
        csv_path: Path to the CICFlowMeter CSV export file.
        source: A single node or a list of nodes to draw from randomly. Acts
            as the traffic originator (iperf3/hping3 client or ping sender).
        destination: A single node or a list of nodes to draw from randomly.
            Acts as the traffic receiver (iperf3 server or hping3/ping target).
        sample_fraction: Proportion of rows to keep, in the range (0.0, 1.0].
            Rows are dropped by evenly distributing skips throughout the file
            (e.g. 0.9 drops every 10th row). Defaults to 1.0 (keep all rows).

    Returns:
        A list of flow objects in CSV row order. Each flow carries a delay
        equal to the cumulative duration of all preceding flows in seconds.
        No flow is started; callers must invoke flow.start() themselves.

    Raises:
        FileNotFoundError: If csv_path does not point to an existing file.
        ValueError: If the CSV contains no data rows after sampling, if
            sample_fraction is not in (0.0, 1.0], or if a node list is empty.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CICFlowMeter CSV not found: {path}")

    source_pool = source if isinstance(source, list) else [source]
    dest_pool = destination if isinstance(destination, list) else [destination]
    if not source_pool:
        raise ValueError("source list must not be empty")
    if not dest_pool:
        raise ValueError("destination list must not be empty")

    df = pd.read_csv(path, skipinitialspace=True)
    df.columns = df.columns.str.strip()
    df = df.fillna(0)
    df = _apply_sample_fraction(df, sample_fraction)

    if df.empty:
        raise ValueError(f"CSV file contains no data rows after sampling: {path}")

    delays: pd.Series = (
        df["Flow Duration"].shift(1, fill_value=0.0).cumsum() / 1_000_000
    )

    flows: list[Union[Iperf3Flow, Hping3Flow, PingFlow]] = []
    for i, (_, row) in enumerate(df.iterrows()):
        src = random.choice(source_pool)
        dst = random.choice(dest_pool)
        delay = float(delays.iloc[i])
        kind = _classify_row(row)
        if kind == "ping":
            flow: Union[Iperf3Flow, Hping3Flow, PingFlow] = PingFlow(
                src, dst, _make_ping_config(row), delay=delay
            )
        elif kind == "hping3":
            flow = Hping3Flow(
                src, dst, _make_hping3_config(row), delay=delay
            )
        else:
            flow = Iperf3Flow(
                src, dst, _make_iperf3_config(row), delay=delay
            )
        flows.append(flow)

    return flows
