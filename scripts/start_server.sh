#!/bin/bash
# Agent System startup script — loads .env then starts uvicorn
cd "$(dirname "$0")"

# Load .env into environment (ignore comments, trim carriage returns)
export $(grep -v '^\s*#' .env | grep -v '^\s*$' | tr -d '\r' | xargs)

echo "=== Environment ==="
echo "LLM_PROVIDER=$LLM_PROVIDER"
echo "OPENAI_API_KEY set: $( [ -n "$OPENAI_API_KEY" ] && echo YES || echo NO )"
echo "AUTH_SECRET set: $( [ -n "$AUTH_SECRET" ] && echo YES || echo NO )"
echo "ENVIRONMENT=$ENVIRONMENT"
echo "==================="

source .venv/Scripts/activate
uvicorn agent_system.api.server:app --host 0.0.0.0 --port 8000
