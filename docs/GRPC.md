# gRPC transport (PR v0.5.0)

The Agent System now exposes its core task + LLM APIs over gRPC,
in addition to the existing REST + WebSocket transports. All three
transports delegate to the same in-process state — the gRPC
servicer is a thin shim that translates between protobuf messages
and the platform's transport-neutral `dict` shapes.

## Why

gRPC is the natural fit for service-to-service traffic:

- **Strongly-typed contracts** — `.proto` files are the source of
  truth; no drift between docs and code.
- **Streaming first** — `StreamLLM` and `ListTasks` use server-side
  streaming RPCs, so a client sees incremental results without
  polling.
- **Code generation** — generate clients in any of the 11
  first-party languages (Python, TypeScript, Go, Java, ...).

REST is the right choice for browser/curl traffic; gRPC is the
right choice for notebook kernels, microservices, and partner
integrations.

## Wire format

The full schema is in
[`src/agent_system/grpc/proto/agent_system.proto`](src/agent_system/grpc/proto/agent_system.proto).

Four RPCs:

```proto
service AgentSystemService {
  rpc SubmitTask(SubmitTaskRequest) returns (Task);
  rpc GetTask(GetTaskRequest) returns (Task);
  rpc ListTasks(ListTasksRequest) returns (stream ListTasksResponse);
  rpc StreamLLM(StreamLLMRequest) returns (stream LLMEvent);
}
```

`LLMEvent` mirrors the WebSocket wire format
(`{type: "text"|"tool_start"|...}`): it's a oneof, so the
generated client surfaces the right event class per `case`.

## Running the gRPC server

The gRPC transport is **opt-in** — the REST/WS API works exactly
as before. To run a gRPC listener alongside the REST API:

```bash
# 1. Install gRPC (one-time)
pip install grpcio grpcio-tools

# 2. Generate the protobuf modules (one-time, or after a proto change)
python -m agent_system.grpc.codegen

# 3. Start the gRPC server
python -m agent_system.grpc.server            # default :50051
AGENT_GRPC_PORT=50052 python -m agent_system.grpc.server
```

The gRPC server uses the same in-process `TaskStore` and
`LLMRouter` the REST API uses, so a task submitted over HTTP
is immediately visible over gRPC (and vice versa).

## gRPC clients

A gRPC client looks like any other Python gRPC client. The
generated modules live alongside `proto/agent_system.proto` in
`src/agent_system/grpc/` — run `python -m agent_system.grpc.codegen`
once (or after a proto change) and import as below.

```python
import grpc
from agent_system.grpc import agent_system_pb2 as pb
from agent_system.grpc import agent_system_pb2_grpc as pbg

channel = grpc.insecure_channel("localhost:50051")
stub = pbg.AgentSystemServiceStub(channel)

# Submit + poll
task = stub.SubmitTask(pb.SubmitTaskRequest(
    input="Write a PRD for a login feature",
    agent="product",
    tenant_id="acme",
))
print(f"submitted: {task.id}")

# GetTask returns NOT_FOUND for missing / wrong-tenant ids
try:
    miss = stub.GetTask(pb.GetTaskRequest(id="nope", tenant_id="acme"))
except grpc.RpcError as e:
    assert e.code() == grpc.StatusCode.NOT_FOUND

# Stream LLM events (text + tool calls)
for ev in stub.StreamLLM(pb.StreamLLMRequest(
    prompt="Explain RS256 JWT",
    model="claude-haiku-4-5-20251001",
    tenant_id="acme",
)):
    if ev.HasField("text"):
        print(ev.text.text, end="", flush=True)
    elif ev.HasField("tool_start"):
        print(f"\n[tool {ev.tool_start.tool} started]")
    elif ev.HasField("done"):
        u = ev.done.usage
        print(f"\n[done — {u.input_tokens} in / {u.output_tokens} out]")
```

## Why the `.proto` is committed (but not the generated `.py`)

We commit the **`.proto`** (the source of truth) but NOT the
generated `*_pb2.py` / `*_pb2_grpc.py`. Generating produces
~200 KB of auto-generated code that creates merge noise on every
proto change. The `codegen.py` helper takes ~2 s to run and is
idempotent.

Run `python -m agent_system.grpc.codegen` once on first checkout
and again any time the `.proto` changes. The result lands in
`src/agent_system/grpc/` (alongside the `codegen.py` helper) and
is **gitignored** — see `.gitignore`:

```gitignore
src/agent_system/grpc/*_pb2.py
src/agent_system/grpc/*_pb2_grpc.py
```

## Architecture

```
                  ┌─────────────────────────────┐
   gRPC client ──▶│  AgentSystemServiceServicer │ ──▶ GrpcServiceHandler
                  │  (generated, ~200 KB)       │     (transport-neutral)
                  └─────────────────────────────┘                  │
                                                                      │
                            ┌─────────────────────────────────────┘
                            ▼
                  ┌─────────────────────────────┐
                  │  Same in-process state as    │
                  │  REST + WebSocket:           │
                  │   - TaskStore (tasks)        │
                  │   - LLMRouter (LLM)          │
                  └─────────────────────────────┘
```

The handler is `dict`-in, `dict`-out. Future transports (JSON-RPC,
in-process bus, gRPC-Web) can reuse it unchanged.

## Tests

`tests/test_grpc_handlers.py` exercises the handler class
directly — no grpcio required. Once grpcio is installed, the
servicer is a 100-line shim that we don't need to test
separately; the handlers ARE the contract.

End-to-end interop (server + client over a real gRPC channel)
was verified during v0.5.0 development; the script lives in the
commit history (`7db48c7`) and exercises SubmitTask / GetTask /
ListTasks plus the `NOT_FOUND` status path on a missing task.

## Limitations

- No gRPC **interceptors** (auth, rate limit) yet — the existing
  HTTP middleware stack doesn't apply. If you need per-RPC
  auth/RL, add a `ServerInterceptor` to the gRPC server in
  `server.py`.
- gRPC server runs in the same process as the REST API. For
  high-RPC workloads, split into a separate deployment by
  starting `agent_system.grpc.server` standalone — the
  `TaskStore` + `LLMRouter` singletons are independent.
- No gRPC reflection — clients must know the proto path. To
  enable: `from grpc_reflection.v1alpha import reflection; reflection.enable_server(...)` after `add_AgentSystemServiceServicer_to_server`.
- Generated `_pb2` modules are **gitignored** — each developer
  regenerates locally via `agent_system.grpc.codegen`. CI also
  runs codegen before any test that exercises the servicer.
