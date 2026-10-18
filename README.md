# AI Command Center

A local-first dashboard where three AI agents work as one team on your tasks, with you approving every consequential change.

| Agent | Role | What it does |
|---|---|---|
| **OpenAI** | Analysis and planning | Breaks requests into subtasks, evaluates evidence, reviews Claude's work |
| **Claude (Anthropic)** | Implementation and synthesis | Writes code and documents as *proposed* changes, writes the final deliverable |
| **Perplexity** | Research and evidence | Live web research with sources, dates and confidence; every source is shown |

Roles are defaults, not restrictions: you can route any step manually, and if a provider is disabled or has no key the task falls back (research falls back to a model **without web access** and says so).

Runs entirely on your computer: React UI, Python FastAPI backend, SQLite file. API keys live only in environment variables or a local `.env`.

> **Status: v0.1.** Everything below was built and tested on Linux with mocked providers. **Nothing has been run against the real OpenAI, Anthropic or Perplexity APIs, and nothing has been run on Windows** (no keys or Windows machine were available). See [Verification status](#verification-status) and [Known limitations](#known-limitations).

---

## 1. Quick start (demo mode, no keys, no cost)

Requirements: **Python 3.11+**, **Node.js 20+** (with npm).

```
python scripts/acc.py setup     # venv, Python + npm dependencies, builds the UI, creates .env (demo mode)
python scripts/acc.py test      # backend + frontend tests (no paid API calls, no network)
python scripts/acc.py start     # http://localhost:8787  (Ctrl+C stops it)
python scripts/acc.py stop      # from another terminal
python scripts/acc.py status
```

Open <http://localhost:8787>. In demo mode a blue banner says so and agents are offline mocks: no network, no cost, and results are clearly labelled `[DEMO]`.

### Windows (PowerShell) — **unverified**

The PowerShell scripts are thin wrappers around `scripts/acc.py` and were **not run on Windows**. If one fails, run the `python scripts\acc.py ...` equivalent shown above and please report the error.

```powershell
cd C:\path\to\ai-command-center
.\scripts\setup.ps1        # same as: py scripts\acc.py setup
.\scripts\test.ps1
.\scripts\start.ps1        # then open http://localhost:8787
.\scripts\stop.ps1
```

If PowerShell refuses to run scripts: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, or just use `py scripts\acc.py setup` etc. directly.

### Try it

1. **Projects** → create a project, set *Permitted directory* to the `sample-project` folder in this repo and *Test command* to `python -m unittest discover -s tests`.
2. **Dashboard** → pick the project and submit: *“Add a divide(a, b) function to calculator.py that raises ValueError on division by zero, with tests.”*
3. Watch **Agent Activity**; when the task reaches *Awaiting your approval*, open **Approvals**, read the diff, and approve or reject.

In demo mode the mock Claude produces a canned greeting function regardless of the request; that is expected. It exercises the full pipeline (plan, research, implement, stage, test, review, approve, apply), not model quality.

---

## 2. Going live (real providers)

1. Get **API keys** from each provider's developer console. **A ChatGPT Plus/Pro, Claude Pro/Max or Perplexity Pro subscription does not include API access**; API usage is billed separately, prepaid or pay-as-you-go.
   - OpenAI: <https://platform.openai.com/api-keys>
   - Anthropic: <https://console.anthropic.com/>
   - Perplexity: <https://www.perplexity.ai/settings/api>
2. Edit `.env` in the repository root (created by setup; git-ignored):

   ```
   ACC_MODE=live
   OPENAI_API_KEY=...
   ANTHROPIC_API_KEY=...
   PERPLEXITY_API_KEY=...
   ```

   Or set them as environment variables (these win over `.env`). Never paste keys into the UI, source code, or chat; the UI never displays them.
3. **Check model ids.** Defaults are `OPENAI_MODEL=gpt-6.1-sol`, `ANTHROPIC_MODEL=claude-opus-5-5` (`claude-sonnet-5-5` is cheaper), `PERPLEXITY_PRESET=low`. Model names change; confirm them in each provider's docs and override in `.env` if needed. A wrong id fails with a clear provider error, not a fake result.
4. **Set limits first.** Settings → Spending and usage limits. Defaults: $1.00 per task, $5.00 per day, per-provider daily caps (OpenAI $3, Anthropic $3, Perplexity $1), 25 calls and 4 research calls per task, 2 repair attempts. Review the model price table; it is an editable *estimate* used when a provider does not report cost.
5. Restart (`stop`, then `start`), open **Settings**, and for each provider click **Validate** (free where possible) then **Send test request** (a few cents at most; asks for confirmation).
   - OpenAI/Anthropic *Validate* lists models (free). Perplexity has no free key check, so only *Send test request* proves its key.

Providers can be toggled individually in Settings. With Perplexity off, research tasks run on OpenAI/Claude **without web access** and the report says findings are unverified.

---

## 3. How it works

**Task lifecycle** (explicit state machine, every transition logged):
`pending → planning → ready → running → reviewing → (repairing ↔ reviewing, max 2) → awaiting_approval → approved → applying_changes → completed`, with `failed` / `cancelled` reachable from any active state and `paused` as a separate flag (budget stop, restart recovery, blocking question).

**Team workflow:** the planner (OpenAI) splits the request into subtasks; Perplexity researches; OpenAI evaluates evidence and analyses; Claude implements/synthesizes; a **different provider** reviews with evidence for any failure; failed review feeds a bounded repair loop; unresolved problems are reported, never hidden.

**Research evidence (Perplexity):**
- Results are structured: findings (each tagged *source-supported* or *interpretation*), sources with title/URL/publisher/dates/excerpt, conflicting evidence, limitations, unresolved questions.
- Sources returned by Perplexity's search are marked `provider`; any URL a model wrote itself is marked `model_claimed` and can never support a “verified” claim.
- Claim status is `verified / unverified / conflicting / outdated`. **Only code** can mark a claim verified, and only when the cited excerpt is found on the live page. OpenAI's evaluation can only *downgrade* a claim. You can override any claim manually in the task view.
- “Verified” means *the cited source says this*, not *this is true*. Time-sensitive claims backed by sources older than 180 days become `outdated`.
- Cache: identical or near-identical questions reuse stored results (6 h for time-sensitive, 72 h otherwise) with no new Perplexity call. Call counts and cost are capped per task.

**Approvals and applying changes:** agents never write to your project. They produce a *proposal* staged under `data/staging`, shown as a diff. Approval is bound to the exact content hash you reviewed; editing a file or a revision creates a new version that needs fresh approval. Apply backs up originals, refuses if files changed since staging, and can be reverted.

**Tools:** agents only get schema-validated tools with permission boundaries: read/list/search files inside the project folder, stage changes, diff, memory search/add (unconfirmed only), source check, run an allow-listed test command in an isolated copy. There are no deploy, email, purchase, shell or arbitrary-network tools.

**Memory:** each project has standing instructions (the *PROJECT.md* field) plus memory items. Only items you mark *confirmed* are treated as facts; agent-added notes are labelled unconfirmed. Export as JSON or `PROJECT.md` from the Projects page.

**Budget:** before every billable call a worst-case estimate is checked against per-task, daily, per-provider, overall and call-count limits. At a limit the task **pauses before spending** and waits for you. Costs are labelled *reported*, *estimated* or *demo*; demo calls are never shown as costing money, and a task that used a paid service is never labelled free.

**Security model:** server binds to `127.0.0.1`; Host and Origin checks (anti DNS-rebinding/CSRF) and a per-run session token on every API call; secrets are redacted from logs/responses; web pages, documents and tool output are fenced as untrusted data in prompts; paths are confined to the project folder (no traversal, symlinks, `.git`, `.env`, key files); one instance per data directory.

More detail: [`docs/architecture.md`](docs/architecture.md).

---

## 4. Project layout

```
backend/app/        FastAPI app: engine, state machine, gateway, budget, providers/, tools/, staging, research, memory, API
backend/tests/      pytest suite (no network, no paid calls)
frontend/src/       React + TypeScript UI (pages/, components/), vitest tests in src/__tests__
scripts/acc.py      setup | test | start | stop | status (cross-platform); *.ps1 are Windows wrappers
sample-project/     small project to try the workflow safely
data/               created at runtime: SQLite DB, staging, backups, sandbox, session token (git-ignored)
```

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `Python 3.11 or newer is required` | Install Python 3.11+ (Windows: python.org, tick “Add to PATH”, or use `py -3.12`). |
| `npm was not found` | Install Node.js 20+ and open a **new** terminal. |
| `Another AI Command Center instance is already using this data directory` | Run `stop`, or close the other terminal. If it crashed, delete `data/instance.lock`. |
| Port 8787 in use | Set `ACC_PORT=8790` in `.env` and restart. |
| UI says *Cannot reach the server* | Server not running; run `start`. The token is fetched automatically; reload the page after restarts. |
| Settings shows `missing: set OPENAI_API_KEY` | Key not in `.env`/environment, or you did not restart after editing. |
| `[auth]` error from a provider | Wrong/expired key, no credits, or key lacks access to the model. A subscription is not an API key. |
| `[rate_limit]`, task paused | Retries are bounded; wait and **Resume**. |
| Task paused “budget” | A limit was hit before a call. Raise the limit in Settings (deliberately) and **Resume**. |
| Tests *error* with “executable not allowed” | The test command's program must be in the allow-list (`python`, `pytest`, `npm`, `node`, ...). |
| Windows: `.venv` activation or script blocked | Use `py scripts\acc.py ...` directly; see the Windows section. |
| Blank page after editing the UI | `cd frontend && npm run build`, or use `npm run dev` (port 5173, proxied to 8787). |

Logs: the terminal running `start`, and the **Agent Activity** / task *Activity* tab (secrets redacted). Database: `data/acc.sqlite3`.

## 6. Verification status

Run on Linux (Python 3.13, Node 22; Python 3.11 and 3.12 are the stated minimum but were not tried) in a sandbox without API keys or internet access to providers:

- **Verified by automated tests (no paid calls):** state machine; budget/limit enforcement; gateway retries/timeouts; staging/approval/hash binding/apply/revert; path and tool security; engine flows in demo mode; research cache, claim statuses, source-origin flagging, Perplexity routing and fallback via mocked HTTP; memory rules; HTTP API incl. auth/Host/Origin guards; UI pages with a mocked API.
- **Verified manually:** a headless browser (Playwright/Chromium) created a project, ran a task, viewed the diff, approved, and confirmed the files were applied, with no console errors.
- **NOT verified:** the OpenAI, Anthropic and Perplexity adapters against the *real* services (they are tested only against fakes built from the documented request/response shapes); the default model ids; the Windows scripts; Windows file semantics (locking, reserved names, backups).

Run `python scripts/acc.py test` yourself to see current numbers; do not rely on this README for pass counts.

## 7. Known limitations

- **The test runner is not a security sandbox.** Tests run in a temporary *copy* of your project with a scrubbed environment, a program allow-list and a timeout, but test code can still use your network and CPU. Only register projects you trust.
- Price tables are estimates; provider-reported cost is used when available. Timed-out requests may still be billed by the provider. Daily limits use UTC days.
- Live adapters, default model ids and the Perplexity key flow are unverified against real services (see above). Model ids in this build's defaults come from documentation the author could not cross-check; confirm them.
- Excerpt verification proves a page *contains* the cited text, not that it is correct. Paywalled or script-rendered pages show as unreachable.
- Documents: txt/md/csv/json/html/pdf/docx text extraction only; scanned PDFs are not OCR'd.
- Anthropic server-side model `fallbacks` are deliberately not enabled; failure surfaces instead of silently switching models.
- Single user, single machine; no accounts. Do not expose the port to a network.
- No deploy, email, purchase or arbitrary shell tools exist by design.
