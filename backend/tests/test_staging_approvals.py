import os
from tests.conftest import make_symlink
from pathlib import Path

import pytest

from app import statemachine as sm
from app.schemas import ProposedFile
from app.staging import ApplyConflict, ApprovalError, StagingError, sha256_bytes


@pytest.fixture
def task(ctx, project):
    t = ctx.store.create_task("stage things", project["id"], "auto", {})
    for s in (sm.PLANNING, sm.READY, sm.RUNNING, sm.REVIEWING):
        sm.transition(ctx.db, t["id"], s)
    return t


def stage(ctx, task, project, files, issues=0):
    p = ctx.staging.create_proposal(task["id"], project, files, "t", "s", "anthropic", unresolved_issues=issues)
    return p


def to_awaiting(ctx, task):
    sm.transition(ctx.db, task["id"], sm.AWAITING_APPROVAL)


def PF(path, content, action="create"):
    return ProposedFile(path=path, action=action, content=content)


def test_staging_never_touches_the_project_and_makes_a_readable_diff(ctx, task, project, project_dir):
    (project_dir / "a.txt").write_bytes(b"one\ntwo\n")  # bytes: write_text would translate to CRLF on Windows
    p = stage(ctx, task, project, [PF("a.txt", "one\nTWO\n", "modify"), PF("new/b.txt", "hello\n")])
    assert (project_dir / "a.txt").read_text() == "one\ntwo\n" and not (project_dir / "new").exists()
    files = {f["path"]: f for f in p["files"]}
    assert files["a.txt"]["action"] == "modify" and "-two" in files["a.txt"]["diff"] and "+TWO" in files["a.txt"]["diff"]
    assert files["a.txt"]["base_hash"] == sha256_bytes(b"one\ntwo\n")
    assert files["new/b.txt"]["action"] == "create" and "+hello" in files["new/b.txt"]["diff"]
    assert Path(files["new/b.txt"]["staged_path"]).read_text() == "hello\n"
    assert str(ctx.cfg.staging_dir) in files["new/b.txt"]["staged_path"]


def test_agent_claiming_create_for_an_existing_file_is_treated_as_modify(ctx, task, project, project_dir):
    (project_dir / "a.txt").write_text("x")
    p = stage(ctx, task, project, [PF("a.txt", "y", "create")])
    assert p["files"][0]["action"] == "modify"


@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/passwd", "C:\\Windows\\x.txt", "a/../../b", ".env", "sub/.env.local", ".git/config", "id_rsa", "server.pem", "con.txt", "a:b.txt", ""])
def test_unsafe_paths_are_rejected(ctx, task, project, bad):
    with pytest.raises(StagingError):
        stage(ctx, task, project, [PF(bad, "x")])


