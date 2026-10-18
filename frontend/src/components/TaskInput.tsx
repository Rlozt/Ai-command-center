import { useState } from "react";
import { post } from "../api";
import type { Project, TaskBrief } from "../types";

export function TaskInput({ projects, onCreated }: { projects: Project[]; onCreated: (id: string) => void }) {
  const [text, setText] = useState("");
  const [projectId, setProjectId] = useState("");
  const [routing, setRouting] = useState<"" | "auto" | "manual">("");
  const [routes, setRoutes] = useState<Record<string, string>>({ research: "perplexity", analysis: "openai", implementation: "anthropic", document: "anthropic", synthesis: "anthropic", plan: "openai", review: "openai" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (text.trim().length < 3) return;
    setBusy(true);
    setError(null);
    try {
      const t = await post<TaskBrief>("/api/tasks", {
        request: text,
        project_id: projectId || null,
        routing_mode: routing || null,
        manual_routes: routing === "manual" ? routes : {},
      });
      setText("");
      onCreated(t.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="task-input" onSubmit={submit} aria-label="New task">
      <label htmlFor="task-text" className="sr-only">
        Describe what you want done
      </label>
      <textarea
        id="task-text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Describe what you want done, e.g. “Research three approaches for drag-and-drop scheduling in my planner app, compare the trade-offs, implement the best one and show me the proposed changes.”"
        rows={4}
      />
      <div className="task-input-row">
        <label>
          <span className="sr-only">Project</span>
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} aria-label="Project">
            <option value="">No project (no file access)</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="sr-only">Agent routing</span>
          <select value={routing} onChange={(e) => setRouting(e.target.value as "" | "auto" | "manual")} aria-label="Agent routing">
            <option value="">Routing: default</option>
            <option value="auto">Routing: automatic</option>
            <option value="manual">Routing: manual</option>
          </select>
        </label>
        <button className="btn btn-primary" type="submit" disabled={busy || text.trim().length < 3}>
          {busy ? "Starting…" : "Run task"}
        </button>
      </div>
      {routing === "manual" && (
        <div className="routes" aria-label="Manual routes">
          {Object.keys(routes).map((k) => (
            <label key={k}>
              <span>{k}</span>
              <select value={routes[k]} onChange={(e) => setRoutes({ ...routes, [k]: e.target.value })}>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Claude</option>
                <option value="perplexity">Perplexity</option>
                <option value="local">Ollama (local or cloud)</option>
              </select>
            </label>
          ))}
        </div>
      )}
      {error && (
        <div className="banner banner-bad" role="alert">
          {error}
        </div>
      )}
    </form>
  );
}
