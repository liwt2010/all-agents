# How to regenerate `requirements.txt`

`requirements.txt` is the dependency lock file for `agent-system`. It pins exact
versions of all transitive dependencies so CI gets reproducible installs.

## When to regenerate

- After updating `pyproject.toml` (new dep, new version constraint, new extra)
- After upgrading the local Python version
- If a test is failing in CI but passing locally (likely a version drift)

## How to regenerate (offline — uses current venv snapshot)

```bash
# Make sure your venv has all the deps you want locked
pip install -e ".[dev,api,storage]"

# Snapshot the venv into requirements.txt
pip list --format=freeze | grep -v "^agent-system==" > requirements.txt
```

The `agent-system` line is filtered out — CI installs the project itself via
`pip install -e .` after installing the lock file.

## How to regenerate (online — uses pip-compile for clean tree)

If you have network access to PyPI:

```bash
pip install pip-tools
pip-compile --output-file=requirements.txt --no-header --strip-extras pyproject.toml
```

`pip-compile` produces a minimal-resolution tree (no duplicates), whereas
`pip list --format=freeze` lists everything currently installed.

## CI behavior

CI now does:

```yaml
- pip install -r requirements.txt   # exact versions
- pip install -e ".[dev,api,storage]"  # project itself
```

This means CI is reproducible: same versions every run, regardless of when the
job executes. Combined with PR 4 (config compat), CI no longer fails on
claude-vs-deepseek drift.