# Agent System — Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY src/ ./src/

# Install Python deps (api + storage)
RUN pip install --no-cache-dir -e ".[api,storage]"

# Create data directories under /data (mounted as a volume in
# docker-compose / k8s for persistence). Default locations match
# the .env.example paths (AGENT_SQLITE_PATH, AGENT_AUDIT_LOG_DIR, etc.).
RUN mkdir -p /data/graph/nodes /data/graph/links /data/audit /data/backup /data/checkpoints
# Keep the relative-paths layout for dev (no volume mount)
RUN mkdir -p /app/data/graph/nodes /app/data/graph/links /app/data/audit /app/data/backup /app/data/checkpoints

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

# Default: start API server
CMD ["uvicorn", "agent_system.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
