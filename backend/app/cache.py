import asyncio
import copy
import time

# A (Time-to-live) TTL cache to reduce database reads
class TTLCache:

    def __init__(self):
        self._entries: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str):
        async with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None

            expires_at, value = entry
            if expires_at <= time.time():
                self._entries.pop(key, None)
                return None

            # Return a copy so callers cannot mutate cached state by accident.
            return copy.deepcopy(value)

    async def set(self, key: str, value, ttl_seconds: int) -> None:
        async with self._lock:
            self._entries[key] = (
                time.time() + ttl_seconds,
                copy.deepcopy(value),
            )

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    async def invalidate_prefix(self, prefix: str) -> None:
        async with self._lock:
            keys = [key for key in self._entries if key.startswith(prefix)]
            for key in keys:
                self._entries.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()


query_cache = TTLCache()
