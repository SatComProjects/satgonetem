"""Tests for the default external-boundary adapters."""

from __future__ import annotations

import sys

import pytest

from satgonetem.ports import SubprocessRunner

pytestmark = pytest.mark.unit


def test_subprocess_runner_returns_captured_output():
    result = SubprocessRunner().run(
        [sys.executable, "-c", "print('ok')"],
    )

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.stderr == ""


def test_subprocess_runner_honours_input_and_check():
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
        input="hello\n",
        check=True,
    )

    assert result.stdout == "hello\n"
