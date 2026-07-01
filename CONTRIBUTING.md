# Contributing to Agent System

Thank you for your interest! This project is at an early stage and we welcome contributions.

## Development

```bash
git clone https://github.com/your-org/agent-system
cd agent-system

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install dev dependencies
pip install -e ".[dev,api,storage]"

# Run tests
AUTH_SECRET="dev-test-32-character-long-key!!" pytest tests/ --ignore=tests/test_performance.py
```

## Guidelines

- **Code**: Python 3.10+, type hints, async-first.
- **Tests**: Every new feature must include tests. Run the full suite before pushing.
- **Security**: Never log secrets, API keys, or user inputs in plaintext.
- **Migrations**: Schema changes go through Alembic (`alembic revision --autogenerate`).

## Code of Conduct

Be respectful. We are all here to learn and build something useful.
