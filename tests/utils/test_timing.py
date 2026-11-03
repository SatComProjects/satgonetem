"""Tests for the stage timing helpers used by update_simulation()."""

from __future__ import annotations

import pytest

from satgonetem.utils.timing import StageTimings, measure_stage


def test_record_accumulates_and_clamps_negative_values():
    timings = StageTimings()
    timings.record("tick", 0.1)
    timings.record("tick", 0.2)
    timings.record("tick", -5.0)

    assert timings.as_dict()["tick"] == pytest.approx(0.3)


def test_measure_records_elapsed_time():
    timings = StageTimings()
    with timings.measure("work"):
        pass

    assert "work" in timings.as_dict()
    assert timings.as_dict()["work"] >= 0.0


def test_as_dict_returns_a_copy():
    timings = StageTimings()
    timings.record("work", 0.5)

    snapshot = timings.as_dict()
    snapshot["work"] = 99.0

    assert timings.as_dict()["work"] == 0.5


def test_reset_drops_all_stages():
    timings = StageTimings()
    timings.record("work", 0.5)
    timings.reset()

    assert timings.as_dict() == {}


def test_measure_stage_is_a_no_op_without_timer():
    class Harness:
        pass

    with measure_stage(Harness(), "work"):
        pass


def test_measure_stage_uses_owner_timer():
    class Harness:
        def __init__(self) -> None:
            self.stage_timings = StageTimings()

    harness = Harness()
    with measure_stage(harness, "work"):
        pass

    assert "work" in harness.stage_timings.as_dict()
