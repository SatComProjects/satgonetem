"""Interval flattening and aggregation for parsed iperf3 JSON output."""

from __future__ import annotations

from typing import Any

import pandas as pd

from satgonetem.traffic.iperf3_units import BPS_TO_MBPS, USEC_TO_MS

# iperf3 names the reverse channel of a --bidir test by appending this suffix
# to the forward summary label.
BIDIR_REVERSE_LABELS: dict[str, str] = {
    "sum": "sum_bidir_reverse",
    "sum_sent": "sum_sent_bidir_reverse",
    "sum_received": "sum_received_bidir_reverse",
}


class Iperf3IntervalData:
    """Flattened interval rows plus their derived time-series aggregates.

    Args:
        data: The complete parsed iperf3 JSON document.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.df_streams, self.df_summaries = self._flatten_intervals(data)

    def _flatten_intervals(
        self, data: dict[str, Any]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Flatten iperf3 intervals into per-stream and per-summary DataFrames.

        Each stream row is annotated with ``_t`` (end timestamp) and
        ``_label`` (socket identifier); summary rows use the summary key as
        their label. Omitted entries are skipped.

        Returns:
            A 2-tuple (df_streams, df_summaries).
        """
        intervals = data.get("intervals", [])
        stream_rows: list[dict[str, Any]] = []
        sum_rows: list[dict[str, Any]] = []

        for it in intervals:
            for s in it.get("streams", []):
                if s.get("omitted", False):
                    continue
                row = dict(s)
                row["_t"] = s.get("end", s.get("start"))
                row["_label"] = f"Socket {s.get('socket', '?')}"
                stream_rows.append(row)
            for sum_key in (
                "sum",
                "sum_sent",
                "sum_received",
                "sum_bidir",
                "sum_bidir_reverse",
            ):
                if sum_key in it:
                    entry = dict(it[sum_key])
                    if entry.get("omitted", False):
                        continue
                    entry["_t"] = entry.get("end", entry.get("start"))
                    entry["_label"] = sum_key
                    sum_rows.append(entry)

        df_streams = pd.DataFrame(stream_rows) if stream_rows else pd.DataFrame()
        df_summaries = pd.DataFrame(sum_rows) if sum_rows else pd.DataFrame()
        return df_streams, df_summaries

    @staticmethod
    def _col_mean(df: pd.DataFrame, col: str, default: float = 0.0) -> float:
        if df.empty or col not in df.columns:
            return default
        s = df[col].dropna()
        return float(s.mean()) if not s.empty else default

    @staticmethod
    def _col_max(df: pd.DataFrame, col: str, default: float = 0.0) -> float:
        if df.empty or col not in df.columns:
            return default
        s = df[col].dropna()
        return float(s.max()) if not s.empty else default

    @staticmethod
    def _col_min(df: pd.DataFrame, col: str, default: float = 0.0) -> float:
        if df.empty or col not in df.columns:
            return default
        s = df[col].dropna()
        return float(s.min()) if not s.empty else default

    def _primary_df(self) -> pd.DataFrame:
        """Return the best available interval DataFrame for scalar aggregates."""
        return self.df_summaries if not self.df_summaries.empty else self.df_streams

    def _summary_series(self, label: str | None = None) -> pd.DataFrame | None:
        """Return one consistent interval summary series, if available."""
        if self.df_summaries.empty or "_label" not in self.df_summaries.columns:
            return None
        candidates = (
            (label,) if label is not None else ("sum", "sum_received", "sum_sent")
        )
        for candidate in candidates:
            series = self.df_summaries[self.df_summaries["_label"] == candidate]
            if not series.empty:
                return series
        return None

    def _bidir_reverse_series(self, series: pd.DataFrame | None) -> pd.DataFrame | None:
        """Return the reverse-direction interval series matching *series*."""
        if series is None or series.empty or "_label" not in series.columns:
            return None
        reverse_label = BIDIR_REVERSE_LABELS.get(str(series["_label"].iloc[0]))
        if reverse_label is None:
            return None
        return self._summary_series(reverse_label)

    def _series_throughput_mbps(self, series: pd.DataFrame | None) -> float | None:
        """Return the mean throughput of one interval series in Mbps."""
        if series is None or series.empty:
            return None
        if "bytes" in series.columns and "seconds" in series.columns:
            total_seconds = float(series["seconds"].sum())
            if total_seconds > 0:
                return float(series["bytes"].sum()) * 8.0 / total_seconds / BPS_TO_MBPS
        return self._col_mean(series, "bits_per_second") / BPS_TO_MBPS

    def _series_interval_rates(self, series: pd.DataFrame | None) -> list[float]:
        """Return one throughput value per reporting interval, in Mbps."""
        if series is None or series.empty:
            return []
        if "bytes" in series.columns and "seconds" in series.columns:
            seconds = series["seconds"].astype(float)
            byte_counts = series["bytes"].astype(float)
            rates = (byte_counts * 8.0 / seconds.where(seconds > 0)) / BPS_TO_MBPS
            return [float(value) for value in rates.dropna()]
        if "bits_per_second" in series.columns:
            return [
                float(value) / BPS_TO_MBPS
                for value in series["bits_per_second"].dropna()
            ]
        return []

    def _combined_interval_rates(self) -> list[float] | None:
        """Return per-interval forward+reverse rates for a bidirectional run."""
        series = self._summary_series()
        if series is None:
            return None
        reverse = self._bidir_reverse_series(series)
        if reverse is None:
            return None
        forward_rates = self._series_interval_rates(series)
        reverse_rates = self._series_interval_rates(reverse)
        if not forward_rates or len(forward_rates) != len(reverse_rates):
            return None
        return [
            forward + reverse_rate
            for forward, reverse_rate in zip(forward_rates, reverse_rates, strict=True)
        ]

    def avg_throughput_mbps(self) -> float:
        """Mean throughput in Mbps, aggregating both --bidir directions."""
        series = self._summary_series()
        throughput = self._series_throughput_mbps(series)
        if throughput is None:
            return self._col_mean(self._primary_df(), "bits_per_second") / BPS_TO_MBPS
        reverse = self._series_throughput_mbps(self._bidir_reverse_series(series))
        return throughput + (reverse or 0.0)

    def max_throughput_mbps(self) -> float:
        """Peak interval throughput in Mbps."""
        combined = self._combined_interval_rates()
        if combined:
            return max(combined)
        return self._col_max(self._primary_df(), "bits_per_second") / BPS_TO_MBPS

    def min_throughput_mbps(self) -> float:
        """Minimum interval throughput in Mbps."""
        combined = self._combined_interval_rates()
        if combined:
            return min(combined)
        return self._col_min(self._primary_df(), "bits_per_second") / BPS_TO_MBPS

    def pmtu(self) -> int:
        """Path MTU in bytes taken from the last stream interval."""
        if not self.df_streams.empty and "pmtu" in self.df_streams.columns:
            vals = self.df_streams["pmtu"].dropna()
            if not vals.empty:
                return int(vals.iloc[-1])
        return 0

    def avg_rtt_ms(self) -> float:
        """Mean TCP round-trip time across all stream intervals in ms."""
        return self._col_mean(self.df_streams, "rtt") / USEC_TO_MS

    def max_rtt_ms(self) -> float:
        """Maximum TCP round-trip time across all stream intervals in ms."""
        return self._col_max(self.df_streams, "rtt") / USEC_TO_MS

    def avg_rtt_var_us(self) -> float:
        """Mean TCP RTT variance in microseconds."""
        return self._col_mean(self.df_streams, "rttvar")

    def avg_cwnd_bytes(self) -> float:
        """Mean TCP send congestion window size in bytes."""
        return self._col_mean(self.df_streams, "snd_cwnd")

    def avg_snd_wnd_bytes(self) -> float:
        """Mean TCP send window size in bytes."""
        return self._col_mean(self.df_streams, "snd_wnd")

    def interval_dataframe(self) -> pd.DataFrame:
        """Return a copy of the per-stream interval DataFrame."""
        return self.df_streams.copy()

    def summary_dataframe(self) -> pd.DataFrame:
        """Return a copy of the per-interval summary DataFrame."""
        return self.df_summaries.copy()


__all__ = ["BIDIR_REVERSE_LABELS", "Iperf3IntervalData"]
