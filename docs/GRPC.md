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

A gRPC client looks like any other Python gRPC client:

```python
import grpc
from agent_system.grpc import _pb2, _pb2_grpc  # generated

channel = grpc.insecure_channel("localhost:50051")
stub = _pb2_grpc.AgentSystemServiceStub(channel)

# Submit + poll
task = stub.SubmitTask(_pb2.SubmitTaskRequest(
    input="Write a PRD for a login feature",
    agent="product",
    tenant_id="acme",
))
print(f"submitted: {task.id}")

# Stream LLM events
for ev in stub.StreamLLM(_pb2.StreamLLMRequest(
    prompt="Explain RS256 JWT",
    model="claude-haiku-4-5-20251001",
    tenant_id="acme",
)):
    if ev.HasField("text"):
        print(ev.text.text, end="", flush=True)
    elif ev.HasField("done"):
        print(f"\n[done — {ev.done.usage.input_tokens} in / {ev.done.usage.output_tokens} out]")
```

## Why the `.proto` is committed (but not the generated `.py`)

We commit the **`.proto`** (the source of truth) but NOT the
generated `_pb2.py` / `_pb2_grpc.py`. Generating produces
~200 KB of auto-generated code that creates merge noise on every
proto change. The `codegen.py` helper takes ~2 s to run and is
idempotent.

Run `python -m agent_system.grpc.codegen` once on first checkout
and again any time the `.proto` changes. The result is checked
into the `_pb2` module directory at the repo root (or in a
sub-package if you prefer; adjust `codegen.py` to taste).

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

## Limitations

- No gRPC **interceptors** (auth, rate limit) yet — the existing
  HTTP middleware stack doesn't apply. If you need per-RPC
  auth/RL, add a `ServerInterceptor` to the gRPC server in
  `server.py`.
- gRPC server runs in the same process as the REST API. For
  high-RPC workloads, split into a separate deployment via
  `AGENT_GRPC_ONLY=1` (TBD).
- No gRPC reflection — clients must know the proto path. To
  enable: `from grpc_reflection.v1alpha import reflection; reflection.enable_server(...)` after `add_AgentSystemServiceServicer_to_server`.
