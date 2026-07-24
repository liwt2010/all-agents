"""
End-to-end gRPC interop test for v0.6.0 owner attribution.

Spins up a real grpc.server + Stub over an insecure channel, and
verifies that x-user-id / x-tenant-id metadata propagates into the
TaskRecord's owner_id and tenant_id.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import grpc
import pytest

from agent_system.storage.task_store import InMemoryTaskStore

# Skip the entire module if grpcio isn't installed (these are
# integration tests, not the handler-level unit tests).
grpcio_available = pytest.importorskip("grpc", reason="grpcio required for interop")


@pytest.fixture
def real_grpc_server():
    """Stand up a real gRPC server with a fresh InMemoryTaskStore."""
    from agent_system.grpc import agent_system_pb2 as pb, agent_system_pb2_grpc as pbg
    from agent_system.grpc.server import _build_patches
    from agent_system.grpc.handlers import GrpcServiceHandler

    Servicer, _, _pb2_grpc = _build_patches()
    store = InMemoryTaskStore()
    """Real LLM is irrelevant for this test — provide a no-op router so
the background _run_task() spawned by submit_task exits cleanly."""
    import asyncio
    class _NoopLLM:
        async def call_llm(self, *a, **kw):
            return ("ok", None)
        async def stream_events(self, *a, **kw):
            if False:
                yield None  # never reached

    handler = GrpcServiceHandler({
        "task_store": store,
        "llm_router": _NoopLLM(),
        "config_getter": None,
        "require_auth": None,
    })
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    _pb2_grpc.add_AgentSystemServiceServicer_to_server(Servicer(handler), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield pbg, pb, store, f"127.0.0.1:{port}"
    finally:
        server.stop(0)


def test_metadata_x_user_id_sets_owner(real_grpc_server):
    pbg, pb, store, addr = real_grpc_server
    ch = grpc.insecure_channel(addr)
    stub = pbg.AgentSystemServiceStub(ch)
    md = (("x-user-id", "alice"), ("x-tenant-id", "acme"))
    sub = stub.SubmitTask(
        pb.SubmitTaskRequest(input="hi", agent="product", tenant_id=""),
        metadata=md,
    )
    saved = store.get(sub.id)
    assert saved is not None
    assert saved.owner_id == "alice"
    assert saved.tenant_id == "acme"
    assert saved.visibility == "private"
    assert saved.assignee_id is None
    # version starts at 1 and only increments on update_fields(); the
    # background _run_task may have bumped it to 2 if it finished
    # synchronously — either is valid.
    assert saved.version in (1, 2)


def test_no_metadata_defaults_owner_to_system(real_grpc_server):
    pbg, pb, store, addr = real_grpc_server
    ch = grpc.insecure_channel(addr)
    stub = pbg.AgentSystemServiceStub(ch)
    sub = stub.SubmitTask(
        pb.SubmitTaskRequest(input="anon", agent="tech", tenant_id="default"),
    )
    saved = store.get(sub.id)
    assert saved.owner_id == "system"
    assert saved.tenant_id == "default"


def test_x_tenant_id_metadata_overrides_request(real_grpc_server):
    """When both request.tenant_id and x-tenant-id metadata are
    supplied, metadata wins — the metadata is the authenticated identity."""
    pbg, pb, store, addr = real_grpc_server
    ch = grpc.insecure_channel(addr)
    stub = pbg.AgentSystemServiceStub(ch)
    md = (("x-user-id", "bob"), ("x-tenant-id", "acme"))
    sub = stub.SubmitTask(
        pb.SubmitTaskRequest(input="hi", agent="product", tenant_id="WRONG"),
        metadata=md,
    )
    saved = store.get(sub.id)
    assert saved.tenant_id == "acme"  # metadata won