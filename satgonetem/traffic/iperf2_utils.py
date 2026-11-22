import platform
import re
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")  # non-GUI backend
import matplotlib.pyplot as plt

from satgonetem.ports import default_runner

# iperf2 prints transfer sizes in binary units (1 MBytes = 2**20 bytes) but
# bit rates in decimal units (1 Mbits/sec = 1e6 bit/s): the label differs
# even though both are printed with the same K/M/G prefixes.
_BYTES_PER_MIB = 1024 * 1024
_BITS_PER_MEGABIT = 1_000_000
_BYTE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
_BIT_UNITS = {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}

_IPERF2_INTERVAL = re.compile(
    r"^\[\s*(?P<stream>\d+|SUM)\]\s*"
    r"(?P<start>\d+(?:\.\d+)?)-\s*(?P<end>\d+(?:\.\d+)?)\s*sec\s*"
    r"(?P<transfer>[\d.]+)\s*(?P<transfer_unit>[KMG]?)Bytes\s*"
    r"(?P<rate>[\d.]+)\s*(?P<rate_unit>[KMG]?)bits/sec"
    r"(?:\s+(?P<jitter>[\d.]+)\s*ms\s+(?P<lost>\d+)\s*/\s*(?P<total>\d+)"
    r"\s*\((?P<loss_pct>[\d.]+)%\))?"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)


_SUMMARY_TRANSFER_TOLERANCE = 0.01


def _is_unlabelled_summary(rows: list[dict]) -> bool:
    """Whether the last row summarises every preceding interval row.

    iperf2 prints the single-stream final summary in the interval line format
    without a ``[SUM]`` label. A genuine interval row of another stream can
    share that final row's span, so the span is not sufficient: the last
    row's transfer must also be approximately the sum of the preceding rows.
    """
    if len(rows) < 2:
        return False
    intervals = rows[:-1]
    last = rows[-1]
    if last["start"] != min(row["start"] for row in intervals):
        return False
    if last["end"] != max(row["end"] for row in intervals):
        return False
    interval_bytes = sum(row["bytes"] for row in intervals)
    tolerance = max(interval_bytes * _SUMMARY_TRANSFER_TOLERANCE, 1.0)
    return abs(last["bytes"] - interval_bytes) <= tolerance


def _parse_iperf2_log(
    lines: list[str],
) -> tuple[str, list[dict], dict | None]:
    """Parse iperf2 interval lines into data rows and a final summary.

    A row is a summary when it is explicitly labelled ``[SUM]`` or when it is
    the last row, spans the union of the preceding interval rows, and its
    transfer is approximately their sum. Span length alone is never used as
    the discriminator: iperf2 intervals may legitimately be longer than one
    second, and several streams can share a one-second span.

    Args:
        lines: iperf2 log lines (newlines optional).

    Returns:
        A 3-tuple ``(protocol, data_rows, summary)``. ``data_rows`` carry the
        displayed ``_label``; ``summary`` is a dict or None.
    """
    parsed: list[dict] = []
    protocol = "TCP"

    for line in lines:
        match = _IPERF2_INTERVAL.match(line.strip())
        if match is None:
            continue
        fields = match.groupdict()
        transfer_unit = (fields["transfer_unit"] or "").upper()
        rate_unit = (fields["rate_unit"] or "").upper()
        row: dict = {
            "_stream": (fields["stream"] or "").upper(),
            "start": float(fields["start"]),
            "end": float(fields["end"]),
            "bytes": float(fields["transfer"]) * _BYTE_UNITS[transfer_unit],
            "bits_per_second": float(fields["rate"]) * _BIT_UNITS[rate_unit],
        }
        if fields["jitter"] is not None:
            protocol = "UDP"
            row.update(
                {
                    "jitter_ms": float(fields["jitter"]),
                    "lost_packets": int(fields["lost"]),
                    "total_packets": int(fields["total"]),
                    "lost_percent": float(fields["loss_pct"]),
                }
            )
        else:
            retransmits = re.match(r"\s*(\d+)", fields["rest"] or "")
            row["retransmits"] = (
                int(retransmits.group(1)) if retransmits is not None else 0
            )
        parsed.append(row)

    if not parsed:
        return protocol, [], None

    sum_rows = [i for i, row in enumerate(parsed) if row["_stream"] == "SUM"]
    if sum_rows:
        summary_index = sum_rows[-1]
    elif _is_unlabelled_summary(parsed):
        summary_index = len(parsed) - 1
    else:
        summary_index = None

    summary_row_indices = set(sum_rows)
    if summary_index is not None:
        summary_row_indices.add(summary_index)

    data_rows: list[dict] = []
    for index, row in enumerate(parsed):
        if index in summary_row_indices:
            continue
        data_row = dict(row)
        data_row["_label"] = f"Socket {data_row.pop('_stream')}"
        data_rows.append(data_row)

    summary: dict | None = None
    if summary_index is not None:
        source = parsed[summary_index]
        summary = {
            "bytes": source["bytes"],
            "bits_per_second": source["bits_per_second"],
        }
        if "jitter_ms" in source:
            summary.update(
                {
                    "jitter_ms": source["jitter_ms"],
                    "lost_packets": source["lost_packets"],
                    "total_packets": source["total_packets"],
                    "lost_percent": source["lost_percent"],
                }
            )

    return protocol, data_rows, summary


