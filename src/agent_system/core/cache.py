"""
LRU cache for hot-path lookups in the memory graph.

Avoids repeated full-graph scans for common access patterns.
"""

import functools
import threading
from collections import OrderedDict
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class ThreadSafeLRU(OrderedDict):
    """
    Thread-safe LRU cache with a max size. Used to cache frequent
    graph queries (e.g. 'user X has access to tenant Y').

    Implementation note: OrderedDict.move_to_end gives us LRU semantics.
    We guard mutations with a lock for thread-safety.
    """

    def __init__(self, maxsize: int = 1024):
        super().__init__()
        self.maxsize = maxsize
        self._lock = threading.RLock()

    def __setitem__(self, key, value):
        with self._lock:
            if key in self:
                self.move_to_end(key)
            super().__setitem__(key, value)
            if len(self) > self.maxsize:
                oldest = next(iter(self))
                del self[oldest]

    def __getitem__(self, key):
        with self._lock:
            value = super().__getitem__(key)
            self.move_to_end(key)
            return value

    def clear(self):
        with self._lock:
            super().clear()


def lru_cache(maxsize: int = 128):
    """Decorator for LRU caching a function's result."""
    cache = ThreadSafeLRU(maxsize)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # Build a hashable key
            try:
                key = (args, tuple(sorted(kwargs.items())))
            except TypeError:
                return func(*args, **kwargs)  # Unhashable; just call
            with cache._lock:
                if key in cache:
                    cache.move_to_end(key)
                    return cache[key]
            result = func(*args, **kwargs)
            with cache._lock:
                cache[key] = result
            return result

        def cache_clear():
            with cache._lock:
                cache.clear()

        def cache_info():
            with cache._lock:
                return {
                    "size": len(cache),
                    "maxsize": cache.maxsize,
                }
        wrapper.clear = cache_clear
        wrapper.info = cache_info
        return wrapper
    return decorator
