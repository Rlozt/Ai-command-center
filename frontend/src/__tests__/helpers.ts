import { vi } from "vitest";
import { resetToken } from "../api";
import type { Dashboard, Proposal, SettingsData } from "../types";

type Handler = (url: string, init?: RequestInit) => { status?: number; body?: unknown } | Promise<{ status?: number; body?: unknown }>;

/** Installs a fake fetch. Routes are matched by prefix; /api/session is answered automatically. Returns the call log. */
export function mockApi(routes: Record<string, Handler | { status?: number; body?: unknown }>) {
  resetToken();
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url === "/api/session") return new Response(JSON.stringify({ token: "test-token", mode: "demo", version: "t" }), { status: 200 });
      const key = Object.keys(routes).find((k) => url.startsWith(k));
      if (!key) return new Response(JSON.stringify({ detail: `no mock for ${url}` }), { status: 500 });
      const r = routes[key];
      const out = typeof r === "function" ? await r(url, init) : r;
      return new Response(out.status === 204 ? null : JSON.stringify(out.body ?? {}), { status: out.status ?? 200 });
    }),
  );
  return calls;
}

export const providers = (configured = true) =>
  (["openai", "anthropic", "perplexity"] as const).map((p) => ({
    provider: p, role: p, enabled: true, configured, ready: configured, demo: false, model: `${p}-model`, detail: "", validated: null,
  }));

const usage = {
  spent_today_usd: 0.25, spent_total_usd: 1, daily_limit_usd: 5, per_task_limit_usd: 1, demo_calls_today: 0,
  providers: { openai: { spent_today_usd: 0, calls_today: 2, input_tokens_today: 0, output_tokens_today: 0, limit_usd: 3 } }, note: "",
};

export const dashboard = (over: Partial<Dashboard> = {}): Dashboard => ({
  mode: "live", current_tasks: [], recently_completed: [], needs_attention: [], assignments: [], pending_approvals: [], recent_errors: [],
  usage, providers: providers() as Dashboard["providers"], ...over,
});

export const proposal = (over: Partial<Proposal> = {}): Proposal => ({
  id: "p_1", task_id: "t_1", task_title: "Add greeter", version: 2, title: "Greeter", summary: "Adds greet()", author_agent: "anthropic",
  status: "pending", content_hash: "abcdef1234567890", unresolved_issues: 0, created_at: "2026-10-18T00:00:00Z",
  files: [{ id: 1, path: "greet.py", action: "create", diff: "+def greet():\n+    pass\n", base_hash: null, new_hash: "h" }], ...over,
});

export const settings = (over: Partial<SettingsData> = {}): SettingsData => ({
  mode: "live", providers_enabled: { openai: true, anthropic: true, perplexity: true }, routing_mode: "auto",
  limits: {
    per_task_usd: 1, daily_usd: 5, overall_usd: 0, per_provider_daily_usd: { openai: 3, anthropic: 3, perplexity: 1 }, max_calls_per_task: 25,
    max_research_calls_per_task: 4, max_repair_attempts: 2, max_subtasks: 8, call_timeout_s: 120, task_timeout_s: 900, max_retries_per_call: 2,
    max_output_tokens: 4096, research_max_results: 8, research_cache_ttl_hours: 72, time_sensitive_ttl_hours: 6,
  },
  prices: { "gpt-6.1-sol": { in: 2, out: 10 } }, providers_status: providers(false) as SettingsData["providers_status"],
  models: { openai: "gpt-6.1-sol", anthropic: "claude-opus-5-5", perplexity: "perplexity/low" },
  credentials: { openai: "missing", anthropic: "configured", perplexity: "missing" }, usage, ...over,
});
