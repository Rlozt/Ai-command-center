import type { ReactNode } from "react";
import type { Provider } from "../types";

export const AGENTS: Record<Provider, { name: string; role: string; label: string }> = {
  openai: { name: "OpenAI", role: "Analysis", label: "OpenAI · Analysis" },
  anthropic: { name: "Claude", role: "Implementation", label: "Claude · Implementation" },
  perplexity: { name: "Perplexity", role: "Research", label: "Perplexity · Research" },
};

export function agentName(p: string): string {
  return AGENTS[p as Provider]?.name ?? p;
}

const STATUS_TONE: Record<string, string> = {
  completed: "good", done: "good", success: "good", verified: "good", pass: "good", passed: "good", ok: "good", excerpt_found: "good",
  running: "info", planning: "info", ready: "info", reviewing: "info", repairing: "info", applying_changes: "info", approved: "info", working: "info",
  awaiting_approval: "warn", pending: "muted", paused: "warn", unverified: "warn", outdated: "warn", not_run: "warn", unchecked: "muted",
  failed: "bad", cancelled: "muted", conflicting: "bad", fail: "bad", error: "bad", skipped: "muted", timeout: "bad", unreachable: "bad",
  excerpt_not_found: "warn", idle: "muted", disabled: "muted", not_configured: "warn",
};

export function StatusPill({ value, label }: { value: string; label?: string }) {
  const tone = STATUS_TONE[value] ?? "muted";
  return <span className={`pill pill-${tone}`}>{label ?? value.replace(/_/g, " ")}</span>;
}

export function Money({ usd, demo }: { usd: number; demo?: boolean }) {
  if (demo) return <span className="muted">demo · no billable calls</span>;
  return <span title="USD, estimated from provider-reported cost or token counts">${usd.toFixed(usd < 0.1 ? 4 : 2)}</span>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty" role="status">
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}

export function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="banner banner-bad" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button className="btn btn-ghost" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      {label}
    </div>
  );
}

export function Card({ title, aside, children, id }: { title?: ReactNode; aside?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="card" id={id}>
      {(title || aside) && (
        <header className="card-head">
          <h2>{title}</h2>
          {aside}
        </header>
      )}
      {children}
    </section>
  );
}

export function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function DemoBanner({ mode }: { mode: string }) {
  if (mode !== "demo") return null;
  return (
    <div className="banner banner-info" role="note">
      Demo mode: agents are offline mock providers. Nothing is sent to OpenAI, Anthropic or Perplexity and no money is spent. Set ACC_MODE=live and add API keys for real work.
    </div>
  );
}
