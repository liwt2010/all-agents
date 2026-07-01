import { NavLink, Outlet } from "react-router-dom";
import {
  LayoutDashboard,
  Send,
  ListTodo,
  Network,
  Activity,
  Bot,
} from "lucide-react";
import clsx from "clsx";

const nav = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/submit", label: "Submit Task", icon: Send },
  { to: "/tasks", label: "Tasks", icon: ListTodo },
  { to: "/graph", label: "Graph", icon: Network },
  { to: "/metrics", label: "Metrics", icon: Activity },
];

export default function Layout() {
  return (
    <div className="flex min-h-screen bg-slate-900 text-slate-200">
      {/* Sidebar */}
      <aside className="w-60 bg-slate-950 border-r border-slate-800 flex flex-col">
        <div className="px-5 py-5 flex items-center gap-2 border-b border-slate-800">
          <Bot className="w-6 h-6 text-brand-500" />
          <div>
            <div className="font-bold text-white text-lg">Agent System</div>
            <div className="text-xs text-slate-500">v0.1.0</div>
          </div>
        </div>
        <nav className="flex-1 px-2 py-3 space-y-1">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                clsx(
                  "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors",
                  isActive
                    ? "bg-brand-600/20 text-brand-100 font-medium"
                    : "text-slate-400 hover:bg-slate-800 hover:text-slate-100",
                )
              }
            >
              <item.icon className="w-4 h-4" />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-3 text-xs text-slate-600 border-t border-slate-800">
          <a
            href="http://localhost:8000/docs"
            target="_blank"
            rel="noreferrer"
            className="hover:text-slate-400"
          >
            API docs (Swagger) ↗
          </a>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        <div className="max-w-7xl mx-auto p-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
