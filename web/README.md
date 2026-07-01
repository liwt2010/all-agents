# Agent System — Web UI

React + Vite + TypeScript + Tailwind dashboard for the Agent System API.

## Run

```bash
# 1. Start the backend (terminal 1)
cd ../
uvicorn agent_system.api.server:app --port 8000

# 2. Install + start the frontend (terminal 2)
cd web
npm install
npm run dev
```

Open http://localhost:5173

The Vite dev server proxies `/api/*` to the FastAPI backend on port 8000.
