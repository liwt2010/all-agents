# Contributing to Agent System

Thank you for your interest! This project is at an early stage and we welcome contributions.

## Development Setup

### 1. Clone & Install

```bash
git clone https://github.com/your-org/agent-system
cd agent-system

# Create virtual environment (Python 3.10+ required)
python -m venv .venv
source .venv/bin/activate   # macOS/Linux
# or
.venv\Scripts\activate      # Windows

# Install dev dependencies
pip install -e ".[dev,api,storage]"
```

### 2. Install pre-commit hooks (REQUIRED)

Pre-commit hooks run automated checks BEFORE you commit, catching most issues
in seconds rather than waiting 5+ minutes for CI feedback.

```bash
# Install pre-commit (one-time)
pip install pre-commit

# Install the git hooks (one-time per clone)
pre-commit install

# (Optional) Run hooks against all files to verify setup
pre-commit run --all-files
```

**What the hooks do**:
- `ruff` -- lint + format (replaces flake8/black/isort)
- `mypy` -- type check (strict on `core/`, `memory/`, `storage/`)
- `detect-private-key` -- block accidental API key commits
- Standard checks -- trailing whitespace, EOF newlines, large files, etc.
- Custom `no-secrets` -- regex-based block on common secret patterns

**Skip hooks (rarely needed)**:
```bash
git commit --no-verify -m "WIP: ..."
```

### 3. Verify Setup

```bash
# Should pass with no errors on a clean repo
AUTH_SECRET="dev-test-32-character-long-key!!" pytest tests/ --ignore=tests/test_performance.py

# Should pass with no warnings
ruff check src/ tests/
```

## Pull Request Workflow

### Branch naming

- `feat/<scope>` -- new feature (e.g. `feat/multi-tenant-graph`)
- `fix/<scope>` -- bug fix (e.g. `fix/jwt-rotation-leak`)
- `docs/<scope>` -- documentation only
- `chore/<scope>` -- tooling / refactoring

### Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(core): add agent capability negotiation
fix(rate-limit): correct sliding window math
docs(README): update API quick-start example
```

### Before opening a PR

- [ ] `pre-commit run --all-files` passes
- [ ] All tests pass locally
- [ ] New features have tests (see [Testing](#testing))
- [ ] Breaking changes documented in `CHANGELOG.md`
- [ ] Security-sensitive changes reviewed by 2+ maintainers

## Coding Guidelines

- **Code**: Python 3.10+, type hints, async-first
- **Style**: ruff-format (auto-applied by pre-commit)
- **Imports**: ruff/isort (auto-applied)
- **Type hints**: required on public APIs, recommended everywhere
- **Tests**: every new feature must include tests
- **Security**: never log secrets, API keys, or user inputs in plaintext
- **Migrations**: schema changes go through Alembic (`alembic revision --autogenerate`)

## Testing

```bash
# Unit tests (no API key needed, runs in CI)
pytest tests/ --ignore=tests/test_*real_llm.py

# Real-LLM tests (need ANTHROPIC_API_KEY or OPENAI_API_KEY)
pytest tests/test_*real_llm.py -v

# Performance benchmarks
pytest tests/test_performance.py -v

# Production-readiness gate (must pass)
pytest tests/test_production_readiness.py -v
```

### Test categories

| Category | When to write | Skip in CI? |
|----------|---------------|-------------|
| Unit (`tests/test_*.py`, not `real_llm`) | Always | No |
| Boundary (`tests/test_boundary_*.py`) | Bug fix or edge case | No |
| Real-LLM (`tests/test_*real_llm.py`) | E2E behavior | Yes (manual/weekly) |
| Performance (`tests/test_performance*.py`) | Hot-path optimization | Yes (advisory) |
| Production-readiness (`tests/test_production_readiness.py`) | Infra / docs / config | No |

## Code of Conduct

Be respectful. We are all here to learn and build something useful.

## Questions?

- Open a GitHub Discussion for general questions
- Open a GitHub Issue for bugs / feature requests
- See [SECURITY.md](SECURITY.md) for security disclosures
