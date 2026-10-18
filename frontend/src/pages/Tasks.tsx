import { useState } from "react";
import { api } from "../api";
import { Empty, ErrorBanner, Loading, Money, StatusPill, agentName, timeAgo } from "../components/ui";
import { usePoll } from "../hooks";
import type { TaskBrief } from "../types";

const FILTERS = [["", "All"], ["running,planning,ready,reviewing,repairing,applying_changes,pending", "Active"], ["awaiting_approval", "Awaiting approval"], ["completed", "Completed"], ["failed,cancelled", "Failed / cancelled"]];

export function Tasks() {
  const [filter, setFilter] = useState("");
  const tasks = usePoll<TaskBrief[]>(() => api(`/api/tasks${filter ? `?status=${filter}` : ""}`), 3000, [filter]);
  return (
    <div className="stack">
      <h1>Tasks</h1>
      <div className="tabs" role="tablist">
        {FILTERS.map(([v, label]) => (
          <button key={label} role="tab" aria-selected={filter === v} className={filter === v ? "tab active" : "tab"} onClick={() => setFilter(v)}>
            {label}
          </button>
        ))}
      </div>
      {tasks.loading && !tasks.data && <Loading />}
      {tasks.error && <ErrorBanner message={tasks.error} onRetry={tasks.reload} />}
      {tasks.data && tasks.data.length === 0 && <Empty title="No tasks here">Create one from the dashboard.</Empty>}
      {tasks.data && tasks.data.length > 0 && (
        <table className="table">
          <thead><tr><th>Task</th><th>Status</th><th>Progress</th><th>Agents</th><th>Cost</th><th>Updated</th></tr></thead>
          <tbody>
            {tasks.data.map((t) => (
              <tr key={t.id}>
                <td><a href={`#/tasks/${t.id}`}>{t.title}</a></td>
                <td>{t.paused ? <StatusPill value="paused" /> : <StatusPill value={t.status} />}{t.outcome === "partial" && <> <StatusPill value="unverified" label="partial" /></>}</td>
                <td>{t.subtasks_done}/{t.subtasks_total}</td>
                <td>{t.agents.map(agentName).join(", ")}</td>
                <td><Money usd={t.cost} demo={t.demo} /></td>
                <td className="muted">{timeAgo(t.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
