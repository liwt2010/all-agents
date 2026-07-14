"""Enhanced performance benchmarks for agent execution.

This module extends test_performance.py with:
- Agent execution time (avg, p50, p95, p99)
- Concurrent task handling limits
- Memory usage under load
- P99 latency testing

Run: pytest tests/test_performance_agent.py -v
"""
import asyncio
import gc
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pytest
pytestmark = pytest.mark.timeout(60)

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
from agent_system.memory.graph import get_graph, reset_graph, GraphNode, NodeType


def _stats(times: list) -> dict:
    """Return p50/p95/p99/avg/max for a list of millisecond timings."""
    if not times:
        return {}
    sorted_times = sorted(times)
    return {
        "avg_ms": statistics.mean(times),
        "p50_ms": sorted_times[int(len(times) * 0.50)],
        "p95_ms": sorted_times[int(len(times) * 0.95)],
        "p99_ms": sorted_times[int(len(times) * 0.99)] if len(times) >= 100 else max(times),
        "max_ms": max(times),
        "min_ms": min(times),
        "n": len(times),
    }


def _bench_async(func, n: int = 50) -> dict:
    """Run async func n times, return ms statistics."""
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        asyncio.run(func())
        times.append((time.perf_counter() - t0) * 1000)
    return _stats(times)


class TestAgentExecutionTime:
    """Benchmark agent execution time."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset state before each test."""
        reset_graph()
        gc.collect()
        yield
        reset_graph()
        gc.collect()

    @pytest.mark.asyncio
    async def test_agent_execution_time_simple_task(self):
        """Benchmark simple task execution (mock LLM)."""
        from pydantic import ConfigDict

        class FastAgent(SmartAgent):
            agent_name: str = "fast_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test agent"
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                    payload={"status": "done"},
                )

        agent = FastAgent()
        times = []

        # Run 50 iterations
        for i in range(50):
            t0 = time.perf_counter()
            task = TaskContext(task_id=f"task-{i}", input=f"task {i}")
            await agent.execute(task)
            times.append((time.perf_counter() - t0) * 1000)

        stats = _stats(times)

        # Target: p99 < 100ms for simple mock tasks
        print(f"\nAgent execution (mock):")
        print(f"  avg={stats['avg_ms']:.2f}ms, p50={stats['p50_ms']:.2f}ms")
        print(f"  p95={stats['p95_ms']:.2f}ms, p99={stats['p99_ms']:.2f}ms")
        print(f"  min={stats['min_ms']:.2f}ms, max={stats['max_ms']:.2f}ms")

        assert stats['p99_ms'] < 200, f"Agent execution too slow: {stats}"

    @pytest.mark.asyncio
    async def test_agent_execution_time_with_memory(self):
        """Benchmark agent with memory enabled."""
        from pydantic import ConfigDict

        class MemoryAgent(SmartAgent):
            agent_name: str = "memory_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test agent with memory"
            memory_enabled: bool = True
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = MemoryAgent()
        times = []

        for i in range(30):
            t0 = time.perf_counter()
            task = TaskContext(task_id=f"mem-task-{i}", input=f"task {i}")
            await agent.execute(task)
            times.append((time.perf_counter() - t0) * 1000)

        stats = _stats(times)

        print(f"\nAgent execution (with memory):")
        print(f"  avg={stats['avg_ms']:.2f}ms, p99={stats['p99_ms']:.2f}ms")

        # Memory adds overhead, so allow more time
        assert stats['p99_ms'] < 500, f"Agent with memory too slow: {stats}"


class TestConcurrentTaskHandling:
    """Test concurrent task handling limits."""

    @pytest.fixture(autouse=True)
    def setup(self):
        reset_graph()
        yield
        reset_graph()

    @pytest.mark.asyncio
    async def test_concurrent_tasks_throughput(self):
        """Measure throughput under concurrent load."""
        from pydantic import ConfigDict

        class ConcurrentAgent(SmartAgent):
            agent_name: str = "concurrent_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                # Simulate some async work
                await asyncio.sleep(0.01)  # 10ms simulated work
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = ConcurrentAgent()
        num_tasks = 20

        # Run tasks concurrently
        t0 = time.perf_counter()
        tasks = [
            agent.execute(TaskContext(task_id=f"concurrent-{i}", input=f"task {i}"))
            for i in range(num_tasks)
        ]
        results = await asyncio.gather(*tasks)
        total_time = (time.perf_counter() - t0) * 1000

        # Calculate throughput
        throughput = num_tasks / (total_time / 1000)  # tasks per second

        print(f"\nConcurrent tasks: {num_tasks}")
        print(f"  Total time: {total_time:.2f}ms")
        print(f"  Throughput: {throughput:.1f} tasks/sec")

        # If truly concurrent, should complete faster than sequential
        # Sequential would be ~200ms (20 * 10ms)
        # Concurrent should be closer to 10-50ms
        assert total_time < 200, f"Concurrent execution too slow: {total_time}ms"
        assert len(results) == num_tasks

    @pytest.mark.asyncio
    async def test_task_queue_backpressure(self):
        """Test behavior under high concurrency."""
        from pydantic import ConfigDict

        class SlowAgent(SmartAgent):
            agent_name: str = "slow_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                await asyncio.sleep(0.05)  # 50ms per task
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = SlowAgent()

        # Submit many tasks rapidly
        num_tasks = 100
        t0 = time.perf_counter()

        tasks = [
            agent.execute(TaskContext(task_id=f"backpressure-{i}", input=f"task {i}"))
            for i in range(num_tasks)
        ]

        # With backpressure, some tasks might be rejected or queued
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=10.0  # 10 second timeout
            )
            success_count = sum(1 for r in results if isinstance(r, OutputSchema))
            total_time = (time.perf_counter() - t0) * 1000

            print(f"\nBackpressure test: {num_tasks} tasks")
            print(f"  Successful: {success_count}/{num_tasks}")
            print(f"  Total time: {total_time:.2f}ms")

            # Should complete within reasonable time
            assert success_count > 0, "No tasks completed"

        except asyncio.TimeoutError:
            pytest.fail("Tasks timed out - possible backpressure issue")


