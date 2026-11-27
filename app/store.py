"""In-memory store for generated GPX files (download links)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class StoredGpx:
    filename: str
    content: str
    created: float


class GpxStore:
    def __init__(self, ttl_seconds: int, max_entries: int):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._items: OrderedDict[str, StoredGpx] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, route_id: str, filename: str, content: str) -> None:
        with self._lock:
            self._purge()
            self._items[route_id] = StoredGpx(filename, content, time.time())
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def get(self, route_id: str) -> StoredGpx | None:
        with self._lock:
            self._purge()
            return self._items.get(route_id)

    def _purge(self) -> None:
        cutoff = time.time() - self.ttl
        while self._items:
            key, item = next(iter(self._items.items()))
            if item.created >= cutoff:
                break
            del self._items[key]
