"""Small, dependency-light metric writers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonlMetricsWriter:
    """Append JSON samples to a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, sample: dict[str, Any]) -> None:
        self._fh.write(json.dumps(sample, default=str, sort_keys=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> JsonlMetricsWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def write_json(path: str | Path, payload: Any) -> Path:
    """Write *payload* as pretty JSON and return the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return target
