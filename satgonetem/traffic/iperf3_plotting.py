"""Plot generation for parsed iperf3 interval data."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib
import pandas as pd

matplotlib.use("Agg")  # non-GUI backend
import matplotlib.pyplot as plt

from satgonetem.traffic.iperf3_units import (
    BPS_TO_MBPS,
    BYTES_TO_KB,
    BYTES_TO_MB,
    USEC_TO_MS,
)


class Iperf3Plotter:
    """Plot protocol-appropriate metrics from flattened iperf3 intervals.

    Args:
        protocol: Transport protocol, ``'TCP'`` or ``'UDP'``.
        df_streams: Flattened per-stream interval rows.
        df_summaries: Flattened per-interval summary rows.
    """

    def __init__(
        self,
        protocol: str,
        df_streams: pd.DataFrame,
        df_summaries: pd.DataFrame,
    ) -> None:
        self._protocol = protocol
        self._df_streams = df_streams
        self._df_summaries = df_summaries

    def _metric_map(self) -> dict[str, tuple[str, Callable]]:
        """Return the protocol-appropriate metric label and transform map."""

        def _id(x: Any) -> Any:
            return x

        if self._protocol == "UDP":
            return {
                "bits_per_second": ("Throughput (Mbps)", lambda x: x / BPS_TO_MBPS),
                "bytes": ("Bytes (MB)", lambda x: x / BYTES_TO_MB),
                "packets": ("Packets", _id),
                "jitter_ms": ("Jitter (ms)", _id),
                "lost_packets": ("Lost Packets", _id),
                "lost_percent": ("Loss (%)", _id),
                "out_of_order": ("Out-of-Order Packets", _id),
                "pmtu": ("Path MTU (bytes)", _id),
            }
        return {
            "bits_per_second": ("Throughput (Mbps)", lambda x: x / BPS_TO_MBPS),
            "bytes": ("Bytes (MB)", lambda x: x / BYTES_TO_MB),
            "retransmits": ("Retransmits (count)", _id),
            "snd_cwnd": ("Send CWnd (KB)", lambda x: x / BYTES_TO_KB),
            "snd_wnd": ("Send Window (KB)", lambda x: x / BYTES_TO_KB),
            "rtt": ("RTT (ms)", lambda x: x / USEC_TO_MS),
            "rttvar": ("RTT Var (us)", _id),
            "pmtu": ("Path MTU (bytes)", _id),
        }

    def plot_metric(
        self,
        metric: str,
        output_dir: str = "/tmp/results",
        file_stem: str = "iperf3",
        use_summary: bool = True,
        fmt: str = "png",
    ) -> str | None:
        """Plot a single time-series metric and save it to a file.

        Args:
            metric: iperf3 JSON field name to plot.
            output_dir: Directory where the output file is written. Created
                if it does not exist.
            file_stem: Base filename stem.
            use_summary: Prefer interval summary rows when the metric is
                present there; TCP-only series fall back to stream rows.
            fmt: File format for the output image.

        Returns:
            Path of the saved plot file, or None if the metric is absent.

        Raises:
            OSError: If the output directory cannot be created.
        """
        mmap = self._metric_map()
        if metric not in mmap:
            return None

        df = self._df_streams
        if (
            use_summary
            and not self._df_summaries.empty
            and metric in self._df_summaries.columns
            and self._df_summaries[metric].notna().any()
        ):
            # Summary rows only carry the shared TCP/UDP metrics; TCP-only
            # series (RTT, cwnd, PMTU) must still fall back to stream rows.
            df = self._df_summaries
        if df.empty or metric not in df.columns or df[metric].isna().all():
            return None

        pretty, transform = mmap[metric]
        tcol = (
            "end"
            if "end" in df.columns
            else ("_t" if "_t" in df.columns else "seconds")
        )

        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        fig, ax = plt.subplots(figsize=(10, 5))
        for label, group in df.groupby("_label"):
            ax.plot(group[tcol], transform(group[metric]), label=str(label))
        ax.set_xlabel("Time (s)")
        ax.set_ylabel(pretty)
        ax.set_title(f"{self._protocol} - {pretty}")
        ax.legend()
        ax.grid(True)

        fpath = out_path / f"{file_stem}_{metric}.{fmt}"
        fig.savefig(fpath, bbox_inches="tight", dpi=150)
        plt.close(fig)
        return str(fpath)

    def plot_all(
        self,
        output_dir: str = "/tmp/results",
        file_stem: str = "iperf3",
        fmt: str = "png",
    ) -> list[str]:
        """Plot all available metrics to separate files.

        Returns:
            List of paths of all successfully saved plot files.
        """
        saved: list[str] = []
        for metric in self._metric_map():
            path = self.plot_metric(metric, output_dir, file_stem, fmt=fmt)
            if path:
                saved.append(path)
        return saved


__all__ = ["Iperf3Plotter"]
