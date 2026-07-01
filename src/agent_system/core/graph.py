"""
LangGraph main orchestration graph

ARCHITECTURE.md L3: StateGraph with input_validator -> agent_executor -> output_validator
Supports both single-agent and multi-agent (CEO pipeline) modes.
"""

import asyncio
import logging
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, validator

logger = logging.getLogger(__name__)


class GraphState(TypedDict):
    task_id: str
    input: str
    config: Dict[str, Any]
    agent_name: Optional[str]
    output: Optional[Dict[str, Any]]
    validation: Optional[Dict[str, Any]]
    error: Optional[str]
    status: Literal["pending", "running", "completed", "failed"]


def create_graph(agent: SmartAgent) -> StateGraph:
    """Create a LangGraph for a single agent"""

    def input_validator(state: GraphState) -> GraphState:
        errors = []
        if not state.get("input"):
            errors.append("input must not be empty")
        if not state.get("task_id"):
            errors.append("task_id must not be empty")
        if errors:
            state["status"] = "failed"
            state["error"] = "; ".join(errors)
        else:
            state["status"] = "running"
            state["agent_name"] = agent.agent_name
        return state

    async def agent_executor(state: GraphState) -> GraphState:
        task = TaskContext(
            task_id=state["task_id"],
            input=state["input"],
            config=state.get("config", {}),
        )
        try:
            output = await agent.execute(task)
            state["output"] = output.model_dump(mode="json")
            state["status"] = "completed"
        except Exception as e:
            state["status"] = "failed"
            state["error"] = str(e)
            logger.error(f"Agent failed [{state['task_id']}]: {e}")
        return state

    def output_validator(state: GraphState) -> GraphState:
        if state["status"] == "failed":
            return state
        output = state.get("output")
        if not output:
            state["status"] = "failed"
            state["error"] = "No output produced"
            return state
        try:
            schema = OutputSchema(**output)
            v_result = validator.validate(schema)
            state["validation"] = v_result.model_dump()
            if not v_result.valid:
                state["status"] = "failed"
                state["error"] = f"Validation failed: {', '.join(v_result.errors)}"
        except Exception as e:
            state["status"] = "failed"
            state["error"] = f"Parse failed: {e}"
        return state

    def route_from_input(state: GraphState) -> Literal["agent_executor", "__end__"]:
        return END if state["status"] == "failed" else "agent_executor"

    def route_from_agent(state: GraphState) -> Literal["output_validator", "__end__"]:
        return END if state["status"] == "failed" else "output_validator"

    workflow = StateGraph(GraphState)
    workflow.add_node("input_validator", input_validator)
    workflow.add_node("agent_executor", agent_executor)
    workflow.add_node("output_validator", output_validator)
    workflow.set_entry_point("input_validator")
    workflow.add_conditional_edges("input_validator", route_from_input)
    workflow.add_conditional_edges("agent_executor", route_from_agent)
    workflow.add_edge("output_validator", END)

    return workflow


def run_agent_sync(
    agent: SmartAgent,
    task_input: str,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Synchronously run a single agent via LangGraph"""
    if task_id is None:
        task_id = OutputSchema.generate_id("task")

    graph = create_graph(agent)
    memory = MemorySaver()
    app = graph.compile(checkpointer=memory)

    initial_state: GraphState = {
        "task_id": task_id,
        "input": task_input,
        "config": {},
        "agent_name": None,
        "output": None,
        "validation": None,
        "error": None,
        "status": "pending",
    }

    config = {"configurable": {"thread_id": task_id}}

    async def run():
        async for _ in app.astream(initial_state, config):
            pass
        final = await app.aget_state(config)
        return final.values if final else initial_state

    return asyncio.run(run())


async def run_agent_async(
    agent: SmartAgent,
    task_input: str,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Truly async version of run_agent_sync — for use within an existing
    event loop (FastAPI, etc.). Does NOT call asyncio.run().
    """
    if task_id is None:
        task_id = OutputSchema.generate_id("task")

    graph = create_graph(agent)
    memory = MemorySaver()
    app = graph.compile(checkpointer=memory)

    initial_state: GraphState = {
        "task_id": task_id,
        "input": task_input,
        "config": {},
        "agent_name": None,
        "output": None,
        "validation": None,
        "error": None,
        "status": "pending",
    }

    config = {"configurable": {"thread_id": task_id}}

    async for _ in app.astream(initial_state, config):
        pass
    final = await app.aget_state(config)
    return final.values if final else initial_state
