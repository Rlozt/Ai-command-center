import pytest

from app import statemachine as sm
from app.db import Database
from app.store import Store


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def make(db):
    return Store(db).create_task("do a thing", None, "auto", {})


def test_all_states_have_a_transition_entry():
    assert set(sm.TRANSITIONS) == set(sm.ALL_STATES)


@pytest.mark.parametrize("src,dst", [(s, d) for s, ds in sm.TRANSITIONS.items() for d in ds])
def test_every_declared_transition_is_legal(src, dst):
    assert sm.can_transition(src, dst)


@pytest.mark.parametrize("src,dst", [
    (sm.PENDING, sm.COMPLETED), (sm.PENDING, sm.RUNNING), (sm.COMPLETED, sm.RUNNING), (sm.CANCELLED, sm.PENDING),
    (sm.AWAITING_APPROVAL, sm.APPLYING), (sm.AWAITING_APPROVAL, sm.COMPLETED), (sm.RUNNING, sm.APPROVED),
    (sm.FAILED, sm.COMPLETED), (sm.APPLYING, sm.AWAITING_APPROVAL),
])
def test_illegal_transitions_are_rejected(src, dst):
    assert not sm.can_transition(src, dst)


def test_terminal_states_have_no_exits():
    assert sm.TRANSITIONS[sm.COMPLETED] == set() and sm.TRANSITIONS[sm.CANCELLED] == set()


def test_transition_persists_state_and_event(db):
    t = make(db)
    sm.transition(db, t["id"], sm.PLANNING, "go")
    assert db.one("SELECT status FROM tasks WHERE id=?", (t["id"],))["status"] == "planning"
    events = db.all("SELECT from_state,to_state FROM state_events WHERE task_id=? ORDER BY id", (t["id"],))
    assert [(e["from_state"], e["to_state"]) for e in events] == [(None, "pending"), ("pending", "planning")]


def test_illegal_transition_raises_and_changes_nothing(db):
    t = make(db)
    with pytest.raises(sm.IllegalTransition):
        sm.transition(db, t["id"], sm.COMPLETED)
    assert db.one("SELECT status FROM tasks WHERE id=?", (t["id"],))["status"] == "pending"
    assert len(db.all("SELECT * FROM state_events WHERE task_id=?", (t["id"],))) == 1


def test_transition_can_set_allowed_fields_only(db):
    t = make(db)
    sm.transition(db, t["id"], sm.PLANNING, error="x", outcome="partial")
    assert db.one("SELECT error,outcome FROM tasks WHERE id=?", (t["id"],)) == {"error": "x", "outcome": "partial"}
    with pytest.raises(ValueError):
        sm.transition(db, t["id"], sm.READY, title="hack")


def test_full_happy_path_is_legal():
    path = [sm.PENDING, sm.PLANNING, sm.READY, sm.RUNNING, sm.REVIEWING, sm.REPAIRING, sm.REVIEWING, sm.AWAITING_APPROVAL,
            sm.APPROVED, sm.APPLYING, sm.COMPLETED]
    for a, b in zip(path, path[1:]):
        assert sm.can_transition(a, b), (a, b)
