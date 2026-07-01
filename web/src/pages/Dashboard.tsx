import { useEffect, useState } from "react";
import { getHealth, listAgents, listTasks, getMetrics, HealthResponse, AgentInfo, MetricsResponse, TaskResponse } from "../lib/api";
import { CheckCircle2, AlertCircle, Loader2, Bot, Send, Activity } from "lucide-react";
import { Link } from "react-router-dom";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";

function MetricTile({ name, value, unit }: { name: string; value: number; unit: string }) {
  const display = unit === "ratio" ? (value * 100).toFixed(1) + "%" : value.toFixed(2);
  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-4">
      <div className="text-xs text-slate-400 uppercase tracking-wide">{name.replace(/_/g, " ")}</div>
      <div className="mt-1 text-2xl font-bold text-white">{display}</div>
      <div className="text-xs text-slate-500 mt-1">{unit}</div>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const colors: Record<string, string> = {
    completed: "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
    failed: "bg-red-500/20 text-red-300 border-red-500/30",
    running: "bg-amber-500/20 text-amber-300 border-amber-500/30",
    pending: "bg-slate-500/20 text-slate-300 border-slate-500/30",
  };
  return (
    <span className={`inline-block px-2 py-0.5 text-xs rounded border ${colors[status] || colors.pending}`}>
      {status}
    </span>
  );
}

export default function Dashboard() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const [h, a, t, m] = await Promise.all([
        getHealth(),
        listAgents(),
        listTasks({ limit: 5 }),
        getMetrics(),
      ]);
      setHealth(h);
      setAgents(a);
      setTasks(t.tasks);
      setMetrics(m);
      setError(null);
    } catch (e: any) {
      setError(e.message || "Failed to load");
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const metricNames = metrics ? Object.keys(metrics.metrics) : [];
  const agentData = agents.map((a) => ({ name: a.name, capabilities: a.capabilities.length }));

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Dashboard</h1>
          <p className="text-sm text-slate-400 mt-1">Real-time overview of the multi-agent system</p>
        </div>
        <div className="flex items-center gap-3">
          {health ? (
            <span className="flex items-center gap-2 text-sm text-emerald-400">
              <CheckCircle2 className="w-4 h-4" /> v{health.version} — up {Math.round(health.uptime)}s
            </span>
          ) : (
            <span className="flex items-center gap-2 text-sm text-amber-400">
              <Loader2 className="w-4 h-4 animate-spin" /> Connecting...
            </span>
          )}
          <Link
            to="/submit"
            className="flex items-center gap-2 px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white rounded-md text-sm font-medium"
          >
            <Send className="w-4 h-4" /> New Task
          </Link>
        </div>
      </header>

      {error && (
        <div className="flex items-center gap-2 bg-red-500/10 border border-red-500/30 text-red-300 px-4 py-3 rounded">
          <AlertCircle className="w-4 h-4" /> {error}
        </div>
      )}

      {/* 9 Metrics */}
      <section>
        <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3 flex items-center gap-2">
          <Activity className="w-4 h-4" /> Auto-calculated metrics
        </h2>
        <div className="grid grid-cols-3 gap-3">
          {metricNames.length === 0 && <div className="text-slate-500 text-sm col-span-3">Loading...</div>}
          {metricNames.map((name) => {
            const m = metrics!.metrics[name];
            return <MetricTile key={name} name={name} value={m.value} unit={m.unit} />;
          })}
        </div>
      </section>

      {/* Agents + Recent Tasks side by side */}
      <div className="grid grid-cols-2 gap-6">
        <section>
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3 flex items-center gap-2">
            <Bot className="w-4 h-4" /> Agents ({agents.length})
          </h2>
          <div className="space-y-2">
            {agents.map((a) => (
              <div key={a.name} className="bg-slate-800/50 border border-slate-700 rounded-lg p-3">
                <div className="flex items-center justify-between">
                  <div className="font-mono text-brand-400 text-sm">{a.name}</div>
                  <div className="text-xs text-slate-500">{a.capabilities.length} capabilities</div>
                </div>
                <div className="text-sm text-slate-300 mt-1">{a.description}</div>
              </div>
            ))}
          </div>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Recent Tasks</h2>
          <div className="space-y-2">
            {tasks.length === 0 && <div className="text-slate-500 text-sm">No tasks yet</div>}
            {tasks.map((t) => (
              <div key={t.task_id} className="bg-slate-800/50 border border-slate-700 rounded-lg p-3 flex items-center justify-between">
                <div className="flex-1 min-w-0">
                  <div className="font-mono text-xs text-slate-400 truncate">{t.task_id}</div>
                  <div className="text-xs text-slate-500 truncate mt-0.5">
                    {t.output?.payload?.title || t.error || "(no output)"}
                  </div>
                </div>
                <StatusPill status={t.status} />
              </div>
            ))}
          </div>
        </section>
      </div>

      {/* Agent capabilities chart */}
      <section>
        <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Agent capability counts</h2>
        <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-4" style={{ height: 200 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={agentData}>
              <XAxis dataKey="name" stroke="#94a3b8" />
              <YAxis stroke="#94a3b8" />
              <Tooltip contentStyle={{ background: "#1e293b", border: "1px solid #475569" }} />
              <Bar dataKey="capabilities" fill="#0ea5e9">
                {agentData.map((_, i) => (
                  <Cell key={i} fill={["#0ea5e9", "#8b5cf6", "#10b981", "#f59e0b"][i % 4]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>
    </div>
  );
}
