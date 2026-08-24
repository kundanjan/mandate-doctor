"""EventBus unit tests."""

from __future__ import annotations

import asyncio

from mandate_doctor.api.events import EventBus, PrintSink


async def test_publish_reaches_subscriber() -> None:
    bus = EventBus()
    _client_id, queue = await bus.subscribe()
    await bus.publish({"type": "step", "node": "order", "status": "ok"})
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event["type"] == "step"
    assert event["node"] == "order"
    assert "ts" in event


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    client_id, queue = await bus.subscribe()
    await bus.unsubscribe(client_id)
    await bus.publish({"type": "webhook", "event_type": "payment.captured"})
    assert queue.empty()
    assert bus.subscriber_count == 0


async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = EventBus()
    await bus.publish({"type": "batch_end"})  # must not raise
    assert bus.subscriber_count == 0


async def test_print_sink_does_not_raise() -> None:
    sink = PrintSink()
    await sink.publish({"type": "batch_start"})  # must not raise


async def test_slow_subscriber_gets_dropped_not_crash() -> None:
    bus = EventBus(max_queue=1)
    _client_id, queue = await bus.subscribe()
    await bus.publish({"type": "a"})
    await bus.publish({"type": "b"})  # overflows — dropped silently
    assert queue.qsize() == 1
