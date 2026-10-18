"""SQLite persistence. One connection guarded by a lock; every statement is short."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  root_dir TEXT NOT NULL DEFAULT '',          -- the only directory agents may read; empty = no file access
  test_command TEXT NOT NULL DEFAULT '',      -- user-approved test command run in an isolated copy
  instructions TEXT NOT NULL DEFAULT '',      -- PROJECT.md content
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  title TEXT NOT NULL,
  request TEXT NOT NULL,
  status TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT '',            -- '', success, partial, failed, cancelled, completed_with_issues
  routing_mode TEXT NOT NULL DEFAULT 'auto',   -- auto | manual
  manual_routes TEXT NOT NULL DEFAULT '{}',    -- JSON {kind: provider} when routing_mode = manual
  plan TEXT NOT NULL DEFAULT '{}',             -- JSON plan
  report TEXT NOT NULL DEFAULT '{}',           -- JSON final report
  error TEXT NOT NULL DEFAULT '',
  paused INTEGER NOT NULL DEFAULT 0,
  pause_reason TEXT NOT NULL DEFAULT '',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  repair_attempts INTEGER NOT NULL DEFAULT 0,
  unresolved_issues INTEGER NOT NULL DEFAULT 0,
  revision_feedback TEXT NOT NULL DEFAULT '',
  revision_rounds INTEGER NOT NULL DEFAULT 0,
  owner_instance TEXT NOT NULL DEFAULT '',     -- engine instance currently driving the task
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS state_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  from_state TEXT, to_state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subtasks (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  idx INTEGER NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,                          -- research | analysis | implementation | document | synthesis | review
  objective TEXT NOT NULL,
  acceptance_criteria TEXT NOT NULL DEFAULT '[]',
  depends_on TEXT NOT NULL DEFAULT '[]',       -- JSON list of subtask ids
  agent TEXT NOT NULL,                         -- the single owning agent (provider name)
  routing_note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ready',        -- blocked | ready | running | done | failed | skipped
  attempts INTEGER NOT NULL DEFAULT 0,
  claim_token TEXT NOT NULL DEFAULT '',
  output TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id);

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, subtask_id TEXT,
  provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,                        -- ok | error
  error_kind TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER, output_tokens INTEGER,
  cost_usd REAL,                               -- NULL = unknown
  cost_basis TEXT NOT NULL DEFAULT 'none',     -- none | demo | reported | estimated
  billable INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER, meta TEXT NOT NULL DEFAULT '{}',
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_task ON calls(task_id);
CREATE INDEX IF NOT EXISTS idx_calls_at ON calls(at);

CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, subtask_id TEXT, level TEXT NOT NULL, agent TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id);

CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  version INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  author_agent TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,                        -- pending | approved | rejected | superseded | applied | failed
  content_hash TEXT NOT NULL,
  unresolved_issues INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, decided_at TEXT, decision_note TEXT NOT NULL DEFAULT '', applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_task ON proposals(task_id);

CREATE TABLE IF NOT EXISTS proposal_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  path TEXT NOT NULL,
  action TEXT NOT NULL,                        -- create | modify
  base_hash TEXT,                              -- sha256 of original at staging time (NULL for create)
  new_hash TEXT NOT NULL,
  staged_path TEXT NOT NULL,
  diff TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, proposal_id TEXT NOT NULL,
  decision TEXT NOT NULL,                      -- approved | rejected
  content_hash TEXT NOT NULL,                  -- the exact proposal version the decision is bound to
  acknowledged_issues INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '', at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, proposal_id TEXT,
  command TEXT NOT NULL, status TEXT NOT NULL, -- passed | failed | error | not_run | timeout
  exit_code INTEGER, output TEXT NOT NULL DEFAULT '', duration_ms INTEGER, at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, subtask_id TEXT, reviewer TEXT NOT NULL, author TEXT NOT NULL,
  round INTEGER NOT NULL, verdict TEXT NOT NULL, issues TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '', at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question_hash TEXT NOT NULL, question TEXT NOT NULL,
  time_sensitive INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL, created_at TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rc_hash ON research_cache(question_hash);

CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, subtask_id TEXT,
  text TEXT NOT NULL,
  status TEXT NOT NULL,                        -- verified | unverified | conflicting | outdated
  support TEXT NOT NULL DEFAULT 'source',      -- source (stated by a source) | interpretation (agent's reading)
  source_urls TEXT NOT NULL DEFAULT '[]',
  note TEXT NOT NULL DEFAULT '',
  checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_task ON claims(task_id);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, subtask_id TEXT,
  question TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '',
  published TEXT, last_updated TEXT, retrieved_at TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'provider',     -- provider (returned by the search API) | model_claimed
  check_status TEXT NOT NULL DEFAULT 'unchecked', -- unchecked | opened | unreachable | excerpt_found | excerpt_not_found
  check_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sources_task ON sources(task_id);

CREATE TABLE IF NOT EXISTS memory_items (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  kind TEXT NOT NULL,                          -- instruction | preference | decision | finding | limitation | file_ref | outstanding | config
  status TEXT NOT NULL,                        -- confirmed | hypothesis | suggestion
  title TEXT NOT NULL, body TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'user',         -- user | agent name | task id
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # settings -----------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def jloads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default
