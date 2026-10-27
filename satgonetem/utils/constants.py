"""Shared constants for satgonetem."""

import os

MAX_WORKERS = os.cpu_count() or 4

# Route installation is process/namespace-bound and benefits from more
# concurrency than the generic CPU-count pool. Keep the upper bound moderate
# so a large constellation does not spawn an unbounded number of nsenter
# processes at once.
ROUTE_APPLY_WORKERS = max(MAX_WORKERS, min(64, (os.cpu_count() or 4) * 2))
