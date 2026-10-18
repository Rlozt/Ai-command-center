import { api } from "../api";
import { Card, DemoBanner, Empty, ErrorBanner, Loading, Money, StatusPill, agentName, AGENTS, timeAgo } from "../components/ui";
import { TaskInput } from "../components/TaskInput";
import { usePoll } from "../hooks";
import type { Dashboard as DashboardData, Project, Provider, TaskBrief } from "../types";

function TaskRow({ t }: { t: TaskBrief }) {
  return (
    <li className="row">
      <a href={`#/tasks/${t.id}`} className="row-main">
        <span className="row-title">{t.title}</span>
        <span className="muted small">
          {t.subtasks_done}/{t.subtasks_total} subtasks · {t.agents.map(agentName).join(", ") || "planning"} · {timeAgo(t.updated_at)}
        </span>
      </a>
      <span className="row-side">
        {t.paused ? <StatusPill value="paused" /> : <StatusPill value={t.status} />}
      </span>
    </li>
  );
}

export function Dashboard() {
  const dash = usePoll<DashboardData>(() => api("/api/dashboard"), 2500);
  const projects = usePoll<Project[]>(() => api("/api/projects"), 0);

  if (dash.loading && !dash.data) return <Loading label="Loading dashboard…" />;
  if (!dash.data) return <ErrorBanner message={dash.error ?? "Could not load the dashboard"} onRetry={dash.reload} />;
  const d = dash.data;
  const u = d.usage;

  return (
    <div className="stack">
      <h1>Dashboard</h1>
      <DemoBanner mode={d.mode} />
      {dash.error && <ErrorBanner message={`Connection problem: ${dash.error}. Showing the last known state.`} onRetry={dash.reload} />}
      <TaskInput projects={projects.data ?? []} onCreated={(id) => (window.location.hash = `#/tasks/${id}`)} />

      <div className="grid-3">
        {d.providers.map((p) => {
          const meta = AGENTS[p.provider as Provider];
          const working = d.assignments.filter((a) => a.agent === p.provider);
          const state = !p.enabled ? "disabled" : !p.configured ? "not_configured" : working.length ? "working" : "idle";
          return (
            <Card key={p.provider} title={`${meta.name} · ${meta.role}`} aside={<StatusPill value={state} />}>
              {working.length ? (
                <ul className="plain">
                  {working.map((a) => (
                    <li key={a.id}>
                      <a href={`#/tasks/${a.task_id}`}>{a.title}</a>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted small">{p.detail || "No active assignment."}</p>
              )}
              <p className="muted small">
                {p.demo ? "demo mock" : p.model} · {u.providers[p.provider]?.calls_today ?? 0} calls today
              </p>
            </Card>
          );
        })}
      </div>

      <div className="grid-2">
        <Card title="Current tasks">
          {d.current_tasks.length === 0 ? (
            <Empty title="Nothing running">Describe a task above and the coordinator will plan it and assign the work.</Empty>
          ) : (
            <ul className="rows">{d.current_tasks.map((t) => <TaskRow key={t.id} t={t} />)}</ul>
          )}
        </Card>
        <Card title="Pending approvals" aside={<a href="#/approvals">Review all</a>}>
          {d.pending_approvals.length === 0 ? (
            <Empty title="No approvals waiting">Proposed file changes appear here and are never applied without your approval.</Empty>
          ) : (
            <ul className="rows">
              {d.pending_approvals.map((p) => (
                <li className="row" key={p.id}>
                  <a className="row-main" href="#/approvals">
                    <span className="row-title">{p.task_title}</span>
                    <span className="muted small">
                      v{p.version} · {p.files.length} file(s){p.unresolved_issues ? " · unresolved issues flagged" : ""}
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Needs attention">
          {d.needs_attention.length === 0 && d.recent_errors.length === 0 ? (
            <Empty title="No errors">Failures and provider errors will show up here.</Empty>
          ) : (
            <ul className="rows">
              {d.needs_attention.map((t) => <TaskRow key={t.id} t={t} />)}
              {d.recent_errors.slice(0, 4).map((e) => (
                <li key={e.id} className="row">
                  <span className="row-main">
                    <span className="row-title small">{e.message}</span>
                    <span className="muted small">{e.agent || "coordinator"} · {timeAgo(e.at)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Recently completed">
          {d.recently_completed.length === 0 ? <Empty title="Nothing completed yet" /> : <ul className="rows">{d.recently_completed.map((t) => <TaskRow key={t.id} t={t} />)}</ul>}
        </Card>
      </div>

      <Card title="Usage and estimated cost">
        <div className="stats">
          <div>
            <span className="stat-n">${u.spent_today_usd.toFixed(4)}</span>
            <span className="muted small">spent today (limit ${u.daily_limit_usd.toFixed(2)})</span>
          </div>
          <div>
            <span className="stat-n">${u.spent_total_usd.toFixed(4)}</span>
            <span className="muted small">all time</span>
          </div>
          <div>
            <span className="stat-n">{u.demo_calls_today}</span>
            <span className="muted small">demo calls today (not billable)</span>
          </div>
        </div>
        <table className="table">
          <thead>
            <tr><th>Agent</th><th>Calls today</th><th>Tokens in / out</th><th>Spent</th><th>Daily limit</th></tr>
          </thead>
          <tbody>
            {Object.entries(u.providers).map(([name, v]) => (
              <tr key={name}>
                <td>{agentName(name)}</td>
                <td>{v.calls_today}</td>
                <td>{v.input_tokens_today} / {v.output_tokens_today}</td>
                <td><Money usd={v.spent_today_usd} demo={d.mode === "demo"} /></td>
                <td>{v.limit_usd ? `$${v.limit_usd.toFixed(2)}` : "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">{u.note}</p>
      </Card>
    </div>
  );
}