def test_symlink_escape_is_blocked(ctx, task, project, project_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    make_symlink(outside, project_dir / "link")
    with pytest.raises(Exception):
        stage(ctx, task, project, [PF("link/pwn.txt", "x")])
    assert not (outside / "pwn.txt").exists()


def test_proposal_limits(ctx, task, project):
    with pytest.raises(StagingError):
        stage(ctx, task, project, [])
    with pytest.raises(StagingError):
        stage(ctx, task, project, [PF("a.txt", "x"), PF("A.txt", "y")])  # duplicate (case-insensitive)
    with pytest.raises(StagingError):
        stage(ctx, task, project, [PF("big.txt", "x" * 600_000)])


def test_approve_then_apply_writes_files_and_backs_up_originals(ctx, task, project, project_dir):
    (project_dir / "a.txt").write_text("old")
    p = stage(ctx, task, project, [PF("a.txt", "new", "modify"), PF("n.txt", "fresh")])
    to_awaiting(ctx, task)
    ctx.staging.approve(p["id"], p["content_hash"], "ok")
    sm.transition(ctx.db, task["id"], sm.APPLYING)
    paths = ctx.staging.apply(p["id"], project)
    assert sorted(paths) == ["a.txt", "n.txt"]
    assert (project_dir / "a.txt").read_text() == "new" and (project_dir / "n.txt").read_text() == "fresh"
    assert (ctx.cfg.backups_dir / task["id"] / p["id"] / "a.txt").read_text() == "old"
    assert ctx.store.get_proposal(p["id"])["status"] == "applied"


def test_apply_without_approval_is_impossible(ctx, task, project, project_dir):
    p = stage(ctx, task, project, [PF("n.txt", "x")])
    with pytest.raises(ApprovalError):
        ctx.staging.apply(p["id"], project)
    assert not (project_dir / "n.txt").exists()


def test_approval_is_bound_to_the_exact_version(ctx, task, project):
    p = stage(ctx, task, project, [PF("n.txt", "x")])
    to_awaiting(ctx, task)
    with pytest.raises(ApprovalError, match="changed"):
        ctx.staging.approve(p["id"], "0" * 64)
    assert ctx.store.get_proposal(p["id"])["status"] == "pending"


def test_changing_a_proposal_after_approval_requires_reapproval(ctx, task, project, project_dir):
    p1 = stage(ctx, task, project, [PF("n.txt", "v1")])
    to_awaiting(ctx, task)
    ctx.staging.approve(p1["id"], p1["content_hash"])
    assert ctx.store.get_task(task["id"])["status"] == "approved"
    # the user (or anything) edits the proposal after approval
    p2 = ctx.staging.edit_staged_file(p1["id"], "n.txt", "v2-edited", project)
    assert p2["version"] == 2 and p2["content_hash"] != p1["content_hash"]
    assert ctx.store.get_proposal(p1["id"])["status"] == "superseded"
    assert ctx.store.get_task(task["id"])["status"] == "awaiting_approval"
    with pytest.raises(ApprovalError):  # the old approval can no longer be used
        ctx.staging.apply(p1["id"], project)
    with pytest.raises(ApprovalError):
        ctx.staging.apply(p2["id"], project)
    assert not (project_dir / "n.txt").exists()
    ctx.staging.approve(p2["id"], p2["content_hash"])  # a fresh approval of v2 works
    sm.transition(ctx.db, task["id"], sm.APPLYING)
    ctx.staging.apply(p2["id"], project)
    assert (project_dir / "n.txt").read_text() == "v2-edited"


def test_tampering_with_staged_bytes_is_detected(ctx, task, project, project_dir):
    p = stage(ctx, task, project, [PF("n.txt", "safe")])
    to_awaiting(ctx, task)
    Path(p["files"][0]["staged_path"]).write_text("tampered")
    with pytest.raises(ApprovalError, match="modified outside"):
        ctx.staging.approve(p["id"], p["content_hash"])


def test_reject_records_decision_and_blocks_apply(ctx, task, project):
    p = stage(ctx, task, project, [PF("n.txt", "x")])
    to_awaiting(ctx, task)
    ctx.staging.reject(p["id"], "no thanks")
    assert ctx.store.get_proposal(p["id"])["status"] == "rejected"
    assert ctx.store.approvals(task["id"])[-1]["decision"] == "rejected"
    with pytest.raises(ApprovalError):
        ctx.staging.approve(p["id"], p["content_hash"])


def test_apply_refuses_to_overwrite_a_file_changed_since_staging(ctx, task, project, project_dir):
    (project_dir / "a.txt").write_text("original")
    p = stage(ctx, task, project, [PF("a.txt", "agent version", "modify"), PF("other.txt", "x")])
    to_awaiting(ctx, task)
    ctx.staging.approve(p["id"], p["content_hash"])
    (project_dir / "a.txt").write_text("user edited this meanwhile")
    sm.transition(ctx.db, task["id"], sm.APPLYING)
    with pytest.raises(ApplyConflict) as e:
        ctx.staging.apply(p["id"], project)
    assert e.value.conflicts == ["a.txt"]
    assert (project_dir / "a.txt").read_text() == "user edited this meanwhile"
    assert not (project_dir / "other.txt").exists()  # nothing partially applied


def test_apply_refuses_when_a_new_file_now_exists(ctx, task, project, project_dir):
    p = stage(ctx, task, project, [PF("n.txt", "x")])
    to_awaiting(ctx, task)
    ctx.staging.approve(p["id"], p["content_hash"])
    (project_dir / "n.txt").write_text("someone else's")
    sm.transition(ctx.db, task["id"], sm.APPLYING)
    with pytest.raises(ApplyConflict):
        ctx.staging.apply(p["id"], project)
    assert (project_dir / "n.txt").read_text() == "someone else's"


def test_unresolved_issues_need_explicit_acknowledgement(ctx, task, project):
    p = stage(ctx, task, project, [PF("n.txt", "x")], issues=1)
    to_awaiting(ctx, task)
    with pytest.raises(ApprovalError, match="acknowledge"):
        ctx.staging.approve(p["id"], p["content_hash"])
    ctx.staging.approve(p["id"], p["content_hash"], acknowledge_issues=True)


def test_revert_restores_backups_only_if_untouched(ctx, task, project, project_dir):
    (project_dir / "a.txt").write_text("old")
    p = stage(ctx, task, project, [PF("a.txt", "new", "modify"), PF("n.txt", "x")])
    to_awaiting(ctx, task)
    ctx.staging.approve(p["id"], p["content_hash"])
    sm.transition(ctx.db, task["id"], sm.APPLYING)
    ctx.staging.apply(p["id"], project)
    (project_dir / "a.txt").write_text("edited after apply")
    with pytest.raises(ApplyConflict):
        ctx.staging.revert(p["id"], project)
    (project_dir / "a.txt").write_text("new")
    ctx.staging.revert(p["id"], project)
    assert (project_dir / "a.txt").read_text() == "old" and not (project_dir / "n.txt").exists()


def test_project_without_directory_cannot_apply(ctx):
    proj = ctx.store.create_project("nodir")
    t = ctx.store.create_task("x task", proj["id"], "auto", {})
    for s in (sm.PLANNING, sm.READY, sm.RUNNING, sm.REVIEWING, sm.AWAITING_APPROVAL):
        sm.transition(ctx.db, t["id"], s)
    p = ctx.staging.create_proposal(t["id"], proj, [PF("n.txt", "x")], "t", "s", "anthropic")
    ctx.staging.approve(p["id"], p["content_hash"])
    sm.transition(ctx.db, t["id"], sm.APPLYING)
    with pytest.raises(StagingError, match="nowhere to apply"):
        ctx.staging.apply(p["id"], proj)
