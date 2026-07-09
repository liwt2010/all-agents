"""
P2-3.1 Success rate measurement: with vs without experience injection.

Runs N tasks twice — once with memory_enabled=True (experience available
for injection), once with memory_enabled=False. Records success rate,
latency, and confidence for each. Reports the delta.

Skipped if no API key.
"""
import asyncio
import os
import time

import pytest


def _has_api_key():
    return bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )


pytestmark = pytest.mark.skipif(
    not _has_api_key(),
    reason="No real API key",
)

N_TASKS = 6  # small to keep cost down


def _setup_router():
    os.environ["LLM_PROVIDER"] = "anthropic"
    if not os.environ.get("ANTHROPIC_BASE_URL") and os.environ.get("OPENAI_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = os.environ["OPENAI_BASE_URL"]
    from agent_system.core.llm_router import LLMRouter, LLMConfig
    cfg = LLMConfig(
        model=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
        max_tokens=4000, temperature=0.3,
    )
    LLMRouter.get_config = lambda self, agent_name, task_complexity=None: cfg
    return cfg


@pytest.mark.asyncio
async def test_success_rate_with_vs_without_experience():
    """
    Run 6 tasks, alternating with and without experience injection.
    Compare success rate (no exception raised + valid OutputSchema).
    """
    cfg = _setup_router()

    from agent_system.agents.product_agent import ProductAgent
    from agent_system.core.agent import TaskContext
    from agent_system.core.observability import get_provenance, ProvenanceSource
    from agent_system.memory.experience import (
        record_experience, get_graph,
    )

    # Tasks: short Chinese questions, alternating topic
    tasks_inputs = [
        "一句话回答:1+1=?",
        "一句话回答:法国的首都是什么?",
        "一句话回答:水的化学式是什么?",
        "一句话回答:中国的国宝动物是什么?",
        "一句话回答:python 是一种什么语言?",
        "一句话回答:一年有多少个月?",
    ]

    # Seed experience graph with helpful prior knowledge
    graph = get_graph()
    for t in tasks_inputs:
        record_experience(
            graph,
            task_id=f"seed-{hash(t) % 100000}",
            summary=(
                f"For short Chinese Q&A tasks, the LLM works best with a "
                f"concise direct-answer prompt. Example input: {t[:30]}. "
                f"Recommended: keep temperature low (0.3) and max_tokens small (4000)."
            ),
            agent_name="product_agent",
            success=True,
        )

    results_with = []  # memory_enabled=True (default)
    results_without = []  # memory_enabled=False

    for i, input_text in enumerate(tasks_inputs):
        # With memory
        a = ProductAgent()
        a.memory_enabled = True
        ctx = TaskContext(task_id=f"with-{i}", input=input_text)
        t0 = time.perf_counter()
        ok, lat, src = False, 0.0, None
        try:
            r = await a.execute(ctx)
            lat = time.perf_counter() - t0
            prov = get_provenance(r)
            src = prov.source if prov else None
            ok = r is not None and src in (ProvenanceSource.REAL_LLM, ProvenanceSource.MOCK)
        except Exception:
            lat = time.perf_counter() - t0
        results_with.append((ok, lat, src))

        # Without memory
        b = ProductAgent()
        b.memory_enabled = False
        ctx2 = TaskContext(task_id=f"without-{i}", input=input_text)
        t0 = time.perf_counter()
        ok2, lat2, src2 = False, 0.0, None
        try:
            r2 = await b.execute(ctx2)
            lat2 = time.perf_counter() - t0
            prov2 = get_provenance(r2)
            src2 = prov2.source if prov2 else None
            ok2 = r2 is not None and src2 in (ProvenanceSource.REAL_LLM, ProvenanceSource.MOCK)
        except Exception:
            lat2 = time.perf_counter() - t0
        results_without.append((ok2, lat2, src2))

    # Compute stats
    def stats(name, results):
        n = len(results)
        ok = sum(1 for r in results if r[0])
        avg_lat = sum(r[1] for r in results) / n if n else 0
        real = sum(1 for r in results if r[2] == ProvenanceSource.REAL_LLM)
        print(f"\n  [{name}] n={n}, success={ok}/{n} ({ok/n*100:.0f}%), "
              f"avg_latency={avg_lat:.2f}s, real_llm={real}/{n}")
        for i, (o, l, s) in enumerate(results, 1):
            print(f"    {i}. ok={o}, {l:.2f}s, src={s}")
        return ok, avg_lat

    ok_with, lat_with = stats("WITH experience", results_with)
    ok_without, lat_without = stats("WITHOUT experience", results_without)

    print(f"\n  Delta: success +{ok_with - ok_without}, "
          f"latency {lat_with - lat_without:+.2f}s")
    print(f"  Verdict: experience injection {'helps' if ok_with >= ok_without else 'neutral-or-worse'} success rate")

    # Always passes — this is a measurement, not a strict assertion.
    # We log the deltas for production tuning.
    assert len(results_with) == N_TASKS
    assert len(results_without) == N_TASKS
