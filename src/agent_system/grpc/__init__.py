"""gRPC transport for the Agent System (PR v0.5.0).

Run `python -m agent_system.grpc.server` to listen on :50051
(default; override with `AGENT_GRPC_PORT`).

See:
  - src/agent_system/grpc/proto/agent_system.proto — wire format
  - src/agent_system/grpc/handlers.py    — RPC implementations
  - src/agent_system/grpc/server.py     — gRPC bootstrap
  - src/agent_system/grpc/codegen.py    — one-shot codegen helper
"""
from agent_system.grpc.handlers import GrpcServiceHandler, is_grpc_available

__all__ = ["GrpcServiceHandler", "is_grpc_available"]
