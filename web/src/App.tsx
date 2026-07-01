import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import SubmitTask from "./pages/SubmitTask";
import Tasks from "./pages/Tasks";
import Graph from "./pages/Graph";
import Metrics from "./pages/Metrics";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/submit" element={<SubmitTask />} />
        <Route path="/tasks" element={<Tasks />} />
        <Route path="/graph" element={<Graph />} />
        <Route path="/metrics" element={<Metrics />} />
        <Route path="*" element={<Dashboard />} />
      </Route>
    </Routes>
  );
}
