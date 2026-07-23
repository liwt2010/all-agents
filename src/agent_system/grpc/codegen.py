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
    proto_dir = repo_root / "agent_system" / "grpc" / "proto"
    out_dir = repo_root / "agent_system" / "grpc"
    proto_file = proto_dir / "agent_system.proto"

    if not proto_file.exists():
        print(f"error: proto file not found: {proto_file}", file=sys.stderr)
        return 2

    cmd = [
        "python",
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

    # protoc emits a file `_pb2.py` and a file `_pb2_grpc.py`. The
    # generated `_pb2_grpc.py` imports `_pb2` as a top-level module,
    # which won't work when the file lives in our package. We fix
    # that import with a one-liner sed.
    for f in (out_dir / "_pb2_grpc.py",):
        if f.exists():
            text = f.read_text(encoding="utf-8")
            # Replace `import agent_system_pb2 as ...` with a relative
            # import. protoc emits:
            #   import agent_system_pb2 as agent__system__pb2
            # which only works if agent_system_pb2 is on sys.path.
            # We replace with the package-relative form:
            #   from . import _pb2 as agent__system__pb2
            new = text.replace(
                "import agent_system_pb2 as",
                "from . import _pb2 as",
            )
            if new != text:
                f.write_text(new, encoding="utf-8")
                print(f"patched relative import in {f.name}")
    print("ok — generated:")
    print(f"  {out_dir / '_pb2.py'}")
    print(f"  {out_dir / '_pb2_grpc.py'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
