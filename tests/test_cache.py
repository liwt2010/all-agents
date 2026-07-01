"""
Tests: LRU cache
"""

import pytest
import threading
import time
from agent_system.core.cache import ThreadSafeLRU, lru_cache


class TestThreadSafeLRU:
    def test_basic(self):
        cache = ThreadSafeLRU(maxsize=2)
        cache["a"] = 1
        cache["b"] = 2
        assert cache["a"] == 1
        assert cache["b"] == 2

    def test_eviction(self):
        cache = ThreadSafeLRU(maxsize=2)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3  # Should evict "a"
        assert "a" not in cache
        assert "b" in cache
        assert "c" in cache

    def test_lru_ordering(self):
        cache = ThreadSafeLRU(maxsize=2)
        cache["a"] = 1
        cache["b"] = 2
        # Access "a" to make it most-recently-used
        _ = cache["a"]
        cache["c"] = 3  # Should evict "b" now, not "a"
        assert "a" in cache
        assert "b" not in cache
        assert "c" in cache

    def test_concurrent(self):
        cache = ThreadSafeLRU(maxsize=100)
        errors = []

        def worker(start):
            try:
                for i in range(start, start + 10):
                    cache[f"k{i}"] = i
                    _ = cache.get(f"k{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(i,))
            for i in range(0, 500, 100)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestLRUCacheDecorator:
    def test_caches_result(self):
        @lru_cache(maxsize=10)
        def add(a, b):
            return a + b

        assert add(1, 2) == 3
        assert add(1, 2) == 3
        assert add.info()["size"] == 1

    def test_different_args_different_cache(self):
        @lru_cache(maxsize=10)
        def fn(x):
            return x * 2

        assert fn(1) == 2
        assert fn(2) == 4
        assert fn.info()["size"] == 2

    def test_cache_clear(self):
        @lru_cache(maxsize=10)
        def fn(x):
            return x * 2

        fn(1)
        assert fn.info()["size"] == 1
        fn.clear()
        assert fn.info()["size"] == 0

    def test_maxsize_eviction(self):
        @lru_cache(maxsize=2)
        def fn(x):
            return x

        fn(1)
        fn(2)
        fn(3)  # evicts 1
        assert fn.info()["size"] == 2
        # But fn(4) evicts 2; etc.
        fn(4)
        assert fn.info()["size"] == 2
