"""Verify the traffic package does not eagerly load plotting dependencies."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def test_importing_traffic_does_not_load_pandas_or_matplotlib():
    code = (
        "import sys; import satgonetem.traffic; "
        "print('pandas' in sys.modules, 'matplotlib' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False False"
