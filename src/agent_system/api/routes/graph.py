"""Graph query endpoints - tenant-isolated access to memory graph."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from agent_system.api.state import get_auth_service_singleton
from agent_system.core.auth import User, require_auth

router = APIRouter(tags=["graph"])


@router.get("/api/graph/stats")
async def graph_stats(
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> dict[str, Any]:
    """Get graph statistics (tenant-isolated)."""
    from agent_system.memory.graph import get_graph
    graph = get_graph()
    return graph.stats()


@router.get("/api/graph/node/{node_id}")
async def get_graph_node(
    node_id: str,
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> dict[str, Any]:
    """Get a graph node with neighbors (tenant-isolated)."""
    from agent_system.memory.graph import get_graph
    graph = get_graph()
    ctx = graph.related_with_context(node_id)
    if not ctx["node"]:
        raise HTTPException(status_code=404, detail="Node not found")

    # Tenant isolation check
    node_tenant = (ctx["node"].metadata or {}).get("tenant_id", "default")
    if node_tenant != user.tenant_id and user.global_role.value not in ("platform_admin", "tenant_admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "node": ctx["node"].model_dump(mode="json"),
        "neighbors": [
            {
                "node_id": n.node.id,
                "node_type": n.node.type.value,
                "link_type": n.link.link_type.value,
                "depth": n.depth,
            }
            for n in ctx["neighbors"]
        ],
        "outgoing_count": ctx["outgoing_count"],
        "incoming_count": ctx["incoming_count"],
    }
