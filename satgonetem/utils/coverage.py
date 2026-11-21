"""Backward-compatible satellite coverage helpers."""

from satgonetem.utils.satellite_coverage import (
    coverage_percentage,
    coverage_percentage_fast,
    coverage_ring_from_elevation,
)

__all__ = [
    "coverage_ring_from_elevation",
    "coverage_percentage",
    "coverage_percentage_fast",
]