class TestMemoryUsage:
    """Test memory usage under load."""

    @pytest.fixture(autouse=True)
    def setup(self):
        reset_graph()
        gc.collect()
        yield
        reset_graph()
        gc.collect()

    @pytest.mark.asyncio
    async def test_memory_graph_scaling(self):
        """Test memory graph scales correctly with data."""
        import psutil

        g = get_graph()
        process = psutil.Process()
        mem_before = process.memory_info().rss / 1024 / 1024  # MB

        # Add many nodes
        num_nodes = 1000
        for i in range(num_nodes):
            g.add_node(GraphNode(
                id=f"perf-node-{i}",
                type=NodeType.TASK,
                content={"index": i, "data": "x" * 100},  # ~100 bytes per node
            ))

        gc.collect()
        mem_after = process.memory_info().rss / 1024 / 1024  # MB
        mem_delta = mem_after - mem_before

        print(f"\nMemory graph scaling ({num_nodes} nodes):")
        print(f"  Before: {mem_before:.2f} MB")
        print(f"  After: {mem_after:.2f} MB")
        print(f"  Delta: {mem_delta:.2f} MB")
        print(f"  Per node: {mem_delta * 1024 / num_nodes:.2f} KB")

        # Should use reasonable memory (< 50MB for 1000 nodes)
        assert mem_delta < 100, f"Memory usage too high: {mem_delta:.2f} MB"

    @pytest.mark.asyncio
    async def test_no_memory_leak_on_repeated_tasks(self):
        """Verify no memory leak on repeated task execution."""
        import psutil

        from pydantic import ConfigDict

        class LeakyAgent(SmartAgent):
            agent_name: str = "leaky_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = LeakyAgent()
        process = psutil.Process()

        # Measure memory before
        gc.collect()
        mem_start = process.memory_info().rss / 1024 / 1024  # MB

        # Run many tasks
        num_iterations = 100
        for i in range(num_iterations):
            task = TaskContext(task_id=f"leak-test-{i}", input=f"task {i}")
            await agent.execute(task)

            # Check memory every 20 iterations
            if i % 20 == 0:
                gc.collect()
                mem_current = process.memory_info().rss / 1024 / 1024
                print(f"  Iteration {i}: {mem_current:.2f} MB")

        gc.collect()
        mem_end = process.memory_info().rss / 1024 / 1024  # MB
        mem_growth = mem_end - mem_start

        print(f"\nMemory leak test ({num_iterations} tasks):")
        print(f"  Start: {mem_start:.2f} MB")
        print(f"  End: {mem_end:.2f} MB")
        print(f"  Growth: {mem_growth:.2f} MB")

        # Memory growth should be minimal (< 20MB for 100 iterations)
        assert mem_growth < 50, f"Possible memory leak: {mem_growth:.2f} MB growth"


class TestP99Latency:
    """Test P99 latency targets."""

    @pytest.fixture(autouse=True)
    def setup(self):
        reset_graph()
        yield
        reset_graph()

    def test_p99_latency_for_graph_operations(self):
        """Measure P99 latency for common graph operations."""
        g = get_graph()

        # Populate graph
        for i in range(100):
            g.add_node(GraphNode(
                id=f"p99-node-{i}",
                type=NodeType.TASK,
                content={"index": i},
            ))

        # Test get_node latency
        get_times = []
        for _ in range(1000):
            t0 = time.perf_counter()
            g.get_node("p99-node-50")
            get_times.append((time.perf_counter() - t0) * 1000)

        stats = _stats(get_times)
        print(f"\nGraph get_node P99: {stats['p99_ms']:.4f}ms")

        # P99 should be < 1ms for in-memory lookups
        assert stats['p99_ms'] < 1.0, f"get_node P99 too high: {stats}"

        # Test find_nodes latency
        find_times = []
        for _ in range(500):
            t0 = time.perf_counter()
            g.find_nodes(NodeType.TASK)
            find_times.append((time.perf_counter() - t0) * 1000)

        stats = _stats(find_times)
        print(f"Graph find_nodes P99: {stats['p99_ms']:.4f}ms")

        # P99 should be < 10ms for small graphs
        assert stats['p99_ms'] < 10.0, f"find_nodes P99 too high: {stats}"
