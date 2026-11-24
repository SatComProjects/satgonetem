"""Tests for the shared ``ThreadedFlow`` lifecycle."""

from __future__ import annotations

import enum
from typing import Any
from unittest.mock import MagicMock

import pytest

from satgonetem.traffic.flow import ThreadedFlow

pytestmark = pytest.mark.unit


class _Status(enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class _RecordingFlow(ThreadedFlow):
    """Flow that records whether its traffic action ran."""

    STATUS = _Status
    flow_name = "RecordingFlow"

    def __init__(self, delay: float) -> None:
        super().__init__(MagicMock(), MagicMock(), MagicMock(), delay=delay)
        self.executed = False
        self.stop_calls = 0

    def _execute(self) -> Any:
        self.executed = True
        return "ran"

    def _stop_process(self) -> None:
        self.stop_calls += 1


def test_flow_stopped_during_delay_does_not_execute() -> None:
    """Ping/Hping3-style flows must not wake up or kill sibling processes during delay."""
    flow = _RecordingFlow(delay=60.0)

    flow.start()
    flow.stop()

    assert flow.status() == _Status.ERROR
    assert flow.executed is False, "stopped flow still ran its traffic action"
    assert flow.stop_calls == 0
    assert flow.is_alive() is False
    assert flow._error is not None
    assert "stopped before it could start" in str(flow._error)


def test_flow_stopped_after_entering_execute_calls_stop_process() -> None:
    """Stopping an actively executing flow must invoke _stop_process()."""
    import threading

    entered = threading.Event()
    release = threading.Event()

    class _BlockingFlow(_RecordingFlow):
        def _execute(self) -> Any:
            self.executed = True
            entered.set()
            release.wait(timeout=5.0)
            return "ran"

        def _stop_process(self) -> None:
            super()._stop_process()
            release.set()

    flow = _BlockingFlow(delay=0.0)
    flow.start()
    assert entered.wait(timeout=2.0)

    flow.stop()

    assert flow.executed is True
    assert flow.stop_calls == 1
    assert flow.is_alive() is False
