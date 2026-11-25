"""DES002 R2: pending revocations take priority over queued senders."""
import asyncio
from contextlib import asynccontextmanager
from collections import defaultdict


class Gate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.tokens = set()
        self.clear = asyncio.Event()
        self.clear.set()

    @asynccontextmanager
    async def work(self):
        while True:
            await self.clear.wait()
            await self.lock.acquire()
            if not self.tokens:
                break
            self.lock.release()
        try:
            yield
        finally:
            self.lock.release()

    def revoke(self, action):
        token = object()
        self.tokens.add(token)
        self.clear.clear()

        async def supervised():
            try:
                async with self.lock:
                    return action()
            finally:
                self.tokens.remove(token)
                if not self.tokens:
                    self.clear.set()
        return asyncio.create_task(supervised())

    async def deliver(self, verify, send, message):
        if self.tokens:
            return False
        async with self.lock:
            if self.tokens or not verify():
                return False
            # Cancelled send cannot outlive this gate; ASGI writer cancellation propagates.
            async with asyncio.timeout(1):
                await send(message)
            return True


class Gates(defaultdict):
    def __init__(self):
        super().__init__(Gate)
