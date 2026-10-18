import { useState } from "react";
import { api, patch, post } from "../api";
import { DiffView } from "../components/DiffView";
import { Card, DemoBanner, Empty, ErrorBanner, Loading, Money, StatusPill, agentName, timeAgo } from "../components/ui";
import { usePoll } from "../hooks";
import type { Claim, TaskDetailData } from "../types";
import { ProposalCard } from "./Approvals";

const TABS = ["Overview", "Plan and subtasks", "Research", "Changes and tests", "Logs and usage"] as const;
const TERMINAL = ["completed", "cancelled"];

function Controls({ d, reload }: { d: TaskDetailData; reload: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [clar, setClar] = useState("");
  const t = d.task;
  async function act(path: string, body?: unknown) {
    setBusy(true);
    setErr(null);
    try {
      await post(`/api/tasks/${t.id}/${path}`, body);
      reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  const active = !TERMINAL.includes(t.status) && t.status !== "failed" && t.status !== "awaiting_approval";
  const needsInput = t.pause_reason.startsWith("needs input");
  return (
    <div>
      <div className="actions">
        {active && !t.paused && <button className="btn" disabled={busy} onClick={() => act("pause")}>Pause</button>}
        {active && t.paused && <button className="btn btn-primary" disabled={busy} onClick={() => act("resume", { clarification: clar })}>Resume</button>}
        {t.status === "failed" && <button className="btn btn-primary" disabled={busy} onClick={() => act("retry")}>Retry failed work</button>}
        {!TERMINAL.includes(t.status) && <button className="btn btn-danger" disabled={busy} onClick={() => act("cancel")}>Cancel</button>}
      </div>
      {t.paused && (
        <div className="banner banner-warn" role="status">
          <div>
            <strong>Paused.</strong> {t.pause_reason || "Paused by user."}
            {t.pause_reason.startsWith("budget") && <> <a href="#/settings">Open limits</a></>}
          </div>
          {needsInput && (
            <label className="field"><span className="small muted">Your answer (added to the request)</span>
              <input value={clar} onChange={(e) => setClar(e.target.value)} /></label>
          )}
        </div>
      )}
      {err && <ErrorBanner message={err} />}
    </div>
  );
}

function ClaimRow({ c, reload }: { c: Claim; reload: () => void }) {
  async function mark(status: string) {
    await patch(`/api/claims/${c.id}`, { status, note: "set by user" });
    reload();
  }
  return (
    <li className="claim">
      <div className="claim-head">
        <StatusPill value={c.status} />
        <span className="muted small">{c.support === "source" ? "stated by a source" : "agent interpretation"}</span>
      </div>
      <p>{c.text}</p>
      {c.note && <p className="muted small">{c.note}</p>}
      {c.source_urls.length > 0 && <p className="small">{c.source_urls.map((u) => <a key={u} href={u} target="_blank" rel="noreferrer noopener">{u}</a>)}</p>}
      <label className="small muted">Mark as&nbsp;
        <select value={c.status} onChange={(e) => mark(e.target.value)} aria-label="Claim status">
          {["verified", "unverified", "conflicting", "outdated"].map((s) => <option key={s}>{s}</option>)}
        </select>
      </label>
    </li>
  );
}

export function TaskDetail({ id }: { id: string }) {
  const q = usePoll<TaskDetailData>(() => api(`/api/tasks/${id}`), 2000, [id]);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Overview");
  if (q.loading && !q.data) return <Loading label="Loading task…" />;
  if (!q.data) return <ErrorBanner message={q.error ?? "Task not found"} onRetry={q.reload} />;
  const d = q.data;
  const t = d.task;
  const report = t.report as Record<string, any>;
  const pending = d.proposals.filter((p) => p.status === "pending");

  return (
    <div className="stack">
      <p><a href="#/tasks">← All tasks</a></p>
      <div className="title-row">
        <h1>{t.title}</h1>
        <div>{t.paused ? <StatusPill value="paused" /> : <StatusPill value={t.status} />}{t.outcome && <> <StatusPill value={t.outcome === "success" ? "success" : "unverified"} label={t.outcome.replace(/_/g, " ")} /></>}</div>
      </div>
      <DemoBanner mode={d.usage.demo ? "demo" : "live"} />
      {q.error && <ErrorBanner message={`Connection problem: ${q.error}`} onRetry={q.reload} />}
      {t.error && <div className="banner banner-bad" role="alert">{t.error}</div>}
      <Controls d={d} reload={q.reload} />

      <div className="tabs" role="tablist">
        {TABS.map((x) => <button key={x} role="tab" aria-selected={tab === x} className={tab === x ? "tab active" : "tab"} onClick={() => setTab(x)}>{x}</button>)}
      </div>

      {tab === "Overview" && (
        <>
          <Card title="Original request"><p className="pre">{t.request}</p></Card>
          {pending.map((p) => <ProposalCard key={p.id} p={{ ...p, task_title: t.title }} onChanged={q.reload} />)}
          <Card title="Result">
            {!report.headline ? <Empty title="Not finished yet">The final report appears when the task stops or finishes. Live progress is in the other tabs.</Empty> : (
              <div className="stack-sm">
                <p><strong>{report.headline}</strong></p>
                <p className="muted">{report.cost_statement}</p>
                {report.failed_checks?.length > 0 && <div className="banner banner-bad"><strong>Failed checks:</strong> {report.failed_checks.join("; ")}</div>}
                {report.unresolved_issues?.length > 0 && <div className="banner banner-warn"><strong>Unresolved issues:</strong><ul>{report.unresolved_issues.map((i: string) => <li key={i}>{i}</li>)}</ul></div>}
                {report.problem_subtasks?.length > 0 && <div className="banner banner-bad"><strong>Not completed:</strong><ul>{report.problem_subtasks.map((s: any) => <li key={s.title}>{s.title}: {s.error || s.status}</li>)}</ul></div>}
                {report.required_approvals?.length > 0 && <p>Awaiting your approval: {report.required_approvals.map((a: any) => `v${a.version} (${a.files.join(", ")})`).join("; ")}</p>}
                {report.deliverables?.map((x: any, i: number) => (
                  <details key={i} open={x.kind === "synthesis" || x.kind === "document"} className="file">
                    <summary>{x.subtask} <span className="muted small">· {agentName(x.agent)} · {x.kind}</span></summary>
                    {x.content ? <pre className="pre-block">{x.content}</pre> : <p>{x.summary}</p>}
                    {x.recommendation && <p><strong>Recommendation:</strong> {x.recommendation}</p>}
                    {x.no_web_access && <p className="muted small">This research ran without web access; nothing here is sourced.</p>}
                  </details>
                ))}
                <p className="muted small">Agents: {Object.entries(report.agents ?? {}).map(([k, v]: [string, any]) => `${agentName(k)} (${v.calls} calls)`).join(", ")}</p>
              </div>
            )}
          </Card>
        </>
      )}

      {tab === "Plan and subtasks" && (
        <>
          <Card title="Plan">
            {t.plan.summary ? <p>{t.plan.summary}</p> : <Empty title="No plan yet" />}
            {t.plan.assumptions && t.plan.assumptions.length > 0 && <><h3>Assumptions</h3><ul>{t.plan.assumptions.map((a) => <li key={a}>{a}</li>)}</ul></>}
          </Card>
          <Card title="Subtasks">
            {d.subtasks.length === 0 ? <Empty title="No subtasks yet" /> : (
              <table className="table">
                <thead><tr><th>#</th><th>Subtask</th><th>Kind</th><th>Agent</th><th>Status</th><th>Depends on</th></tr></thead>
                <tbody>
                  {d.subtasks.map((s) => (
                    <tr key={s.id}>
                      <td>{s.idx + 1}</td>
                      <td><strong>{s.title}</strong><div className="muted small">{s.objective}</div>
                        {s.acceptance_criteria.length > 0 && <ul className="small muted">{s.acceptance_criteria.map((c) => <li key={c}>{c}</li>)}</ul>}
                        {s.error && <div className="small bad-text">{s.error}</div>}
                        {s.routing_note && s.routing_note !== "default role" && <div className="small warn-text">{s.routing_note}</div>}</td>
                      <td>{s.kind}</td>
                      <td>{agentName(s.agent)}</td>
                      <td><StatusPill value={s.status} />{s.attempts > 1 && <div className="muted small">attempt {s.attempts}</div>}</td>
                      <td>{s.depends_on.map((x) => d.subtasks.find((y) => y.id === x)?.idx).filter((x) => x !== undefined).map((x) => (x as number) + 1).join(", ") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
          <Card title="Review findings">
            {d.reviews.length === 0 ? <Empty title="No reviews yet" /> : d.reviews.map((r) => (
              <div key={r.id} className="review">
                <p><StatusPill value={r.verdict} /> round {r.round} · reviewed by <strong>{agentName(r.reviewer)}</strong> (author: {agentName(r.author)})</p>
                <p className="muted">{r.summary}</p>
                <ul>{r.issues.map((i, k) => <li key={k}><StatusPill value={i.severity === "minor" ? "unverified" : "failed"} label={i.severity} /> {i.description}<div className="muted small">Evidence: {i.evidence}</div></li>)}</ul>
              </div>
            ))}
          </Card>
          <Card title="State history">
            <ol className="plain">{d.events.map((e) => <li key={e.id} className="small"><span className="muted">{timeAgo(e.at)}</span> {e.from_state ?? "—"} → <strong>{e.to_state}</strong> {e.reason && <span className="muted">· {e.reason}</span>}</li>)}</ol>
          </Card>
        </>
      )}

      {tab === "Research" && (
        <>
          <Card title="Claims">
            {d.claims.length === 0 ? <Empty title="No research claims">Research tasks record each claim with its verification status.</Empty> :
              <ul className="plain claims">{d.claims.map((c) => <ClaimRow key={c.id} c={c} reload={q.reload} />)}</ul>}
          </Card>
          <Card title="Sources">
            {d.sources.length === 0 ? <Empty title="No sources" /> : (
              <table className="table">
                <thead><tr><th>Source</th><th>Published</th><th>Retrieved</th><th>Origin</th><th>Check</th></tr></thead>
                <tbody>
                  {d.sources.map((s) => (
                    <tr key={s.id}>
                      <td><a href={s.url} target="_blank" rel="noreferrer noopener">{s.title || s.url}</a><div className="muted small">{s.domain}</div>
                        {s.excerpt && <blockquote className="small">{s.excerpt}</blockquote>}</td>
                      <td>{s.published ?? s.last_updated ?? "unknown"}</td>
                      <td className="small">{s.retrieved_at.slice(0, 10)}</td>
                      <td><StatusPill value={s.origin === "provider" ? "ok" : "unverified"} label={s.origin === "provider" ? "from search API" : "written by model"} /></td>
                      <td><StatusPill value={s.check_status} /><div className="muted small">{s.check_note}</div></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}

      {tab === "Changes and tests" && (
        <>
          <Card title="Proposed changes">
            {d.proposals.length === 0 ? <Empty title="No proposed file changes" /> : d.proposals.map((p) => (
              <details key={p.id} open={p.status === "pending"} className="file">
                <summary>Version {p.version} <StatusPill value={p.status === "applied" ? "completed" : p.status === "pending" ? "awaiting_approval" : p.status === "rejected" ? "failed" : "cancelled"} label={p.status} /> · {p.files.length} file(s)</summary>
                {p.files.map((f) => <div key={f.path}><code>{f.path}</code> <span className="muted small">({f.action})</span><DiffView diff={f.diff} /></div>)}
              </details>
            ))}
          </Card>
          <Card title="Test results">
            {d.tests.length === 0 ? <Empty title="No tests run">Tests run only for implementation tasks, using the test command you set for the project.</Empty> : d.tests.map((x) => (
              <details key={x.id} open={x.status !== "passed"} className="file"><summary><StatusPill value={x.status} /> <code>{x.command || "(none configured)"}</code></summary><pre className="pre-block">{x.output}</pre></details>
            ))}
          </Card>
        </>
      )}

      {tab === "Logs and usage" && (
        <>
          <Card title="Usage"><p>{d.usage.calls} calls · {d.usage.input_tokens} tokens in / {d.usage.output_tokens} out · <Money usd={d.usage.cost_usd} demo={d.usage.demo} local={d.usage.local_calls + d.usage.cloud_calls > 0 && d.usage.billable_calls === 0} /></p></Card>
          <Card title="Model calls">
            {d.calls.length === 0 ? <Empty title="No model calls yet" /> : (
              <table className="table">
                <thead><tr><th>Agent</th><th>Purpose</th><th>Status</th><th>Tokens</th><th>Cost</th></tr></thead>
                <tbody>{d.calls.map((c) => <tr key={c.id}><td>{agentName(c.provider)}<div className="muted small">{c.model}</div></td><td>{c.purpose}</td>
                  <td><StatusPill value={c.status} />{c.error && <div className="small bad-text">{c.error}</div>}</td><td>{c.input_tokens ?? "?"} / {c.output_tokens ?? "?"}</td>
                  <td>{c.billable ? `$${(c.cost_usd ?? 0).toFixed(4)} (${c.cost_basis})` : c.cost_basis === "local" ? "local · free" : c.cost_basis === "ollama_cloud" ? "Ollama cloud · not billed here" : "demo"}</td></tr>)}</tbody>
              </table>
            )}
          </Card>
          <Card title="Log">
            <ul className="plain log">{d.logs.map((l) => <li key={l.id} className={`small log-${l.level}`}><span className="muted">{l.at.slice(11, 19)}</span> <strong>{l.agent || "coordinator"}</strong> {l.message}</li>)}</ul>
          </Card>
        </>
      )}
    </div>
  );
}
