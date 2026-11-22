"""Small, dependency-light metric writers."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
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
    """Write *payload* as pretty JSON and return the path.

    The payload is serialized before the target is touched and written to a
    temporary file that replaces the target atomically, so a failure cannot
    leave a truncated artifact behind.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, default=str) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=str(target.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return target
