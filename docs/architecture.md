# Architecture notes

## Components
- `engine.py` drives each task as an async runner (one per task, enforced). Planning → per-subtask execution in dependency order → tests → independent review → bounded repair → proposal for approval → apply. State changes go through `statemachine.transition()` (conditional UPDATE + event row), so illegal or duplicate transitions are refused.
- `gateway.py` is the only path to a provider: budget pre-check (worst-case estimate), timeout, bounded retries (SDK retries are disabled so the app counts them), cost recording. `budget.py` raises `BudgetExceeded`, which pauses the task instead of failing it.
- `router.py` maps subtask kinds to agents (research → Perplexity, analysis/planning → OpenAI, implementation/synthesis → Claude), honours manual overrides, and picks a reviewer different from the author.
- `research.py` holds the cache, source/claim registration and code-only verification.
- `staging.py` stages proposals, binds approval to a content hash, applies atomically with backups, detects conflicts, and reverts.
- `tools/` registers schema-validated tools; every tool call is permission-checked and logged.
- `store.py`/`db.py`: SQLite (WAL) with a single lock-guarded connection.

## Local provider
`providers/local_provider.py` talks to Ollama's native `/api/chat` (so each request can set `num_ctx`; the OpenAI-compatible endpoint cannot). It is `billable=False, local=True`: calls are recorded with `cost_basis='local'` (also for failures), which keeps them distinct from demo mocks in reports, budgets and the UI. The router uses it only after paid providers (or when chosen manually); research on it is flagged as no-web.

## Trust boundaries
- Trusted: your messages, project instructions, confirmed memory, the settings you saved.
- Untrusted data (fenced in prompts with `<untrusted_data>`): web content, project files, documents, tool output, upstream agent output, unconfirmed memory.
- Deterministic code, not models, decides: claim verification, budget stops, path permission, approval validity, the final report (built from stored facts).

## Mock failure tokens (demo mode, for testing)
Put any of these in a request: `[mock:rate-limit]`, `[mock:rate-limit-once]`, `[mock:timeout]` (Claude's writing steps hang until the call timeout), `[mock:auth]`, `[mock:bad-json]`, `[mock:review-fail]` (fails round 1), `[mock:review-fail-always]`, `[mock:conflict]` and `[mock:outdated]` (first research claim is downgraded), `[mock:break-tests]`, `[mock:unsafe-path]`, `[mock:fail-research]`.
