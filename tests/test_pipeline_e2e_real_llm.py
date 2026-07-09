"""
P1: Real-LLM end-to-end pipeline test.

Runs the CEO Agent's _run_pipeline which chains:
  Product Agent -> PRD
  Tech Agent    -> code (uses PRD as upstream)
  Test Agent    -> tests (uses code as upstream)
  Deploy Agent  -> deploy plan (uses tests as upstream)

Each step uses a REAL LLM API call. Skipped if no API key is set.

Run:
    OPENAI_API_KEY=sk-... ANTHROPIC_API_KEY=sk-... \\
    PYTHONPATH=src .venv/Scripts/python.exe -m pytest \\
    tests/test_pipeline_e2e_real_llm.py -v -s
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
    reason="No real API key (set ANTHROPIC_API_KEY or OPENAI_API_KEY to run)",
)


@pytest.mark.asyncio
async def test_pipeline_product_tech_test_real_llm():
    """Run the full Product -> Tech -> Test -> Deploy pipeline with real LLM.

    Each step calls the real LLM API (via the configured provider). Verifies
    that the handoff between agents works: each agent receives the previous
    agent's OutputSchema as upstream_output and produces a valid result.
    """
    # Force anthropic provider (works around the proxy's OpenAI-SDK issue)
    os.environ["LLM_PROVIDER"] = "anthropic"
    if not os.environ.get("ANTHROPIC_BASE_URL") and os.environ.get("OPENAI_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = os.environ["OPENAI_BASE_URL"]

    # Bump max_tokens: deepseek-v4-pro reasoning eats ~1000 tokens; default
    # 4096 isn't enough for an actual structured reply.
    from agent_system.core.llm_router import LLMRouter, LLMConfig, router as default_router
    test_config = LLMConfig(
        model=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
        max_tokens=8000,
        temperature=0.5,
    )
    LLMRouter.get_config = lambda self, agent_name, task_complexity=None: test_config
    # Patch settings so all 4 pipeline agents pick up the model
    s = default_router.settings
    s.llm.default.model = test_config.model
    s.llm.default.max_tokens = test_config.max_tokens
    if hasattr(s.llm, "fast"):
        s.llm.fast.model = test_config.model
        s.llm.fast.max_tokens = test_config.max_tokens
    if hasattr(s.llm, "agents") and s.llm.agents:
        for cfg in s.llm.agents.values():
            if hasattr(cfg, "model"):
                cfg.model = test_config.model
            if hasattr(cfg, "max_tokens"):
                cfg.max_tokens = test_config.max_tokens

    from agent_system.agents.ceo_agent import CEOAgent
    from agent_system.core.agent import TaskContext

    ceo = CEOAgent()
    task = TaskContext(
        task_id="e2e-pipeline-real-1",
        input="极简登录功能(3个核心功能点)",
    )

    print(f"\n=== End-to-end pipeline (model={test_config.model}) ===")
    t0 = time.perf_counter()
    try:
        result = await ceo._run_pipeline(task)
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"=== PIPELINE FAILED in {elapsed:.2f}s ===")
        print(f"{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        pytest.fail(f"Pipeline raised: {e}")
    elapsed = time.perf_counter() - t0

    print(f"=== Pipeline completed in {elapsed:.2f}s ===")
    print(f"Result type: {type(result).__name__}")
    if hasattr(result, "payload") and result.payload:
        payload = result.payload
        if isinstance(payload, dict):
            print(f"Pipeline status: {payload.get('pipeline_status')}")
            summary = payload.get("summary", {})
            print(f"Summary: {summary}")
            print("Steps:")
            for i, step in enumerate(payload.get("steps", []), 1):
                agent = step.get("agent", "?")
                status = step.get("status", "?")
                err = step.get("error", "")
                print(f"  {i}. {agent:15s} {status:10s} {err[:80] if err else ''}")

    # The pipeline should have at least Product completed (the most basic test)
    payload = result.payload
    assert payload["pipeline_status"] == "completed", (
        f"Pipeline did not complete: {payload.get('error', '?')}; "
        f"steps: {[s.get('agent') + ':' + s.get('status') for s in payload.get('steps', [])]}"
    )
    completed_agents = [s["agent"] for s in payload["steps"] if s["status"] == "completed"]
    assert "product_agent" in completed_agents, "Product Agent must complete for any pipeline"
    # We accept the pipeline stopping at any step (the goal is to verify it
    # runs through real LLM end-to-end without crashing), but the more steps
    # that complete, the more confident we are that handoffs work.
    print(f"\nPipeline reached: {' -> '.join(completed_agents)}")
    assert len(completed_agents) >= 1


@pytest.mark.asyncio
async def test_pipeline_step_by_step_real_llm():
    """Run Product -> Tech -> Test separately (no CEO), to isolate each step.

    This is the strictest test: verify each agent independently works
    end-to-end with real LLM and the upstream handoff payload is valid.
    """
    os.environ["LLM_PROVIDER"] = "anthropic"
    if not os.environ.get("ANTHROPIC_BASE_URL") and os.environ.get("OPENAI_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = os.environ["OPENAI_BASE_URL"]

    from agent_system.core.llm_router import LLMRouter, LLMConfig, router as default_router
    test_config = LLMConfig(
        model=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
        max_tokens=8000,
        temperature=0.5,
    )
    LLMRouter.get_config = lambda self, agent_name, task_complexity=None: test_config
    s = default_router.settings
    s.llm.default.model = test_config.model
    s.llm.default.max_tokens = test_config.max_tokens
    if hasattr(s.llm, "fast"):
        s.llm.fast.model = test_config.model
        s.llm.fast.max_tokens = test_config.max_tokens
    if hasattr(s.llm, "agents") and s.llm.agents:
        for cfg in s.llm.agents.values():
            if hasattr(cfg, "model"):
                cfg.model = test_config.model
            if hasattr(cfg, "max_tokens"):
                cfg.max_tokens = test_config.max_tokens

    from agent_system.agents.product_agent import ProductAgent
    from agent_system.agents.tech_agent import TechAgent
    from agent_system.agents.test_agent import TestAgent
    from agent_system.core.agent import TaskContext

    print(f"\n=== Step-by-step pipeline (model={test_config.model}) ===")
    total_t0 = time.perf_counter()

    # Step 1: Product
    print("\n[1/3] Product Agent...")
    product = ProductAgent()
    prd_ctx = TaskContext(task_id="e2e-product", input="极简登录功能(3个核心功能点)")
    t0 = time.perf_counter()
    prd = await product.execute(prd_ctx)
    print(f"  Done in {time.perf_counter()-t0:.2f}s, output_id={prd.id}")
    print(f"  Payload keys: {list(prd.payload.keys()) if prd.payload else '(empty)'}")

    # Step 2: Tech (using PRD as upstream)
    print("\n[2/3] Tech Agent...")
    tech = TechAgent()
    tech_ctx = TaskContext(
        task_id="e2e-tech",
        input="基于上面PRD实现代码",
        upstream_output=prd.model_dump(mode="json"),
    )
    t0 = time.perf_counter()
    tech_out = await tech.execute(tech_ctx)
    print(f"  Done in {time.perf_counter()-t0:.2f}s, output_id={tech_out.id}")
    print(f"  Payload keys: {list(tech_out.payload.keys()) if tech_out.payload else '(empty)'}")

    # Step 3: Test (using code as upstream)
    print("\n[3/3] Test Agent...")
    test_agent = TestAgent()
    test_ctx = TaskContext(
        task_id="e2e-test",
        input="为上面的代码写测试",
        upstream_output=tech_out.model_dump(mode="json"),
    )
    t0 = time.perf_counter()
    test_out = await test_agent.execute(test_ctx)
    print(f"  Done in {time.perf_counter()-t0:.2f}s, output_id={test_out.id}")
    print(f"  Payload keys: {list(test_out.payload.keys()) if test_out.payload else '(empty)'}")

    print(f"\n=== Full pipeline in {time.perf_counter()-total_t0:.2f}s ===")
    # All three must complete (with valid payloads)
    assert prd.payload, "Product Agent returned empty payload"
    assert tech_out.payload, "Tech Agent returned empty payload"
    assert test_out.payload, "Test Agent returned empty payload"