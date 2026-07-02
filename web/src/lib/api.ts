/**
 * Type-safe API client for the Agent System backend.
 *
 * The Vite dev server proxies /api/* to http://localhost:8000.
 * All authenticated endpoints require a Bearer token.
 */

import axios from "axios";

const client = axios.create({
  baseURL: "/api",
  timeout: 120_000,
});

// ── Token management ──

const TOKEN_KEY = "agent_system_token";

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

// Automatically issue a token on first load (dev mode)
async function ensureToken(): Promise<string | null> {
  let token = getToken();
  if (!token) {
    try {
      const resp = await axios.post("/api/auth/token", {
        user_id: "anonymous",
        tenant_id: "default",
        role: "user",
        ttl: 3600,
      });
      token = resp.data.access_token as string;
      setToken(token);
    } catch {
      return null;
    }
  }
  return token;
}

// Request interceptor: attach Bearer token to every request
client.interceptors.request.use(
  async (config) => {
    const token = getToken();
    if (token) {
      config.headers.Authorization = `Bearer ${token}` as string;
    }
    return config;
  },
  (error) => Promise.reject(error),
);

// Response interceptor: auto-refresh on 401
client.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      const newToken = await ensureToken();
      if (newToken && error.config) {
        error.config.headers.Authorization = `Bearer ${newToken}`;
        return client(error.config);
      }
    }
    return Promise.reject(error);
  },
);

// ── Types ──

export interface HealthResponse {
  status: string;
  version: string;
  uptime: number;
}

export interface AgentInfo {
  name: string;
  description: string;
  capabilities: string[];
}

export interface TaskRequest {
  input: string;
  agent: string;
  department_id?: string;
  task_id?: string;
  // user_id is derived from JWT, not sent by the client
}

export interface TaskResponse {
  task_id: string;
  status: "pending" | "running" | "completed" | "failed";
  output?: Record<string, any> | null;
  error?: string | null;
}

export interface GraphStats {
  total_nodes: number;
  total_links: number;
  nodes_by_type: Record<string, number>;
  links_by_type: Record<string, number>;
}

export interface MetricValue {
  value: number;
  unit: string;
  labels: Record<string, string>;
}

export interface MetricsResponse {
  metrics: Record<string, MetricValue>;
}

export interface LiveProgress {
  task_id: string;
  status: string;
  progress: number;
  current_step: string;
  current_step_id: string;
  completed_steps: string[];
  pending_steps: string[];
  error: string | null;
  started_at: string | null;
  updated_at: string;
  output: any;
  retry_count: number;
}

// ── Endpoints ──

// Ensure token on startup
ensureToken();

export const getHealth = () => client.get<HealthResponse>("/health").then(r => r.data);

export const listAgents = () => client.get<AgentInfo[]>("/agents").then(r => r.data);

export const submitTask = (req: TaskRequest) =>
  client.post<TaskResponse>("/tasks", req).then(r => r.data);

export const getTask = (id: string) =>
  client.get<TaskResponse>(`/tasks/${id}`).then(r => r.data);

export const getTaskProgress = (id: string) =>
  client.get<LiveProgress>(`/tasks/${id}/progress`).then(r => r.data);

export const listTasks = (params?: { limit?: number; offset?: number; status?: string }) =>
  client
    .get<{ tasks: TaskResponse[]; total: number; offset: number }>("/tasks", { params })
    .then(r => r.data);

export const graphStats = () => client.get<GraphStats>("/graph/stats").then(r => r.data);

export const graphNode = (id: string) =>
  client
    .get<{
      node: Record<string, any>;
      neighbors: { node_id: string; node_type: string; link_type: string; depth: number }[];
      outgoing_count: number;
      incoming_count: number;
    }>(`/graph/node/${id}`)
    .then(r => r.data);

export const getMetrics = () => client.get<MetricsResponse>("/metrics").then(r => r.data);

// ── WebSocket helper ──

export function openTaskSocket(
  taskId: string,
  onMessage: (data: any) => void,
  onClose?: () => void,
): WebSocket {
  const token = getToken();
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${window.location.host}/api/ws/${taskId}?token=${token}`);
  ws.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data));
    } catch {
      onMessage(e.data);
    }
  };
  ws.onclose = () => onClose?.();
  return ws;
}
