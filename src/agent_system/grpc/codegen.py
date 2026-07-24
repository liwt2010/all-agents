"""
gRPC code generation helper.

Run this once to generate the _pb2 / _pb2_grpc modules that the
gRPC server imports. We don't pre-generate and check these in
because the output is 200+ KB of auto-generated code that would
bloat the repo and create merge noise on every proto change.

Usage:
    python -m agent_system.grpc.codegen

This invokes grpc_tools.protoc with the right -I / --python_out
flags. If grpcio-tools isn't installed, prints a hint and exits 2.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        # grpc_tools is shipped as a console_script by grpcio-tools.
        # We just need to call its compiler programmatically.
        from grpc_tools import protoc  # type: ignore
    except ImportError:
        print(
            "error: grpcio-tools not installed. Run:\n"
            "  pip install grpcio grpcio-tools",
            file=sys.stderr,
        )
        return 2

    repo_root = Path(__file__).resolve().parents[3]  # .../src/agent_system/grpc
    proto_dir = repo_root / "src" / "agent_system" / "grpc" / "proto"
    out_dir = repo_root / "src" / "agent_system" / "grpc"
    proto_file = proto_dir / "agent_system.proto"

    if not proto_file.exists():
        print(f"error: proto file not found: {proto_file}", file=sys.stderr)
        return 2

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        str(proto_file),
    ]
    print("running:", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("error: protoc failed", file=sys.stderr)
        print(res.stdout, file=sys.stderr)
        print(res.stderr, file=sys.stderr)
        return res.returncode

    # protoc emits `<proto-basename>_pb2.py` and `<proto-basename>_pb2_grpc.py`
    # based on the .proto file's basename. For agent_system.proto, the
    # files are agent_system_pb2.py and agent_system_pb2_grpc.py.
    #
    # The generated _pb2_grpc.py imports `agent_system_pb2` as a
    # top-level module, which only works if the grpc directory is on
    # sys.path. We patch that import to be package-relative so the
    # module works inside the agent_system package.
    proto_basename = proto_file.stem  # 'agent_system'
    pb2_file = out_dir / f"{proto_basename}_pb2.py"
    pb2_grpc_file = out_dir / f"{proto_basename}_pb2_grpc.py"

    if pb2_grpc_file.exists():
        text = pb2_grpc_file.read_text(encoding="utf-8")
        new = text.replace(
            f"import {proto_basename}_pb2 as",
            f"from . import {proto_basename}_pb2 as",
        )
        if new != text:
            pb2_grpc_file.write_text(new, encoding="utf-8")
            print(f"patched relative import in {pb2_grpc_file.name}")
    print("ok — generated:")
    print(f"  {pb2_file}")
    print(f"  {pb2_grpc_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
