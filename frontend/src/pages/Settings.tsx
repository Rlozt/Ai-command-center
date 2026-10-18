import { useEffect, useState } from "react";
import { api, post, put } from "../api";
import { Card, DemoBanner, ErrorBanner, Loading, Money, StatusPill, AGENTS } from "../components/ui";
import { usePoll } from "../hooks";
import type { Provider, SettingsData } from "../types";

const PROVIDERS: Provider[] = ["openai", "anthropic", "perplexity"];
const ENV_VAR: Record<Provider, string> = { openai: "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY", perplexity: "PERPLEXITY_API_KEY" };

export function Settings() {
  const q = usePoll<SettingsData>(() => api("/api/settings"), 0);
  const [draft, setDraft] = useState<SettingsData | null>(null);
  const [results, setResults] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { if (q.data && !draft) setDraft(structuredClone(q.data)); }, [q.data, draft]);
  if (q.loading && !q.data) return <Loading label="Loading settings…" />;
  if (!q.data || !draft) return <ErrorBanner message={q.error ?? "Could not load settings"} onRetry={q.reload} />;
  const s = q.data;

  async function save() {
    setErr(null);
    setSaved(null);
    try {
      await put("/api/settings", { providers_enabled: draft!.providers_enabled, routing_mode: draft!.routing_mode, limits: draft!.limits, prices: draft!.prices });
      setSaved("Settings saved.");
      q.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }
  async function validate(p: Provider) {
    setResults((r) => ({ ...r, [p]: "Checking…" }));
    const r = await post<{ ok: boolean; message: string }>(`/api/providers/${p}/validate`);
    setResults((x) => ({ ...x, [p]: `${r.ok ? "OK" : "Problem"}: ${r.message}` }));
    q.reload();
  }
  async function test(p: Provider) {
    if (s.mode === "live" && !window.confirm(`Send one tiny real request to ${AGENTS[p].name}? This may cost a fraction of a cent and counts toward your limits.`)) return;
    const r = await post<{ ok: boolean; message: string }>(`/api/providers/${p}/test`, { deep: true });
    setResults((x) => ({ ...x, [p]: `${r.ok ? "Test OK" : "Test failed"}: ${r.message}` }));
    q.reload();
  }
  const L = draft.limits;
  const num = (v: string) => (v === "" ? 0 : Number(v));
  const setLimit = (k: keyof typeof L, v: number) => setDraft({ ...draft, limits: { ...L, [k]: v } });

  return (
    <div className="stack">
      <h1>Settings</h1>
      <DemoBanner mode={s.mode} />
      <Card title="Providers">
        <p className="muted small">API keys are read from environment variables or the .env file on this computer and are never shown here. A ChatGPT, Claude or Perplexity subscription does not include API access: you need an API key from each provider's developer console.</p>
        <table className="table">
          <thead><tr><th>Agent</th><th>Enabled</th><th>Credentials</th><th>Model</th><th>Daily limit (USD)</th><th></th></tr></thead>
          <tbody>
            {PROVIDERS.map((p) => (
              <tr key={p}>
                <td><strong>{AGENTS[p].name}</strong><div className="muted small">{AGENTS[p].role}</div></td>
                <td><label className="switch"><input type="checkbox" aria-label={`Enable ${AGENTS[p].name}`} checked={draft.providers_enabled[p]} onChange={(e) => setDraft({ ...draft, providers_enabled: { ...draft.providers_enabled, [p]: e.target.checked } })} /> {draft.providers_enabled[p] ? "on" : "off"}</label></td>
                <td>{s.mode === "demo" ? <StatusPill value="ok" label="demo (not needed)" /> : <StatusPill value={s.credentials[p] === "configured" ? "ok" : "not_configured"} label={s.credentials[p] === "configured" ? "configured" : `missing: set ${ENV_VAR[p]}`} />}</td>
                <td className="small">{s.mode === "demo" ? "demo mock" : s.models[p]}</td>
                <td><input className="narrow" type="number" min={0} step="0.5" aria-label={`${AGENTS[p].name} daily limit`} value={L.per_provider_daily_usd[p] ?? 0} onChange={(e) => setDraft({ ...draft, limits: { ...L, per_provider_daily_usd: { ...L.per_provider_daily_usd, [p]: num(e.target.value) } } })} /></td>
                <td className="actions-col"><button className="btn btn-ghost" onClick={() => validate(p)}>Validate</button><button className="btn btn-ghost" onClick={() => test(p)}>Send test request</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {Object.entries(results).map(([k, v]) => <p key={k} className="small" role="status"><strong>{AGENTS[k as Provider].name}:</strong> {v}</p>)}
        <p className="muted small">“Validate” uses free calls where the provider offers one (OpenAI and Anthropic model lists). Perplexity has no key-checking endpoint, so only “Send test request” proves its key.</p>
      </Card>

      <Card title="Spending and usage limits">
        <div className="form-grid">
          <label className="field"><span>Per-task limit (USD)</span><input type="number" min={0} step="0.1" value={L.per_task_usd} onChange={(e) => setLimit("per_task_usd", num(e.target.value))} /></label>
          <label className="field"><span>Daily limit, all providers (USD)</span><input type="number" min={0} step="0.5" value={L.daily_usd} onChange={(e) => setLimit("daily_usd", num(e.target.value))} /></label>
          <label className="field"><span>Overall limit (USD, 0 = none)</span><input type="number" min={0} step="1" value={L.overall_usd} onChange={(e) => setLimit("overall_usd", num(e.target.value))} /></label>
          <label className="field"><span>Max model calls per task</span><input type="number" min={1} value={L.max_calls_per_task} onChange={(e) => setLimit("max_calls_per_task", num(e.target.value))} /></label>
          <label className="field"><span>Max research calls per task</span><input type="number" min={0} value={L.max_research_calls_per_task} onChange={(e) => setLimit("max_research_calls_per_task", num(e.target.value))} /></label>
          <label className="field"><span>Max repair attempts</span><input type="number" min={0} value={L.max_repair_attempts} onChange={(e) => setLimit("max_repair_attempts", num(e.target.value))} /></label>
          <label className="field"><span>Call timeout (seconds)</span><input type="number" min={5} value={L.call_timeout_s} onChange={(e) => setLimit("call_timeout_s", num(e.target.value))} /></label>
          <label className="field"><span>Retries per failed call</span><input type="number" min={0} max={5} value={L.max_retries_per_call} onChange={(e) => setLimit("max_retries_per_call", num(e.target.value))} /></label>
        </div>
        <p className="small">Spent today: <Money usd={s.usage.spent_today_usd} demo={s.mode === "demo"} /> · when a limit would be exceeded, the task pauses before the call is made and waits for you.</p>
      </Card>

      <Card title="Routing">
        <label className="field"><span>Default for new tasks</span>
          <select value={draft.routing_mode} onChange={(e) => setDraft({ ...draft, routing_mode: e.target.value as "auto" | "manual" })}>
            <option value="auto">Automatic: research → Perplexity, analysis → OpenAI, implementation → Claude</option>
            <option value="manual">Manual: choose agents when creating each task</option>
          </select></label>
      </Card>

      <Card title="Model prices (USD per 1M tokens)">
        <p className="muted small">Estimates used for budget checks when a provider does not report cost. Edit if your pricing differs. Unknown models use a conservative default.</p>
        <table className="table"><thead><tr><th>Model</th><th>Input</th><th>Output</th></tr></thead>
          <tbody>{Object.entries(draft.prices).map(([m, v]) => <tr key={m}><td><code>{m}</code></td>
            <td><input className="narrow" type="number" step="0.01" value={v.in} onChange={(e) => setDraft({ ...draft, prices: { ...draft.prices, [m]: { ...v, in: num(e.target.value) } } })} aria-label={`${m} input price`} /></td>
            <td><input className="narrow" type="number" step="0.01" value={v.out} onChange={(e) => setDraft({ ...draft, prices: { ...draft.prices, [m]: { ...v, out: num(e.target.value) } } })} aria-label={`${m} output price`} /></td></tr>)}</tbody></table>
      </Card>

      {err && <ErrorBanner message={err} />}
      {saved && <p role="status">{saved}</p>}
      <div className="actions"><button className="btn btn-primary" onClick={save}>Save settings</button></div>
    </div>
  );
}
