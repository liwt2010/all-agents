import { useEffect, useState } from "react";
import { getMetrics, MetricsResponse } from "../lib/api";
import { Loader2, Activity } from "lucide-react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";

function formatValue(value: number, unit: string): string {
  if (unit === "ratio") return (value * 100).toFixed(2) + "%";
  if (unit === "usd") return "$" + value.toFixed(4);
  if (unit === "seconds") return value.toFixed(2) + "s";
  return value.toFixed(2);
}

export default function Metrics() {
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [history, setHistory] = useState<Record<string, { tick: number; value: number }[]>>({});
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetch = async () => {
      try {
        const m = await getMetrics();
        setMetrics(m);
        setError(null);
        setHistory((h) => {
          const next = { ...h };
          for (const [name, mv] of Object.entries(m.metrics)) {
            const arr = next[name] ? [...next[name]] : [];
            arr.push({ tick, value: mv.value });
            if (arr.length > 30) arr.shift();
            next[name] = arr;
          }
          return next;
        });
        setTick((t) => t + 1);
      } catch (e: any) {
        setError(e.message || "Failed");
      }
    };
    fetch();
    const id = setInterval(fetch, 3000);
    return () => clearInterval(id);
  }, []);

  if (error) return <div className="text-red-300">{error}</div>;
  if (!metrics) return <div className="flex items-center gap-2 text-slate-400"><Loader2 className="w-4 h-4 animate-spin" /> Loading...</div>;

  const names = Object.keys(metrics.metrics);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-bold text-white flex items-center gap-3">
          <Activity className="w-7 h-7 text-brand-500" /> Metrics
        </h1>
        <p className="text-sm text-slate-400 mt-1">
          9 auto-calculated metrics from the MultiLinkGraph, refreshed every 3s
        </p>
      </header>

      <div className="grid grid-cols-3 gap-4">
        {names.map((name) => {
          const m = metrics.metrics[name];
          const hist = history[name] || [];
          return (
            <div key={name} className="bg-slate-800/50 border border-slate-700 rounded-lg p-4">
              <div className="flex items-center justify-between mb-2">
                <div className="text-xs text-slate-400 uppercase tracking-wide">
                  {name.replace(/_/g, " ")}
                </div>
                <div className="text-xs text-slate-500">{m.unit}</div>
              </div>
              <div className="text-2xl font-bold text-white mb-3">
                {formatValue(m.value, m.unit)}
              </div>
              <div style={{ height: 80 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={hist}>
                    <Line type="monotone" dataKey="value" stroke="#0ea5e9" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                    <Tooltip
                      contentStyle={{ background: "#1e293b", border: "1px solid #475569", fontSize: 11 }}
                      labelStyle={{ color: "#94a3b8" }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
