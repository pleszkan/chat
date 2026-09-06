import asyncio

import pytest

from app.adapters.event_broker import GenerationEventBroker


def test_subscriber_receives_event_published_before_queue_consumption():
    broker = GenerationEventBroker()

    queue = broker.subscribe("generation-1")
    broker.publish("generation-1", {"event": "completed"})

    assert queue.get_nowait() == {"event": "completed"}
    broker.unsubscribe("generation-1", queue)
    broker.publish("generation-1", {"event": "failed"})
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()
