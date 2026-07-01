import { useEffect, useState } from "react";
import { graphStats, graphNode, GraphStats } from "../lib/api";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import { Network, X } from "lucide-react";

const COLORS = ["#0ea5e9", "#8b5cf6", "#10b981", "#f59e0b", "#ec4899", "#06b6d4"];

export default function Graph() {
  const [stats, setStats] = useState<GraphStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [modalNodeId, setModalNodeId] = useState<string | null>(null);
  const [modalData, setModalData] = useState<any>(null);

  const refresh = async () => {
    try {
      const s = await graphStats();
      setStats(s);
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

  const openNode = async (id: string) => {
    setModalNodeId(id);
    setModalData(null);
    try {
      const d = await graphNode(id);
      setModalData(d);
    } catch (e: any) {
      setModalData({ error: e.message });
    }
  };

  if (error) {
    return <div className="text-red-300">{error}</div>;
  }
  if (!stats) {
    return <div className="text-slate-400">Loading...</div>;
  }

  const nodeTypeData = Object.entries(stats.nodes_by_type).map(([type, count]) => ({
    type, count,
  }));
  const linkTypeData = Object.entries(stats.links_by_type).map(([type, count]) => ({
    type, count,
  }));

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-bold text-white flex items-center gap-3">
          <Network className="w-7 h-7 text-brand-500" /> MultiLinkGraph
        </h1>
        <p className="text-sm text-slate-400 mt-1">
          {stats.total_nodes} nodes, {stats.total_links} links across the memory system
        </p>
      </header>

      <div className="grid grid-cols-2 gap-6">
        <section>
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Nodes by type</h2>
          <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-4" style={{ height: 280 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={nodeTypeData} layout="vertical">
                <XAxis type="number" stroke="#94a3b8" />
                <YAxis dataKey="type" type="category" stroke="#94a3b8" width={100} />
                <Tooltip contentStyle={{ background: "#1e293b", border: "1px solid #475569" }} />
                <Bar dataKey="count" fill="#0ea5e9">
                  {nodeTypeData.map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Links by type</h2>
          <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-4" style={{ height: 280 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={linkTypeData} layout="vertical">
                <XAxis type="number" stroke="#94a3b8" />
                <YAxis dataKey="type" type="category" stroke="#94a3b8" width={140} />
                <Tooltip contentStyle={{ background: "#1e293b", border: "1px solid #475569" }} />
                <Bar dataKey="count" fill="#8b5cf6">
                  {linkTypeData.map((_, i) => (
                    <Cell key={i} fill={COLORS[(i + 2) % COLORS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
      </div>

      <section>
        <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Browse nodes</h2>
        <div className="grid grid-cols-4 gap-3">
          {Object.entries(stats.nodes_by_type).map(([type, count]) => (
            <div
              key={type}
              className="bg-slate-800/50 border border-slate-700 rounded-lg p-4"
            >
              <div className="text-xs text-slate-400 uppercase">{type}</div>
              <div className="text-2xl font-bold text-white mt-1">{count}</div>
              <div className="text-xs text-slate-500">nodes</div>
            </div>
          ))}
        </div>
        <p className="text-xs text-slate-500 mt-3">
          Click a task ID in the <a href="/tasks" className="text-brand-400 hover:underline">Tasks</a> page to view it here.
        </p>
      </section>

      {modalNodeId && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center p-8 z-50" onClick={() => setModalNodeId(null)}>
          <div className="bg-slate-900 border border-slate-700 rounded-lg max-w-2xl w-full max-h-[80vh] overflow-auto p-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-4">
              <div className="font-mono text-sm text-brand-400">{modalNodeId}</div>
              <button onClick={() => setModalNodeId(null)} className="text-slate-400 hover:text-white">
                <X className="w-5 h-5" />
              </button>
            </div>
            {modalData ? (
              <pre className="text-xs text-slate-300 bg-slate-950 p-3 rounded overflow-auto">
                {JSON.stringify(modalData, null, 2)}
              </pre>
            ) : (
              <div className="text-slate-400">Loading node details...</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
