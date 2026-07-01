import { useEffect, useState } from "react";
import { listTasks, TaskResponse } from "../lib/api";
import { Loader2, Filter } from "lucide-react";
import clsx from "clsx";

const STATUSES = ["all", "completed", "failed", "running", "pending"];

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

export default function Tasks() {
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const params = status !== "all" ? { status, limit: 50 } : { limit: 50 };
      const data = await listTasks(params);
      setTasks(data.tasks);
      setError(null);
    } catch (e: any) {
      setError(e.message || "Failed to load");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [status]);

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Tasks</h1>
          <p className="text-sm text-slate-400 mt-1">{tasks.length} task(s) shown</p>
        </div>
        <div className="flex items-center gap-2">
          <Filter className="w-4 h-4 text-slate-500" />
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className={clsx(
                "px-3 py-1.5 text-xs rounded-md border transition-colors",
                status === s
                  ? "bg-brand-600 text-white border-brand-500"
                  : "bg-slate-800/50 text-slate-400 border-slate-700 hover:bg-slate-800",
              )}
            >
              {s}
            </button>
          ))}
        </div>
      </header>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-300 px-4 py-3 rounded">
          {error}
        </div>
      )}

      {loading && tasks.length === 0 ? (
        <div className="flex items-center gap-2 text-slate-400">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading...
        </div>
      ) : tasks.length === 0 ? (
        <div className="bg-slate-800/30 border border-dashed border-slate-700 rounded-lg p-8 text-center text-slate-500">
          No tasks match the current filter
        </div>
      ) : (
        <div className="bg-slate-800/50 border border-slate-700 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/50 text-slate-400 text-xs uppercase tracking-wider">
              <tr>
                <th className="text-left px-4 py-3">Task ID</th>
                <th className="text-left px-4 py-3">Title / Content</th>
                <th className="text-left px-4 py-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t) => {
                const title = t.output?.payload?.title || t.output?.payload?.pipeline_status || t.error || "(no content)";
                return (
                  <tr key={t.task_id} className="border-t border-slate-700/50 hover:bg-slate-800/50">
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">{t.task_id}</td>
                    <td className="px-4 py-3 text-slate-200 max-w-md truncate">{title}</td>
                    <td className="px-4 py-3"><StatusPill status={t.status} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
