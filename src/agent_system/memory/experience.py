"""
Agent 记忆集成 — 经验回流 Mechanism

每次任务完成/失败 → 自动记录到 MultiLinkGraph → 下次执行注入相关经验。
参考架构文档 4.3 经验共享机制。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent_system.memory.graph import (
    MultiLinkGraph,
    GraphNode,
    GraphLink,
    NodeType,
    LinkType,
    get_graph,
)
from agent_system.core.agent import (
    SmartAgent,
    TaskContext,
    OutputSchema,
    event_bus,
    EventType,
    AgentEvent,
)
from agent_system.memory.persistence import save_node, save_link

logger = logging.getLogger(__name__)


def record_task_start(graph: MultiLinkGraph, task_id: str, input_text: str, agent_name: str):
    """Record a task as a graph node"""
    node = GraphNode(
        id=task_id,
        type=NodeType.TASK,
        content={"input": input_text[:500], "agent": agent_name},
        metadata={"agent_name": agent_name, "status": "running"},
    )
    graph.add_node(node)
    save_node(node)


def record_task_complete(
    graph: MultiLinkGraph,
    task_id: str,
    output: OutputSchema,
):
    """Record task completion and link to output"""
    # Update task node
    task_node = graph.get_node(task_id)
    if task_node:
        task_node.content["status"] = "completed"
        task_node.updated_at = datetime.now(timezone.utc)
        task_node.content["output_id"] = output.id
        save_node(task_node)

    # Record output node
    output_node = GraphNode(
        id=output.id,
        type=NodeType.OUTPUT,
        content={
            "type": output.type,
            "payload_summary": str(output.payload)[:300],
            "created_by": output.created_by,
        },
        metadata={
            "task_id": task_id,
            "agent_name": output.created_by,
        },
    )
    graph.add_node(output_node)
    save_node(output_node)

    # Link: task -> output
    graph.link(
        source_id=task_id,
        target_id=output.id,
        link_type=LinkType.CREATED_BY,
        context={"step": "execution"},
        created_by=output.created_by,
    )
    save_link(GraphLink(
        source_id=task_id,
        target_id=output.id,
        link_type=LinkType.CREATED_BY,
        context={"step": "execution"},
        created_by=output.created_by,
    ))


def record_task_failure(
    graph: MultiLinkGraph,
    task_id: str,
    error: str,
    agent_name: str,
    details: dict[str, Any] | None = None,
):
    """Record task failure and create failure node + link"""
    # Update task node
    task_node = graph.get_node(task_id)
    if task_node:
        task_node.content["status"] = "failed"
        task_node.content["error"] = error[:500]
        task_node.updated_at = datetime.now(timezone.utc)
        save_node(task_node)

    # Create failure node
    failure_id = f"failure-{task_id}"
    failure_node = GraphNode(
        id=failure_id,
        type=NodeType.FAILURE,
        content={
            "error": error[:1000],
            "agent": agent_name,
            "task_id": task_id,
        },
        metadata=details or {},
    )
    graph.add_node(failure_node)
    save_node(failure_node)

    # Link: task --caused_by-> failure
    graph.link(
        source_id=task_id,
        target_id=failure_id,
        link_type=LinkType.CAUSED_BY,
        context={"error": error[:200]},
        created_by=agent_name,
    )
    save_link(GraphLink(
        source_id=task_id,
        target_id=failure_id,
        link_type=LinkType.CAUSED_BY,
        context={"error": error[:200]},
        created_by=agent_name,
    ))

    # Look for similar past failures (experience lookup)
    similar = find_similar_failures(graph, error)
    if similar:
        logger.info(f"Found {len(similar)} similar past failures for task {task_id}")
        for exp_node, _ in similar[:3]:
            # Link new failure to relevant experience
            graph.link(
                source_id=failure_id,
                target_id=exp_node.id,
                link_type=LinkType.REFERENCES,
                context={"relevance": "similar_failure"},
                created_by=agent_name,
            )
            save_link(GraphLink(
                source_id=failure_id,
                target_id=exp_node.id,
                link_type=LinkType.REFERENCES,
                context={"relevance": "similar_failure"},
                created_by=agent_name,
            ))


def record_experience(
    graph: MultiLinkGraph,
    task_id: str,
    summary: str,
    agent_name: str,
    success: bool = True,
    related_failure_ids: list[str] | None = None,
):
    """Record a distilled experience from a task"""
    exp_id = f"exp-{task_id}"
    exp_node = GraphNode(
        id=exp_id,
        type=NodeType.EXPERIENCE,
        content={
            "summary": summary[:1000],
            "agent": agent_name,
            "success": success,
            "task_id": task_id,
        },
        metadata={
            "source": "experience_loop",
        },
    )
    graph.add_node(exp_node)
    save_node(exp_node)

    # Link task -> experience
    graph.link(
        source_id=task_id,
        target_id=exp_id,
        link_type=LinkType.EVOLVED_FROM,
        created_by=agent_name,
    )
    save_link(GraphLink(
        source_id=task_id,
        target_id=exp_id,
        link_type=LinkType.EVOLVED_FROM,
        created_by=agent_name,
    ))

    # Link related failures
    if related_failure_ids:
        for fid in related_failure_ids:
            if graph.has_node(fid):
                graph.link(
                    source_id=fid,
                    target_id=exp_id,
                    link_type=LinkType.REFERENCES,
                    context={"relationship": "resolved_by"},
                    created_by=agent_name,
                )
                save_link(GraphLink(
                    source_id=fid,
                    target_id=exp_id,
                    link_type=LinkType.REFERENCES,
                    context={"relationship": "resolved_by"},
                    created_by=agent_name,
                ))


def find_similar_failures(
    graph: MultiLinkGraph,
    error_text: str,
    max_results: int = 5,
    half_life_days: float = 30.0,
) -> list[tuple[GraphNode, float]]:
    """
    Find similar past failures by similarity (embedding/keyword) and recency.

    Score = similarity * decay_factor(age). Recent experiences with similar
    content rank higher than old ones.
    """
    if not error_text:
        return []

    from agent_system.memory.embeddings import get_backend, effective_score

    backend = get_backend()
    candidates = [
        (node, json.dumps(node.content, ensure_ascii=False))
        for node in graph.find_nodes(node_type=NodeType.EXPERIENCE)
    ]
    if not candidates:
        return []

    if hasattr(backend, "find_top_k"):
        # TfidfBackend / similar batch backend
        texts = [c[1] for c in candidates]
        ranked = backend.find_top_k(error_text, texts, k=max_results * 2)  # type: ignore
        scored = []
        for idx, sim in ranked:
            if sim < 0.01:
                continue
            node = candidates[idx][0]
            eff = effective_score(sim, node, half_life_days=half_life_days)
            scored.append((node, eff))
    else:
        # Fallback: per-pair similarity
        scored = []
        q_vec = backend.embed([error_text])[0]
        for node, text in candidates:
            n_vec = backend.embed([text])[0]
            sim = backend.similarity(q_vec, n_vec)
            if sim < 0.01:
                continue
            eff = effective_score(sim, node, half_life_days=half_life_days)
            scored.append((node, eff))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:max_results]


def get_relevant_experiences(
    graph: MultiLinkGraph,
    task_input: str,
    max_results: int = 3,
    half_life_days: float = 30.0,
) -> list[str]:
    """
    Get relevant experience summaries for injecting into agent prompts.

    Uses similarity * recency decay for ranking.
    """
    if not task_input:
        return []

    from agent_system.memory.embeddings import get_backend, effective_score

    backend = get_backend()
    candidates = []
    for node in graph.find_nodes(node_type=NodeType.EXPERIENCE):
        summary = node.content.get("summary", "")
        if not summary:
            continue
        candidates.append((node, summary))

    if not candidates:
        return []

    if hasattr(backend, "find_top_k"):
        texts = [c[1] for c in candidates]
        ranked = backend.find_top_k(task_input, texts, k=max_results * 2)  # type: ignore
        scored = []
        for idx, sim in ranked:
            if sim < 0.01:
                continue
            node = candidates[idx][0]
            eff = effective_score(sim, node, half_life_days=half_life_days)
            scored.append((node, eff))
    else:
        scored = []
        q_vec = backend.embed([task_input])[0]
        for node, text in candidates:
            n_vec = backend.embed([text])[0]
            sim = backend.similarity(q_vec, n_vec)
            if sim < 0.01:
                continue
            eff = effective_score(sim, node, half_life_days=half_life_days)
            scored.append((node, eff))

    scored.sort(key=lambda x: x[1], reverse=True)

    summaries = []
    for node, _ in scored[:max_results]:
        summary = node.content.get("summary", "")
        if summary:
            summaries.append(f"- [{node.id}] {summary[:200]}")

    return summaries


# ── Agent 集成 hook ──

def install_memory_hooks(agent: SmartAgent):
    """Install memory hooks on an agent to auto-record to graph"""

    original_execute = agent.execute

    async def memory_aware_execute(task: TaskContext) -> OutputSchema:
        graph = get_graph()

        # Record task start
        record_task_start(graph, task.task_id, task.input, agent.agent_name)

        # Inject relevant experiences into task metadata
        experiences = get_relevant_experiences(graph, task.input)
        if experiences:
            task.metadata["experiences"] = experiences

        try:
            output = await original_execute(task)
            # Record success
            record_task_complete(graph, task.task_id, output)

            # Optionally record experience (configurable)
            if not task.metadata.get("skip_experience_recording"):
                record_experience(
                    graph,
                    task.task_id,
                    f"Agent {agent.agent_name} completed task: {task.input[:100]}",
                    agent.agent_name,
                    success=True,
                )

            return output
        except Exception as e:
            # Record failure
            record_task_failure(
                graph,
                task.task_id,
                str(e),
                agent.agent_name,
                details={"attempts": task.retry_count},
            )
            raise

    # Monkey-patch the execute method via object.__setattr__ to bypass Pydantic
    object.__setattr__(agent, 'execute', memory_aware_execute)
    return agent
