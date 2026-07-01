import { useState, useEffect, useRef } from "react";
import { listAgents, submitTask, getTaskProgress, openTaskSocket, AgentInfo, TaskRequest, TaskResponse, LiveProgress } from "../lib/api";
import { Send, Loader2, CheckCircle2, AlertCircle, Terminal, Activity, Clock } from "lucide-react";
import clsx from "clsx";

const SAMPLE_TASKS = [
  { agent: "product", input: "Build a user profile page with avatar upload" },
  { agent: "ceo", input: "Build a simple todo app" },
  { agent: "tech", input: "Implement a JWT auth middleware" },
  { agent: "test", input: "Write tests for the login flow" },
];

function ProgressBar({ value, status }: { value: number; status: string }) {
  const pct = Math.round(value * 100);
  const color =
    status === "failed" ? "bg-red-500" :
    status === "completed" ? "bg-emerald-500" :
    "bg-brand-500";
  return (
    <div className="w-full">
      <div className="flex items-center justify-between text-xs text-slate-400 mb-1">
        <span>Progress</span>
        <span className="font-mono">{pct}%</span>
      </div>
      <div className="h-2 bg-slate-800 rounded-full overflow-hidden">
        <div
          className={clsx("h-full transition-all duration-300", color)}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function StepRow({ step, status, current }: { step: string; status: "completed" | "running" | "pending"; current?: boolean }) {
  return (
    <div className={clsx(
      "flex items-center gap-3 text-sm py-1.5",
      current ? "text-slate-100" : "text-slate-500"
    )}>
      {status === "completed" && <CheckCircle2 className="w-4 h-4 text-emerald-400" />}
      {status === "running" && <Loader2 className="w-4 h-4 text-brand-400 animate-spin" />}
      {status === "pending" && <div className="w-4 h-4 rounded-full border-2 border-slate-700" />}
      <span className="font-mono text-xs">{step}</span>
    </div>
  );
}

export default function SubmitTask() {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [input, setInput] = useState("");
  const [agent, setAgent] = useState("product");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<TaskResponse | null>(null);
  const [progress, setProgress] = useState<LiveProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wsMessages, setWsMessages] = useState<string[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const pollRef = useRef<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const startTimeRef = useRef<number>(0);

  useEffect(() => {
    listAgents().then(setAgents).catch(() => {});
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  // Elapsed timer
  useEffect(() => {
    if (!submitting) {
      setElapsed(0);
      return;
    }
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startTimeRef.current) / 1000));
    }, 200);
    return () => clearInterval(id);
  }, [submitting]);

  const startPolling = (taskId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      try {
        const p = await getTaskProgress(taskId);
        setProgress(p);
        if (p.status === "completed" || p.status === "failed") {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        // ignore 404s
      }
    }, 250);
  };

  const handleSubmit = async () => {
    if (!input.trim()) return;
    setSubmitting(true);
    setError(null);
    setResult(null);
    setProgress(null);
    setWsMessages([]);
    startTimeRef.current = Date.now();
    try {
      const req: TaskRequest = { input, agent };
      const task = await submitTask(req);
      setResult(task);

      // Start polling
      startPolling(task.task_id);

      // Open WebSocket
      const ws = openTaskSocket(
        task.task_id,
        (data) => setWsMessages((m) => [...m, JSON.stringify(data)].slice(-10)),
      );
      wsRef.current = ws;
      setTimeout(() => ws.close(), 3000);
    } catch (e: any) {
      setError(e.response?.data?.error || e.message || "Failed to submit task");
    } finally {
      setSubmitting(false);
    }
  };

  const currentStatus = result?.status || progress?.status || (submitting ? "running" : "");
  const finalStatus = result?.status || (progress?.status === "completed" || progress?.status === "failed" ? progress.status : "");

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-bold text-white">Submit Task</h1>
        <p className="text-sm text-slate-400 mt-1">Send a task to a single agent or the full CEO pipeline</p>
      </header>

      <div className="grid grid-cols-2 gap-6">
        {/* Left: form */}
        <section className="bg-slate-800/50 border border-slate-700 rounded-lg p-5 space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-2">Agent</label>
            <select
              value={agent}
              onChange={(e) => setAgent(e.target.value)}
              disabled={submitting}
              className="w-full bg-slate-900 border border-slate-700 rounded-md px-3 py-2 text-white focus:outline-none focus:border-brand-500"
            >
              {agents.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name} — {a.description.slice(0, 60)}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-300 mb-2">Task input</label>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={submitting}
              rows={6}
              placeholder="Describe the task in natural language..."
              className="w-full bg-slate-900 border border-slate-700 rounded-md px-3 py-2 text-white focus:outline-none focus:border-brand-500 resize-y"
            />
          </div>

          <button
            onClick={handleSubmit}
            disabled={submitting || !input.trim()}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-brand-600 hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-md font-medium"
          >
            {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            {submitting ? "Running..." : "Submit Task"}
          </button>

          <div>
            <div className="text-xs text-slate-500 mb-2">Sample tasks:</div>
            <div className="space-y-1">
              {SAMPLE_TASKS.map((s, i) => (
                <button
                  key={i}
                  onClick={() => {
                    setAgent(s.agent);
                    setInput(s.input);
                  }}
                  className="w-full text-left text-xs text-slate-400 hover:text-brand-400 bg-slate-900/50 px-3 py-2 rounded border border-slate-800 hover:border-slate-700"
                >
                  <span className="font-mono text-brand-500">[{s.agent}]</span> {s.input}
                </button>
              ))}
            </div>
          </div>
        </section>

        {/* Right: live progress + result */}
        <section className="space-y-4">
          {error && (
            <div className="flex items-center gap-2 bg-red-500/10 border border-red-500/30 text-red-300 px-4 py-3 rounded">
              <AlertCircle className="w-4 h-4" /> {error}
            </div>
          )}

          {(submitting || progress) && (
            <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-5 space-y-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Activity className="w-4 h-4 text-brand-400" />
                  <div className="font-mono text-xs text-slate-400">
                    {result?.task_id || "submitting..."}
                  </div>
                </div>
                <div className="flex items-center gap-2 text-xs text-slate-400">
                  <Clock className="w-3 h-3" />
                  {elapsed}s
                </div>
              </div>

              <ProgressBar
                value={progress?.progress ?? 0}
                status={currentStatus}
              />

              {/* Steps */}
              {progress && progress.pending_steps.length > 0 && (
                <div className="border-t border-slate-700/50 pt-3 space-y-1">
                  <div className="text-xs text-slate-500 mb-2">Steps</div>
                  {progress.completed_steps.map((s) => (
                    <StepRow key={s} step={s} status="completed" />
                  ))}
                  {progress.pending_steps.map((s, i) => (
                    <StepRow
                      key={s}
                      step={s}
                      status={i === 0 ? "running" : "pending"}
                      current={i === 0}
                    />
                  ))}
                </div>
              )}

              {progress?.error && (
                <div className="flex items-start gap-2 bg-red-500/10 border border-red-500/30 text-red-300 px-3 py-2 rounded text-sm">
                  <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                  <div>
                    <div className="font-semibold mb-1">Failure</div>
                    <div className="text-xs text-red-200/80">{progress.error}</div>
                  </div>
                </div>
              )}
            </div>
          )}

          {result && finalStatus && !submitting && (finalStatus === "completed" || finalStatus === "failed") && (
            <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-5">
              <div className="flex items-center gap-2 mb-3">
                {finalStatus === "completed" ? (
                  <span className="flex items-center gap-1.5 text-emerald-400 font-semibold">
                    <CheckCircle2 className="w-4 h-4" /> Completed
                  </span>
                ) : (
                  <span className="flex items-center gap-1.5 text-red-400 font-semibold">
                    <AlertCircle className="w-4 h-4" /> Failed
                  </span>
                )}
                <span className="text-xs text-slate-500">in {elapsed}s</span>
              </div>
              {result.error && (
                <div className="text-sm text-red-300 mb-3">{result.error}</div>
              )}
              {result.output && (
                <pre className="text-xs text-slate-300 bg-slate-950 p-3 rounded overflow-auto max-h-96">
                  {JSON.stringify(result.output.payload ?? result.output, null, 2)}
                </pre>
              )}
            </div>
          )}

          {wsMessages.length > 0 && (
            <div className="bg-slate-900 border border-slate-700 rounded-lg p-4">
              <div className="flex items-center gap-2 text-xs text-slate-400 mb-2">
                <Terminal className="w-4 h-4" /> WebSocket stream
              </div>
              <div className="space-y-1">
                {wsMessages.map((m, i) => (
                  <div key={i} className="text-xs font-mono text-slate-500">{m}</div>
                ))}
              </div>
            </div>
          )}

          {!result && !error && !submitting && (
            <div className="bg-slate-800/30 border border-dashed border-slate-700 rounded-lg p-8 text-center text-slate-500">
              Submit a task to see results here
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
