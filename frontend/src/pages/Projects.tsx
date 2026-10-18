import { useState } from "react";
import { api, del, patch, post, sessionToken } from "../api";
import { Card, Empty, ErrorBanner, Loading, StatusPill } from "../components/ui";
import { usePoll } from "../hooks";
import type { MemoryItem, Project } from "../types";

const KINDS = ["instruction", "preference", "decision", "finding", "limitation", "file_ref", "outstanding", "config"];

function ProjectPanel({ p, reload }: { p: Project; reload: () => void }) {
  const mem = usePoll<MemoryItem[]>(() => api(`/api/projects/${p.id}/memory`), 0, [p.id]);
  const [form, setForm] = useState({ name: p.name, root_dir: p.root_dir, test_command: p.test_command, instructions: p.instructions });
  const [item, setItem] = useState({ kind: "decision", title: "", body: "", status: "confirmed" });
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setErr(null);
    try {
      await patch(`/api/projects/${p.id}`, form);
      setMsg("Saved.");
      reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }
  async function addItem() {
    setErr(null);
    try {
      await post(`/api/projects/${p.id}/memory`, item);
      setItem({ ...item, title: "", body: "" });
      mem.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }
  async function exportMarkdown() {
    const r = await fetch(`/api/projects/${p.id}/export?format=markdown`, { headers: { "X-ACC-Token": await sessionToken() } });
    const blob = new Blob([await r.text()], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "PROJECT.md";
    a.click();
  }
  async function exportAll() {
    const data = await api(`/api/projects/${p.id}/export`);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${p.name.replace(/\W+/g, "-")}-memory.json`;
    a.click();
  }

  return (
    <div className="stack">
      <Card title={p.name}>
        <label className="field"><span>Name</span><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
        <label className="field"><span>Permitted directory (the only folder agents can read; changes are staged and applied here only after approval)</span>
          <input value={form.root_dir} onChange={(e) => setForm({ ...form, root_dir: e.target.value })} placeholder="C:\\Users\\you\\projects\\planner" /></label>
        <label className="field"><span>Test command (you choose it; agents cannot; runs in an isolated copy)</span>
          <input value={form.test_command} onChange={(e) => setForm({ ...form, test_command: e.target.value })} placeholder="python -m pytest -q" /></label>
        <label className="field"><span>PROJECT.md: standing instructions for every agent</span>
          <textarea rows={8} value={form.instructions} onChange={(e) => setForm({ ...form, instructions: e.target.value })} /></label>
        {err && <ErrorBanner message={err} />}
        {msg && <p className="muted" role="status">{msg}</p>}
        <div className="actions"><button className="btn btn-primary" onClick={save}>Save project</button>
          <button className="btn" onClick={exportAll}>Export memory (JSON)</button>
          <button className="btn" onClick={exportMarkdown}>Export PROJECT.md</button></div>
      </Card>
      <Card title="Project memory">
        <p className="muted small">Confirmed items are given to agents as facts. Hypotheses and suggestions are never presented as facts. Nothing a model says is saved automatically.</p>
        {mem.loading && !mem.data && <Loading />}
        {mem.data && mem.data.length === 0 && <Empty title="No memory yet">Add decisions, preferences and findings you want agents to remember.</Empty>}
        <ul className="plain mem">
          {mem.data?.map((m) => (
            <li key={m.id}>
              <div className="claim-head"><StatusPill value={m.status === "confirmed" ? "verified" : "unverified"} label={m.status} /><span className="muted small">{m.kind} · from {m.source}</span></div>
              <strong>{m.title}</strong><p>{m.body}</p>
              <div className="actions">
                {m.status !== "confirmed" && <button className="btn btn-ghost" onClick={async () => { await patch(`/api/memory/${m.id}`, { status: "confirmed" }); mem.reload(); }}>Confirm</button>}
                <button className="btn btn-ghost" onClick={async () => { await del(`/api/memory/${m.id}`); mem.reload(); }}>Delete</button>
              </div>
            </li>
          ))}
        </ul>
        <h3>Add item</h3>
        <div className="form-row">
          <select value={item.kind} onChange={(e) => setItem({ ...item, kind: e.target.value })} aria-label="Kind">{KINDS.map((k) => <option key={k}>{k}</option>)}</select>
          <select value={item.status} onChange={(e) => setItem({ ...item, status: e.target.value })} aria-label="Status">{["confirmed", "hypothesis", "suggestion"].map((k) => <option key={k}>{k}</option>)}</select>
          <input value={item.title} onChange={(e) => setItem({ ...item, title: e.target.value })} placeholder="Title" aria-label="Memory title" />
        </div>
        <textarea rows={3} value={item.body} onChange={(e) => setItem({ ...item, body: e.target.value })} placeholder="Details" aria-label="Memory details" />
        <div className="actions"><button className="btn" disabled={!item.title.trim()} onClick={addItem}>Add to memory</button></div>
      </Card>
    </div>
  );
}

export function Projects() {
  const projects = usePoll<Project[]>(() => api("/api/projects"), 0);
  const [selected, setSelected] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function create() {
    setErr(null);
    try {
      const p = await post<Project>("/api/projects", { name });
      setName("");
      setSelected(p.id);
      projects.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }
  if (projects.loading && !projects.data) return <Loading />;
  const current = projects.data?.find((p) => p.id === selected) ?? projects.data?.[0];
  return (
    <div className="stack">
      <h1>Projects</h1>
      {projects.error && <ErrorBanner message={projects.error} onRetry={projects.reload} />}
      <div className="form-row">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New project name" aria-label="New project name" />
        <button className="btn btn-primary" disabled={!name.trim()} onClick={create}>Create project</button>
      </div>
      {err && <ErrorBanner message={err} />}
      {projects.data && projects.data.length === 0 && <Empty title="No projects yet">A project gives agents a permitted folder, standing instructions (PROJECT.md) and memory. Tasks without a project cannot read or change files.</Empty>}
      {projects.data && projects.data.length > 0 && (
        <>
          <div className="tabs" role="tablist">{projects.data.map((p) => <button key={p.id} role="tab" aria-selected={current?.id === p.id} className={current?.id === p.id ? "tab active" : "tab"} onClick={() => setSelected(p.id)}>{p.name}</button>)}</div>
          {current && <ProjectPanel key={current.id} p={current} reload={projects.reload} />}
        </>
      )}
    </div>
  );
}
