"""
Pytest session-wide fixtures + env setup.

Key issue solved here:
  The user's local `.env` has ALLOWED_FILE_ROOTS=data,tmp (production
  sandbox default), but several legacy tests (e.g. test_iteration1.py::
  TestToolRegistry) read files from cwd and assume cwd is allowed.

  Without this conftest, importing `agent_system.api.server` triggers
  `python-dotenv load_dotenv()`, which sets ALLOWED_FILE_ROOTS=data,tmp
  from .env. Subsequent tests that try to read cwd files (like
  pyproject.toml) fail with "path outside allowed roots".

  Fix: when pytest collects, force ALLOWED_FILE_ROOTS to include cwd so
  the tests can read source-tree files. Tests that explicitly need the
  stricter sandbox can set it manually.
"""
import os
import sys
from pathlib import Path

# Make `src/` importable so individual tests don't need PYTHONPATH=src
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Force cwd in allowed roots so legacy tests work even after server.py
# imports load_dotenv() with a stricter .env default.
os.environ["ALLOWED_FILE_ROOTS"] = "data,tmp,."

# Provide a default JWT secret for tests that construct AuthService() with
# no args (e.g. test_singleton, test_non_http_passes_through). 32+ chars
# so the production-length warning doesn't fire.
os.environ.setdefault(
    "AUTH_SECRET",
    "test-only-jwt-secret-32chars-long-enough-for-hs256",
)


def pytest_collection_modifyitems(config, items):
    """
    Make sure iteration* tests are collected in deterministic order
    so test pollution is reproducible.
    """
    items.sort(key=lambda x: x.nodeid)
