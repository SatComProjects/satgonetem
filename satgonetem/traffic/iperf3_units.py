"""Unit conversion constants for iperf3 result parsing and presentation."""

from __future__ import annotations

BPS_TO_MBPS: float = 1_000_000.0
BYTES_TO_MB: float = 1_000_000.0
BYTES_TO_KB: float = 1_000.0
USEC_TO_MS: float = 1_000.0

__all__ = ["BPS_TO_MBPS", "BYTES_TO_KB", "BYTES_TO_MB", "USEC_TO_MS"]
