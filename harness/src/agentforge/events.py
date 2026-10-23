import asyncio


class EventBus:
    def __init__(self, runtime=None):
        self.runtime = runtime
        self.events: list[dict] = []
        self.condition = asyncio.Condition()

    async def emit(self, event: dict):
        async with self.condition:
            self.events.append(event.copy())
            self.condition.notify_all()

    async def stream(self, after: int = 0):
        cursor = after
        while True:
            async with self.condition:
                await self.condition.wait_for(lambda: len(self.events) > cursor)
                batch = self.events[cursor:]
            for event in batch:
                cursor += 1
                yield event
                if event["type"] in {"done", "error"}:
                    return
