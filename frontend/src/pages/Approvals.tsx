import { useState } from "react";
import { api, post, put } from "../api";
import { DiffView } from "../components/DiffView";
import { Card, Empty, ErrorBanner, Loading, StatusPill, agentName } from "../components/ui";
import { usePoll } from "../hooks";
import type { Proposal } from "../types";

export function ProposalCard({ p, onChanged }: { p: Proposal; onChanged: () => void }) {
  const [note, setNote] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ path: string; content: string } | null>(null);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function startEdit(path: string) {
    try {
      const r = await api<{ content: string }>(`/api/proposals/${p.id}/file?path=${encodeURIComponent(path)}`);
      setEditing({ path, content: r.content });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Card title={<>{p.task_title ?? p.title} <span className="muted small">· proposal v{p.version} by {agentName(p.author_agent)}</span></>}
      aside={<a href={`#/tasks/${p.task_id}`}>Open task</a>}>
      {p.summary && <p>{p.summary}</p>}
      {p.unresolved_issues > 0 && (
        <div className="banner banner-warn" role="alert">
          This proposal has unresolved review issues or failing checks. Read the task's review and test results before approving.
        </div>
      )}
      {p.files.map((f) => (
        <details key={f.path} open className="file">
          <summary>
            <code>{f.path}</code> <StatusPill value={f.action === "create" ? "ok" : "unverified"} label={f.action === "create" ? "new file" : "modifies existing file"} />
          </summary>
          <DiffView diff={f.diff} />
          <button className="btn btn-ghost" onClick={() => startEdit(f.path)}>Edit this file (creates a new version)</button>
        </details>
      ))}
      {editing && (
        <div className="edit">
          <label>
            <span className="small muted">Editing {editing.path}. Saving creates version {p.version + 1}, which needs a fresh approval.</span>
            <textarea rows={12} value={editing.content} onChange={(e) => setEditing({ ...editing, content: e.target.value })} />
          </label>
          <div className="actions">
            <button className="btn" disabled={busy} onClick={() => run(async () => { await put(`/api/proposals/${p.id}/files`, editing); setEditing(null); })}>Save as new version</button>
            <button className="btn btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </div>
      )}
      <label className="field">
        <span className="small muted">Note (required context if you request a revision)</span>
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note or revision request" />
      </label>
      {p.unresolved_issues > 0 && (
        <label className="check">
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I have read the unresolved issues and still want to approve
        </label>
      )}
      {error && <ErrorBanner message={error} />}
      <div className="actions">
        <button className="btn btn-primary" disabled={busy || (p.unresolved_issues > 0 && !ack)}
          onClick={() => run(() => post(`/api/proposals/${p.id}/approve`, { content_hash: p.content_hash, note, acknowledge_issues: ack }))}>
          Approve and apply
        </button>
        <button className="btn" disabled={busy} onClick={() => run(() => post(`/api/proposals/${p.id}/reject`, { note, request_revision: true }))}>
          Reject and request revision
        </button>
        <button className="btn btn-ghost" disabled={busy} onClick={() => run(() => post(`/api/proposals/${p.id}/reject`, { note, request_revision: false }))}>
          Reject and cancel task
        </button>
      </div>
      <p className="muted small">Approval applies exactly version {p.version} ({p.content_hash.slice(0, 8)}). If the proposal changes, you will be asked again.</p>
    </Card>
  );
}

export function Approvals() {
  const list = usePoll<Proposal[]>(() => api("/api/approvals"), 3000);
  if (list.loading && !list.data) return <Loading label="Loading approvals…" />;
  return (
    <div className="stack">
      <h1>Approvals</h1>
      {list.error && <ErrorBanner message={list.error} onRetry={list.reload} />}
      {list.data && list.data.length === 0 && <Empty title="Nothing to approve">When an agent proposes file changes they are staged here with a readable diff. Nothing is written to your project until you approve.</Empty>}
      {list.data?.map((p) => <ProposalCard key={p.id} p={p} onChanged={list.reload} />)}
    </div>
  );
}
