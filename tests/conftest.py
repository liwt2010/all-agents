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

  Second issue solved here:
  `load_dotenv()` injects whatever's in `.env` into os.environ, including
  OPENAI_API_KEY. Real-LLM-gated tests use
  `skipif(not (ANTHROPIC_API_KEY or OPENAI_API_KEY))`, so once a key
  leaks in, those tests stop skipping and actually try to hit the API
  (failing on bad / expired keys). Patch load_dotenv to *not* write
  the two API-key variables unless the user explicitly exported them
  in their shell. Real-LLM tests opt in via the shell (CI sets the
  key explicitly; local devs set ANTHROPIC_API_KEY=/OPENAI_API_KEY=).
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

# Patch python-dotenv so it doesn't leak API keys from `.env` into the
# test process. The real-LLM test suite uses `skipif` on these vars to
# stay green without a key; a leaky `.env` silently flips that switch
# and the tests start hitting the API. Real-LLM tests still run when
# the developer / CI explicitly exports the var in their shell — those
# are set BEFORE pytest starts and load_dotenv() (default
# override=False) leaves them alone.
import dotenv as _dotenv  # noqa: E402

_orig_load_dotenv = _dotenv.load_dotenv


def _load_dotenv_no_api_keys(*args, **kwargs):
    """load_dotenv wrapper that suppresses API-key vars from `.env`.

    All other vars (ALLOWED_FILE_ROOTS, REDIS_URL, etc.) still load.
    """
    # Capture what was already in the environment BEFORE load_dotenv runs
    # so we can restore the "user did not export this" state.
    already_set = {
        k: os.environ.get(k)
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
        if k in os.environ
    }
    _orig_load_dotenv(*args, **kwargs)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if k not in already_set and k in os.environ:
            del os.environ[k]


_dotenv.load_dotenv = _load_dotenv_no_api_keys


def pytest_collection_modifyitems(config, items):
    """
    Make sure iteration* tests are collected in deterministic order
    so test pollution is reproducible.
    """
    items.sort(key=lambda x: x.nodeid)