def generate_iperf2_plots(
    log_file_path: str, log_file_name: str = "iperf2", use_tmp_dir=True
) -> list[str]:
    """
    Parse an iperf2 log file and generate plots similar to iperf3.
    Supports TCP and UDP logs.
    """
    # ---------- Output dir ----------
    if use_tmp_dir:
        out_dir = Path("/tmp/results")
    else:
        path = Path(log_file_path)
        path = path.parent
        out_dir = Path(f"{path}/results")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- Parse log ----------
    lines = []
    with open(log_file_path) as f:
        lines = f.readlines()

    protocol, data_rows, summary_data = _parse_iperf2_log(lines)

    if not data_rows:
        print("No valid iperf2 data found in log.")
        return []

    df = pd.DataFrame(data_rows)

    # ---------- Plot helper ----------
    def plot_metric(df, metric, ylabel, title, filename):
        plt.figure(figsize=(10, 5))
        for label, g in df.groupby("_label"):
            plt.plot(g["end"], g[metric], label=label)
        plt.xlabel("Time (s)")
        plt.ylabel(ylabel)
        plt.title(title)
        plt.legend()
        plt.grid(True)
        fpath = out_dir / filename
        plt.savefig(fpath, bbox_inches="tight", dpi=150)
        plt.close()
        return str(fpath)

    saved_files = []

    # Throughput
    saved_files.append(
        plot_metric(
            df,
            "bits_per_second",
            "Throughput (Mbps)",
            f"{protocol} Throughput per Socket",
            f"{log_file_name}_{protocol.lower()}_throughput.png",
        )
    )

    # Bytes
    saved_files.append(
        plot_metric(
            df,
            "bytes",
            "Bytes (MB)",
            f"{protocol} Bytes per Socket",
            f"{log_file_name}_{protocol.lower()}_bytes.png",
        )
    )

    # Retransmits for TCP
    if protocol == "TCP" and "retransmits" in df.columns:
        saved_files.append(
            plot_metric(
                df,
                "retransmits",
                "Retransmits",
                f"{protocol} Retransmits per Socket",
                f"{log_file_name}_{protocol.lower()}_retransmits.png",
            )
        )

    # Jitter/Loss for UDP
    if protocol == "UDP":
        if "jitter_ms" in df.columns:
            saved_files.append(
                plot_metric(
                    df,
                    "jitter_ms",
                    "Jitter (ms)",
                    f"{protocol} Jitter per Socket",
                    f"{log_file_name}_{protocol.lower()}_jitter.png",
                )
            )
        if "lost_percent" in df.columns:
            saved_files.append(
                plot_metric(
                    df,
                    "lost_percent",
                    "Packet Loss (%)",
                    f"{protocol} Packet Loss per Socket",
                    f"{log_file_name}_{protocol.lower()}_loss.png",
                )
            )

    # Cumulative data plot
    plt.figure(figsize=(10, 5))
    for label, g in df.groupby("_label"):
        g_sorted = g.sort_values("end")
        cumulative_mb = g_sorted["bytes"].cumsum() / _BYTES_PER_MIB
        plt.plot(g_sorted["end"], cumulative_mb, label=label)
    plt.xlabel("Time (s)")
    plt.ylabel("Cumulative Data Transmitted (MB)")
    plt.title(f"{protocol} Cumulative Data per Socket")
    plt.legend()
    plt.grid(True)
    fpath = out_dir / f"{log_file_name}_{protocol.lower()}_cumulative.png"
    plt.savefig(fpath, bbox_inches="tight", dpi=150)
    plt.close()
    saved_files.append(str(fpath))

    # ---------- Print summary ----------
    print("=" * 60)
    print(f"           {protocol} IPERF2 TEST STATISTICS")
    print("=" * 60)
    # Throughput
    avg_throughput_mbps = df["bits_per_second"].mean() / _BITS_PER_MEGABIT
    print(f"Average Throughput:      {avg_throughput_mbps:.2f} Mbps")
    # Retransmits / Loss
    if protocol == "TCP":
        if "retransmits" in df.columns:
            total_retransmits = df["retransmits"].sum()
            print(f"Total Retransmits:       {total_retransmits}")
    else:
        if summary_data:
            total_lost = summary_data["lost_packets"]
            avg_loss = summary_data["lost_percent"]
            avg_jitter = summary_data["jitter_ms"]
        else:
            total_lost = df["lost_packets"].sum()
            avg_loss = df["lost_percent"].mean()
            avg_jitter = df["jitter_ms"].mean()
        print(f"Total Lost Packets:      {total_lost}")
        print(f"Packet Loss %:           {avg_loss:.2f}%")
        print(f"Average Jitter:          {avg_jitter:.2f} ms")
    # Total data: the final summary row covers the whole test when present,
    # so it is not double-counted with the interval rows.
    if summary_data:
        total_mb = summary_data["bytes"] / _BYTES_PER_MIB
    else:
        total_mb = df["bytes"].sum() / _BYTES_PER_MIB
    print(f"Total Data Transferred:  {total_mb:.2f} MB")
    print("=" * 60)
    print(f"Plots saved to: {out_dir}")
    print("=" * 60)

    # ---------- Open folder ----------
    if use_tmp_dir:
        try:
            system = platform.system()
            if system == "Darwin":
                default_runner.run(["open", str(out_dir)], check=False, capture=False)
            elif system == "Windows":
                win_path = Path(out_dir).as_posix().replace("/", "\\")
                default_runner.run(["explorer", win_path], check=False, capture=False)
            else:
                default_runner.run(
                    ["xdg-open", str(out_dir)], check=False, capture=False
                )
        except Exception:
            pass

    return saved_files


# Example:
# files = generate_iperf2_plots("/path/to/iperf2.log")
# print("Saved plots:", files)
