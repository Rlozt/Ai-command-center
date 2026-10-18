export type Provider = "openai" | "anthropic" | "perplexity" | "local";

export interface ProviderStatus {
  provider: Provider;
  role: string;
  enabled: boolean;
  configured: boolean;
  ready: boolean;
  demo: boolean;
  model: string;
  detail: string;
  validated: boolean | null;
}

export interface TaskBrief {
  id: string;
  title: string;
  status: string;
  outcome: string;
  paused: boolean;
  pause_reason: string;
  created_at: string;
  updated_at: string;
  subtasks_done: number;
  subtasks_total: number;
  agents: string[];
  error: string;
  cost: number;
  demo: boolean;
  local: boolean;
}

export interface ProposalFile {
  id: number;
  path: string;
  action: "create" | "modify";
  diff: string;
  base_hash: string | null;
  new_hash: string;
}

export interface Proposal {
  id: string;
  task_id: string;
  task_title?: string;
  version: number;
  title: string;
  summary: string;
  author_agent: string;
  status: string;
  content_hash: string;
  unresolved_issues: number;
  created_at: string;
  files: ProposalFile[];
}

export interface LogRow { id: number; level: string; agent: string; message: string; at: string; task_id: string | null }

export interface UsageSummary {
  spent_today_usd: number;
  spent_total_usd: number;
  daily_limit_usd: number;
  per_task_limit_usd: number;
  demo_calls_today: number;
  local_calls_today: number;
  providers: Record<string, { spent_today_usd: number; calls_today: number; input_tokens_today: number; output_tokens_today: number; limit_usd: number | null }>;
  note: string;
}

export interface Dashboard {
  mode: "demo" | "live";
  current_tasks: TaskBrief[];
  recently_completed: TaskBrief[];
  needs_attention: TaskBrief[];
  assignments: { id: string; title: string; kind: string; agent: string; task_id: string; task_title: string }[];
  pending_approvals: Proposal[];
  recent_errors: LogRow[];
  usage: UsageSummary;
  providers: ProviderStatus[];
}

export interface Subtask {
  id: string; idx: number; title: string; kind: string; objective: string; agent: string; routing_note: string;
  status: string; attempts: number; error: string; depends_on: string[]; acceptance_criteria: string[]; output: Record<string, unknown>;
}

export interface Claim { id: number; text: string; status: string; support: string; source_urls: string[]; note: string }
export interface Source { id: number; title: string; url: string; domain: string; published: string | null; last_updated: string | null; retrieved_at: string; excerpt: string; origin: string; check_status: string; check_note: string; question: string }
export interface Review { id: number; reviewer: string; author: string; round: number; verdict: string; summary: string; issues: { severity: string; description: string; evidence: string; location: string }[] }
export interface TestRun { id: number; status: string; command: string; output: string; exit_code: number | null; at: string }
export interface CallRow { id: number; provider: string; model: string; purpose: string; status: string; error_kind: string; error: string; input_tokens: number | null; output_tokens: number | null; cost_usd: number | null; cost_basis: string; billable: boolean; at: string }

export interface TaskDetailData {
  task: {
    id: string; title: string; request: string; status: string; outcome: string; paused: boolean; pause_reason: string; error: string;
    routing_mode: string; plan: { summary?: string; assumptions?: string[]; missing_information?: string[] }; report: Record<string, any>;
    repair_attempts: number; unresolved_issues: number; created_at: string; updated_at: string;
  };
  subtasks: Subtask[];
  logs: LogRow[];
  calls: CallRow[];
  reviews: Review[];
  tests: TestRun[];
  proposals: Proposal[];
  claims: Claim[];
  sources: Source[];
  events: { id: number; from_state: string | null; to_state: string; reason: string; at: string }[];
  usage: { calls: number; input_tokens: number; output_tokens: number; cost_usd: number; billable_calls: number; local_calls: number; cloud_calls: number; demo: boolean };
  running: boolean;
}

export interface AgentInfo extends ProviderStatus {
  state: "working" | "idle" | "disabled" | "not_configured";
  running: { id: string; title: string; kind: string; task_id: string; task_title: string }[];
  queued: { id: string; title: string; kind: string; task_id: string; task_title: string }[];
  recent_results: CallRow[];
  last_error: CallRow | null;
  usage: { spent_today_usd: number; calls_today: number; input_tokens_today: number; output_tokens_today: number; limit_usd: number | null };
}

export interface Project { id: string; name: string; description: string; root_dir: string; test_command: string; instructions: string }
export interface MemoryItem { id: string; kind: string; status: string; title: string; body: string; source: string; updated_at: string }

export interface SettingsData {
  mode: "demo" | "live";
  providers_enabled: Record<Provider, boolean>;
  routing_mode: "auto" | "manual";
  limits: {
    per_task_usd: number; daily_usd: number; overall_usd: number; per_provider_daily_usd: Record<string, number>;
    max_calls_per_task: number; max_research_calls_per_task: number; max_repair_attempts: number; max_subtasks: number;
    call_timeout_s: number; local_call_timeout_s: number; task_timeout_s: number; max_retries_per_call: number; max_output_tokens: number;
    research_max_results: number; research_cache_ttl_hours: number; time_sensitive_ttl_hours: number;
  };
  prices: Record<string, { in: number; out: number }>;
  providers_status: ProviderStatus[];
  models: Record<string, string>;
  credentials: Record<string, "configured" | "missing">;
  usage: UsageSummary;
}
