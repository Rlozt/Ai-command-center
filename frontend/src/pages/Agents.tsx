import { api } from "../api";
import { Card, DemoBanner, Empty, ErrorBanner, Loading, Money, StatusPill, timeAgo, AGENTS } from "../components/ui";
import { usePoll } from "../hooks";
import type { AgentInfo, CallRow, Provider } from "../types";

export function Agents() {
  const agents = usePoll<AgentInfo[]>(() => api("/api/agents"), 2500);
  const calls = usePoll<CallRow[]>(() => api("/api/calls?limit=40"), 4000);
  if (agents.loading && !agents.data) return <Loading label="Loading agents…" />;
  const demo = agents.data?.some((a) => a.demo) ? "demo" : "live";
  return (
    <div className="stack">
      <h1>Agent Activity</h1>
      <DemoBanner mode={demo} />
      {agents.error && <ErrorBanner message={agents.error} onRetry={agents.reload} />}
      <div className="grid-3">
        {agents.data?.map((a) => {
          const meta = AGENTS[a.provider as Provider];
          return (
            <Card key={a.provider} title={`${meta.name} · ${meta.role}`} aside={<StatusPill value={a.state} />}>
              <p className="muted small">{a.demo ? "Demo mock provider" : `Model: ${a.model}`} · {a.detail}</p>
              <h3>Working on</h3>
              {a.running.length ? <ul className="plain">{a.running.map((r) => <li key={r.id}><a href={`#/tasks/${r.task_id}`}>{r.title}</a></li>)}</ul> : <p className="muted small">Nothing right now.</p>}
              {a.queued.length > 0 && <><h3>Queued</h3><ul className="plain">{a.queued.map((r) => <li key={r.id} className="small">{r.title}</li>)}</ul></>}
              <h3>Recent results</h3>
              {a.recent_results.length ? <ul className="plain small">{a.recent_results.map((c) => <li key={c.id}>{c.purpose} · {timeAgo(c.at)}</li>)}</ul> : <p className="muted small">None yet.</p>}
              {a.last_error && <div className="banner banner-bad small" role="alert">Last error ({a.last_error.error_kind}): {a.last_error.error}</div>}
              <p className="small">Today: {a.usage.calls_today} calls · {a.usage.input_tokens_today}/{a.usage.output_tokens_today} tokens · <Money usd={a.usage.spent_today_usd} demo={a.demo} /></p>
            </Card>
          );
        })}
      </div>
      <Card title="Recent model calls">
        {!calls.data || calls.data.length === 0 ? <Empty title="No calls yet" /> : (
          <table className="table">
            <thead><tr><th>When</th><th>Agent</th><th>Purpose</th><th>Status</th><th>Tokens</th><th>Cost</th></tr></thead>
            <tbody>{calls.data.map((c) => <tr key={c.id}><td className="muted">{timeAgo(c.at)}</td><td>{c.provider}</td><td>{c.purpose}</td><td><StatusPill value={c.status} />{c.error_kind && <span className="muted small"> {c.error_kind}</span>}</td>
              <td>{c.input_tokens ?? "?"}/{c.output_tokens ?? "?"}</td><td>{c.billable ? `$${(c.cost_usd ?? 0).toFixed(4)}` : "demo"}</td></tr>)}</tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
