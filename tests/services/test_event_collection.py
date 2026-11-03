"""Tests for the V5 event collector and link-delta coalescing."""

from __future__ import annotations

import asyncio
from collections import deque

from satgonetem.services.event_collection import EventCollector, LinkDelta

KEY = frozenset(("Sat0", "Sat1"))
OTHER_KEY = frozenset(("Sat2", "Sat3"))


def test_collector_drains_in_arrival_order():
    collector = EventCollector()

    async def collect():
        await collector.handle("first")
        await collector.handle("second")

    asyncio.run(collect())

    assert collector.drain() == ["first", "second"]
    assert collector.drain() == []


def test_collector_len_tracks_pending_events():
    collector = EventCollector()

    async def collect():
        await collector.handle("event")

    asyncio.run(collect())

    assert len(collector) == 1


def test_delta_created_for_new_link():
    delta = LinkDelta()
    delta.record(KEY, "created", "created")

    result = delta.resolve(existing_keys=set())

    assert result.created == {KEY: "created"}
    assert result.updated == {}
    assert result.removed == {}


def test_delta_created_for_existing_link_is_an_update():
    delta = LinkDelta()
    delta.record(KEY, "created", "recreated")

    result = delta.resolve(existing_keys={KEY})

    assert result.updated == {KEY: "recreated"}
    assert result.created == {}


def test_delta_removed_for_existing_link():
    delta = LinkDelta()
    delta.record(KEY, "removed", "removed")

    result = delta.resolve(existing_keys={KEY})

    assert result.removed == {KEY: "removed"}


def test_delta_removed_for_unknown_link_is_ignored():
    delta = LinkDelta()
    delta.record(KEY, "removed", "removed")

    result = delta.resolve(existing_keys=set())

    assert result.removed == {}
    assert result.ignored == 1


def test_delta_created_then_removed_new_link_is_ignored():
    delta = LinkDelta()
    delta.record(KEY, "created", "created")
    delta.record(KEY, "removed", "removed")

    result = delta.resolve(existing_keys=set())

    assert result.created == {}
    assert result.removed == {}
    assert result.ignored == 1


def test_delta_removed_then_created_existing_link_is_an_update():
    delta = LinkDelta()
    delta.record(KEY, "removed", "removed")
    delta.record(KEY, "created", "recreated")

    result = delta.resolve(existing_keys={KEY})

    assert result.created == {}
    assert result.removed == {}
    assert result.updated == {KEY: "recreated"}


def test_delta_last_event_per_key_wins_and_deduplicates():
    delta = LinkDelta()
    delta.record(KEY, "created", "first")
    delta.record(KEY, "created", "last")
    delta.record(OTHER_KEY, "created", "other")

    result = delta.resolve(existing_keys=set())

    assert result.created == {KEY: "last", OTHER_KEY: "other"}
    assert len(delta) == 2


def test_collector_swallows_handler_failures_and_counts_dropped():
    collector = EventCollector()

    class BrokenQueue(deque):
        def append(self, event):
            raise RuntimeError("queue unavailable")

    collector._events = BrokenQueue()

    asyncio.run(collector.handle("event"))

    assert collector.dropped == 1
    assert collector.drain() == []
