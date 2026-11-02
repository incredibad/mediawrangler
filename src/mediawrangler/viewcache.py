"""Built comparison views, reused until the data changes.

Building a view reads every item, file or watch row for a group (up to about
half a second for a large library), so a built view is kept until `version`
moves. The app bumps the version after every sync and every change made
through the UI. Cached views are shared between requests and never mutated.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any


class ViewCache:
    def __init__(self, max_entries: int = 96):
        self._entries: OrderedDict[Hashable, tuple[int, Any]] = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    def get(self, key: Hashable, version: int, build: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and hit[0] == version:
                self._entries.move_to_end(key)
                return hit[1]
        view = build()
        with self._lock:
            self._entries[key] = (version, view)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return view
