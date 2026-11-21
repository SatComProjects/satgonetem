"""Backward-compatible terminal bridge module path."""

from satgonetem.cli.node_terminal_bridge import (
    _find_python as _find_python,
)
from satgonetem.cli.node_terminal_bridge import (
    _log_error as _log_error,
)
from satgonetem.cli.node_terminal_bridge import (
    main,
)

__all__ = ["_find_python", "_log_error", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
