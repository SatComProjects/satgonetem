from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib
import pandas as pd

matplotlib.use("Agg")  # non-GUI backend
import matplotlib.pyplot as plt

from satgonetem.ports import default_runner

if TYPE_CHECKING:
    pass


def generate_iperf3_plots(
    json_file_path: str, json_file_name: str = "iperf3", use_tmp_dir=True
) -> list[str]:
    """
    Read an iperf3 JSON result file and save all relevant plots (per-stream and summaries)
    into /tmp/results as PNGs. Returns the list of saved file paths and opens the folder.
    """
    # ---------- Load ----------
    with open(json_file_path) as f:
        data = json.load(f)
    protocol = str(
        data.get("start", {}).get("test_start", {}).get("protocol", "TCP")
    ).upper()

    # ---------- Output dir ----------
    if use_tmp_dir:
        out_dir = Path("/tmp/results")
    else:
        path = Path(json_file_path)
        path = path.parent
        out_dir = Path(f"{path}/results")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- Flatten helpers ----------
    def flatten_intervals(d):
        intervals = d.get("intervals", [])
        stream_rows = []
        sum_rows = []
        for it in intervals:
            for s in it.get("streams", []):
                row = dict(s)
                row["_t"] = s.get("end", s.get("start"))
                row["_label"] = f"Socket {s.get('socket', '?')}"
                stream_rows.append(row)
            for sum_key in [
                "sum",
                "sum_sent",
                "sum_received",
                "sum_bidir",
                "sum_bidir_reverse",
            ]:
                if sum_key in it:
                    s = dict(it[sum_key])
                    s["_t"] = s.get("end", s.get("start"))
                    s["_label"] = sum_key
                    sum_rows.append(s)
        return (
            pd.DataFrame(stream_rows) if stream_rows else pd.DataFrame(),
            pd.DataFrame(sum_rows) if sum_rows else pd.DataFrame(),
        )

    df_streams, df_summaries = flatten_intervals(data)

    # ---------- Metric maps ----------
    def _id(x):
        return x

    def _bps_to_mbps(x):
        return x / 1_000_000  # Convert bps to Mbps

    def _bytes_to_mb(x):
        return x / 1_000_000  # Convert bytes to MB

    def _bytes_to_kb(x):
        return x / 1_000  # Convert bytes to KB

    metric_map_tcp = {
        "bits_per_second": ("Throughput (Mbps)", _bps_to_mbps),
        "bytes": ("Bytes (MB)", _bytes_to_mb),
        "retransmits": ("Retransmits (count)", _id),
        "snd_cwnd": ("Send Congestion Window (KB)", _bytes_to_kb),
        "snd_wnd": ("Send Window (KB)", _bytes_to_kb),
        "rtt": ("RTT (ms)", lambda s: s / 1000.0),  # usec -> ms
        "rttvar": ("RTT Var (usec)", _id),
        "pmtu": ("Path MTU (bytes)", _id),
    }
    metric_map_udp = {
        "bits_per_second": ("Throughput (Mbps)", _bps_to_mbps),
        "bytes": ("Bytes (MB)", _bytes_to_mb),
        "packets": ("Packets", _id),
        "jitter_ms": ("Jitter (ms)", _id),
        "lost_packets": ("Lost Packets", _id),
        "lost_percent": ("Loss (%)", _id),
        "out_of_order": ("Out-of-Order Packets", _id),
        "pmtu": ("Path MTU (bytes)", _id),
    }
    metric_map = metric_map_udp if protocol == "UDP" else metric_map_tcp

    # ---------- Detect present metrics ----------
    def present_metrics(df, metric_map):
        if df.empty:
            return {}
        avail = {}
        for k, v in metric_map.items():
            if k in df.columns and df[k].notna().any():
                avail[k] = v
        return avail

    stream_metrics = present_metrics(df_streams, metric_map)
    summary_metrics = present_metrics(df_summaries, metric_map)

    # ---------- Plotting helper ----------
    def plot_and_save(df, metrics, title_prefix, stem_prefix):
        saved = []
        if df.empty or not metrics:
            return saved
        tcol = (
            "end"
            if "end" in df.columns
            else ("_t" if "_t" in df.columns else "seconds")
        )
        # Ensure label column exists
        if "_label" not in df.columns:
            df = df.copy()
            if "socket" in df.columns:
                df["_label"] = df["socket"].apply(lambda s: f"Socket {s}")
            else:
                df["_label"] = "series"
        for metric_key, (pretty, tfm) in metrics.items():
            plt.figure(figsize=(10, 5))
            for label, g in df.groupby("_label"):
                plt.plot(g[tcol], tfm(g[metric_key]), label=str(label))
            plt.xlabel("Time (s)")
            plt.ylabel(pretty)
            plt.title(f"{title_prefix} — {pretty}")
            plt.legend()
            plt.grid(True)
            fname = f"{json_file_name}_{stem_prefix}_{metric_key}.png"
            fpath = out_dir / fname
            plt.savefig(fpath, bbox_inches="tight", dpi=150)
            fname = f"{json_file_name}_{stem_prefix}_{metric_key}.pdf"
            fpath = out_dir / fname
            plt.savefig(fpath, bbox_inches="tight", dpi=150)
            plt.close()
            saved.append(str(fpath))
        return saved

    saved_files = []
    saved_files += plot_and_save(
        df_streams,
        stream_metrics,
        f"{protocol} per-stream",
        f"{protocol.lower()}_per_stream",
    )
    saved_files += plot_and_save(
        df_summaries,
        summary_metrics,
        f"{protocol} interval summaries",
        f"{protocol.lower()}_summaries",
    )

    # ---------- Cumulative data plot ----------
    def plot_cumulative_data(df, title_prefix, stem_prefix):
        saved = []
        if df.empty:
            return saved

        tcol = (
            "end"
            if "end" in df.columns
            else ("_t" if "_t" in df.columns else "seconds")
        )

        # Ensure label column exists
        if "_label" not in df.columns:
            df = df.copy()
            if "socket" in df.columns:
                df["_label"] = df["socket"].apply(lambda s: f"Socket {s}")
            else:
                df["_label"] = "series"

        # Only plot if bytes column exists
        if "bytes" in df.columns and df["bytes"].notna().any():
            plt.figure(figsize=(10, 5))
            for label, g in df.groupby("_label"):
                # Sort by time to ensure proper cumulative calculation
                g_sorted = g.sort_values(by=tcol)
                cumulative_mb = g_sorted["bytes"].cumsum() / 1_000_000  # Convert to MB
                plt.plot(g_sorted[tcol], cumulative_mb, label=str(label))

            plt.xlabel("Time (s)")
            plt.ylabel("Cumulative Data Transmitted (MB)")
            plt.title(f"{title_prefix} — Cumulative Data Transmitted")
            plt.legend()
            plt.grid(True)
            fname = f"{json_file_name}_{stem_prefix}_cumulative_data.png"
            fpath = out_dir / fname
            plt.savefig(fpath, bbox_inches="tight", dpi=150)
            fname = f"{json_file_name}_{stem_prefix}_cumulative_data.pdf"
            fpath = out_dir / fname
            plt.savefig(fpath, bbox_inches="tight", dpi=150)
            plt.close()
            saved.append(str(fpath))

        return saved

    # Add cumulative plots for both streams and summaries
    saved_files += plot_cumulative_data(
        df_streams, f"{protocol} per-stream", f"{protocol.lower()}_per_stream"
    )
    saved_files += plot_cumulative_data(
        df_summaries, f"{protocol} interval summaries", f"{protocol.lower()}_summaries"
    )

    # ---------- Calculate and print statistics ----------
    def calculate_and_print_stats(df_streams, df_summaries, protocol):
        # Clear console
        # os.system('clear' if os.name == 'posix' else 'cls')

        print("=" * 60)
        print(f"           {protocol} IPERF3 TEST STATISTICS")
        print("=" * 60)

        # Use summary data if available, otherwise use stream data
        df_for_stats = df_summaries if not df_summaries.empty else df_streams

        if df_for_stats.empty:
            print("No data available for statistics calculation.")
            return

        # Average Throughput (Mbps)
        if "bits_per_second" in df_for_stats.columns:
            avg_throughput_bps = df_for_stats["bits_per_second"].mean()
            avg_throughput_mbps = avg_throughput_bps / 1_000_000
            print(f"Average Throughput:      {avg_throughput_mbps:.2f} Mbps")
        else:
            print("Average Throughput:      Not available")

        # Total Retransmissions (TCP only)
        if protocol == "TCP" and "retransmits" in df_for_stats.columns:
            total_retransmits = df_for_stats["retransmits"].sum()
            print(f"Total Retransmissions:   {total_retransmits:.0f}")
        elif protocol == "TCP":
            print("Total Retransmissions:   Not available")

        # Average RTT (TCP only)
        if protocol == "TCP" and "rtt" in df_for_stats.columns:
            # RTT is in microseconds, convert to milliseconds
            avg_rtt_us = df_for_stats["rtt"].mean()
            avg_rtt_ms = avg_rtt_us / 1000.0
            print(f"Average RTT:             {avg_rtt_ms:.2f} ms")
        elif protocol == "TCP":
            print("Average RTT:             Not available")

        # UDP-specific stats
        if protocol == "UDP":
            if "lost_packets" in df_for_stats.columns:
                total_lost = df_for_stats["lost_packets"].sum()
                print(f"Total Lost Packets:      {total_lost:.0f}")

            if "lost_percent" in df_for_stats.columns:
                avg_loss_percent = df_for_stats["lost_percent"].mean()
                print(f"Average Packet Loss:     {avg_loss_percent:.2f}%")

            if "jitter_ms" in df_for_stats.columns:
                avg_jitter = df_for_stats["jitter_ms"].mean()
                print(f"Average Jitter:          {avg_jitter:.2f} ms")

        # Total Data Transferred
        if "bytes" in df_for_stats.columns:
            total_bytes = df_for_stats["bytes"].sum()
            total_mb = total_bytes / 1_000_000
            print(f"Total Data Transferred:  {total_mb:.2f} MB")

        print("=" * 60)
        print(f"Plots saved to: {out_dir}")
        print("=" * 60)

    calculate_and_print_stats(df_streams, df_summaries, protocol)

    # ---------- Open folder in explorer ----------
    if use_tmp_dir:
        try:
            system = platform.system()
            if system == "Darwin":  # macOS
                default_runner.run(["open", str(out_dir)], check=False, capture=False)
            elif system == "Windows":
                default_runner.run(
                    ["explorer", str(out_dir)], check=False, capture=False
                )
            else:  # Linux and others
                default_runner.run(
                    ["xdg-open", str(out_dir)], check=False, capture=False
                )
        except Exception:
            pass

    return saved_files


# Example:
# files = generate_iperf_plots("/path/to/iperf3.json")
# print("Saved plots:", files)
