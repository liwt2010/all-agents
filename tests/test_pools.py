"""
Tests: Async batching + connection pooling
"""

import asyncio
import time
import pytest

from agent_system.core.pools import (
    AsyncTaskPool, LLMRateLimiter,
    get_task_pool, get_llm_rate_limiter, reset_pools,
)


class TestAsyncTaskPool:
    @pytest.mark.asyncio
    async def test_runs_tasks(self):
        pool = AsyncTaskPool(max_workers=4)
        async def f(i):
            return i * 2
        results = await pool.map(f, [1, 2, 3, 4])
        clean = [r for r in results if not isinstance(r, Exception)]
        assert sorted(clean) == [2, 4, 6, 8]

    @pytest.mark.asyncio
    async def test_concurrency_limit(self):
        pool = AsyncTaskPool(max_workers=2)
        active = 0
        peak = 0

        async def task():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return 1

        results = await pool.map(lambda _: task(), list(range(10)))
        assert peak <= 2

    @pytest.mark.asyncio
    async def test_returns_exceptions(self):
        pool = AsyncTaskPool(max_workers=4)
        async def good():
            return 1
        async def bad():
            raise ValueError("oops")
        async def runner(i):
            if i == 0:
                return await good()
            return await bad()
        results = await pool.map(runner, [0, 1, 2, 3])
        assert results[0] == 1
        assert isinstance(results[1], ValueError)

    def test_stats(self):
        pool = AsyncTaskPool(max_workers=4)
        s = pool.stats()
        assert s["max_workers"] == 4
        assert s["active"] == 0


class TestLLMRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_succeeds_immediately_first_time(self):
        rl = LLMRateLimiter(rate_per_minute=6000)
        await rl.acquire()

    @pytest.mark.asyncio
    async def test_rate_limit_slows_consecutive_calls(self):
        rl = LLMRateLimiter(rate_per_minute=120)
        t0 = time.monotonic()
        await rl.acquire()
        t1 = time.monotonic()
        await rl.acquire()
        t2 = time.monotonic()
        assert t2 - t1 >= 0.4
        assert t1 - t0 < 0.1


class TestPoolSingletons:
    def setup_method(self):
        reset_pools()

    def teardown_method(self):
        reset_pools()

    def test_singleton_task_pool(self):
        p1 = get_task_pool()
        p2 = get_task_pool()
        assert p1 is p2

    def test_singleton_llm_limiter(self):
        l1 = get_llm_rate_limiter()
        l2 = get_llm_rate_limiter()
        assert l1 is l2
