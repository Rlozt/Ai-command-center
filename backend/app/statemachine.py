"""Explicit task state machine. Every transition is validated and persisted."""
from __future__ import annotations

from .db import Database, now

PENDING = "pending"
PLANNING = "planning"
READY = "ready"
RUNNING = "running"
REVIEWING = "reviewing"
REPAIRING = "repairing"
AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
APPLYING = "applying_changes"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

ALL_STATES = [PENDING, PLANNING, READY, RUNNING, REVIEWING, REPAIRING, AWAITING_APPROVAL, APPROVED,
              APPLYING, COMPLETED, FAILED, CANCELLED]
TERMINAL = {COMPLETED, CANCELLED}
ACTIVE = {PLANNING, READY, RUNNING, REVIEWING, REPAIRING, APPLYING}  # states the engine drives

TRANSITIONS: dict[str, set[str]] = {
    PENDING: {PLANNING, CANCELLED},
    PLANNING: {READY, FAILED, CANCELLED},
    READY: {RUNNING, CANCELLED, FAILED},
    RUNNING: {REVIEWING, REPAIRING, AWAITING_APPROVAL, COMPLETED, FAILED, CANCELLED},
    REVIEWING: {REPAIRING, AWAITING_APPROVAL, COMPLETED, FAILED, CANCELLED},
    REPAIRING: {RUNNING, REVIEWING, AWAITING_APPROVAL, FAILED, CANCELLED},
    AWAITING_APPROVAL: {APPROVED, REPAIRING, CANCELLED},
    APPROVED: {APPLYING, AWAITING_APPROVAL, CANCELLED},  # back to awaiting if the proposal changes
    APPLYING: {COMPLETED, FAILED},
    FAILED: {READY, PLANNING, CANCELLED},  # retry resumes failed work or re-plans
    COMPLETED: set(),
    CANCELLED: set(),
}


class IllegalTransition(Exception):
    def __init__(self, src: str, dst: str):
        super().__init__(f"illegal task transition {src} -> {dst}")
        self.src, self.dst = src, dst


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def transition(db: Database, task_id: str, dst: str, reason: str = "", **fields) -> str:
    """Atomically move a task to `dst`, recording the event. Returns the previous state.

    The UPDATE is conditioned on the state we validated against, so concurrent writers
    cannot both succeed from the same source state.
    """
    allowed_cols = {"error", "outcome", "paused", "pause_reason", "repair_attempts", "unresolved_issues",
                    "report", "plan", "owner_instance", "cancel_requested", "revision_feedback", "revision_rounds"}
    bad = set(fields) - allowed_cols
    if bad:
        raise ValueError(f"unexpected columns: {bad}")
    with db.tx() as c:
        row = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        src = row["status"]
        if not can_transition(src, dst):
            raise IllegalTransition(src, dst)
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values())
        c.execute(
            f"UPDATE tasks SET status=?, updated_at=?{', ' + sets if sets else ''} WHERE id=? AND status=?",
            [dst, now(), *params, task_id, src],
        )
        c.execute(
            "INSERT INTO state_events(task_id,from_state,to_state,reason,at) VALUES(?,?,?,?,?)",
            (task_id, src, dst, reason, now()),
        )
    return src
