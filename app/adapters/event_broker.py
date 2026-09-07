import asyncio


class GenerationEventBroker:
    def __init__(self) -> None:
        self.subscribers: dict[str, set[asyncio.Queue[dict]]] = {}

    def subscribe(self, generation_id: str) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self.subscribers.setdefault(generation_id, set()).add(queue)
        return queue

    def unsubscribe(self, generation_id: str, queue: asyncio.Queue[dict]) -> None:
        subscribers = self.subscribers.get(generation_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self.subscribers.pop(generation_id, None)

    def publish(self, generation_id: str, event: dict) -> None:
        for queue in self.subscribers.get(generation_id, set()):
            queue.put_nowait(event)
