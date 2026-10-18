"""HTTP API behaviour via TestClient (demo providers, no network)."""
import time

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Config
from app.context import build_context
from tests.conftest import no_sleep

SAMPLE = "Write a python function that greets people"


@pytest.fixture
def api(tmp_path):
    cfg = Config(mode="demo", data_dir=tmp_path / "data", allowed_origins=["http://localhost:8787"])
    ctx = build_context(cfg, sleep=no_sleep)
    with TestClient(create_app(ctx, cfg), base_url="http://localhost:8787") as client:
        client.headers["x-acc-token"] = client.get("/api/session").json()["token"]
        client.ctx = ctx
        client.root = tmp_path / "proj"
        client.root.mkdir()
        yield client
    ctx.db.close()


def wait_for(api, tid, states, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        t = api.get(f"/api/tasks/{tid}").json()
        if t["task"]["status"] in states and not t["running"]:
            return t
        time.sleep(0.05)
    raise AssertionError(f"task stuck in {t['task']['status']}")


def mkproject(api):
    r = api.post("/api/projects", json={"name": "P", "root_dir": str(api.root), "test_command": "python -m unittest discover -s demo_output"})
    assert r.status_code == 201
    return r.json()


def test_auth_host_and_origin_guards(api, tmp_path):
    bare = TestClient(api.app, base_url="http://localhost:8787")
    assert bare.get("/api/tasks").status_code == 401
    assert bare.get("/api/health").status_code == 200
    assert api.get("/api/tasks", headers={"host": "evil.example"}).status_code == 403
    assert api.get("/api/tasks", headers={"origin": "http://evil.example"}).status_code == 403
    assert api.get("/api/tasks").status_code == 200


def test_task_validation(api):
    assert api.post("/api/tasks", json={"request": "x"}).status_code == 422
    assert api.post("/api/tasks", json={"request": SAMPLE, "project_id": "nope"}).status_code == 404
    assert api.post("/api/tasks", json={"request": SAMPLE, "routing_mode": "weird"}).status_code == 400
    assert api.post("/api/tasks", json={"request": SAMPLE, "manual_routes": {"code": "skynet"}}).status_code == 400
    assert api.get("/api/tasks/missing").status_code == 404


def test_full_approval_flow_over_http_writes_files_only_after_approval(api):
    p = mkproject(api)
    tid = api.post("/api/tasks", json={"request": SAMPLE, "project_id": p["id"]}).json()["id"]
    d = wait_for(api, tid, {"awaiting_approval"})
    assert list(api.root.iterdir()) == []  # nothing applied yet
    prop = api.get("/api/approvals").json()[0]
    assert prop["files"] and prop["task_id"] == tid
    # wrong hash is refused
    assert api.post(f"/api/proposals/{prop['id']}/approve", json={"content_hash": "deadbeef"}).status_code == 409
    assert list(api.root.iterdir()) == []
    r = api.post(f"/api/proposals/{prop['id']}/approve", json={"content_hash": prop["content_hash"]})
    assert r.status_code == 200, r.text
    d = wait_for(api, tid, {"completed"})
    assert any(api.root.rglob("*.py"))
    # approving twice is refused
    assert api.post(f"/api/proposals/{prop['id']}/approve", json={"content_hash": prop["content_hash"]}).status_code == 409
    assert api.get("/api/approvals").json() == []


def test_reject_requests_revision_and_creates_new_version(api):
    p = mkproject(api)
    tid = api.post("/api/tasks", json={"request": SAMPLE, "project_id": p["id"]}).json()["id"]
    wait_for(api, tid, {"awaiting_approval"})
    first = api.get("/api/approvals").json()[0]
    assert api.post(f"/api/proposals/{first['id']}/reject", json={"note": "add docstring", "request_revision": True}).status_code == 200
    wait_for(api, tid, {"awaiting_approval"})
    second = api.get("/api/approvals").json()[0]
    assert second["id"] != first["id"] and second["version"] == first["version"] + 1
    assert list(api.root.iterdir()) == []
    # old version can no longer be approved
    assert api.post(f"/api/proposals/{first['id']}/approve", json={"content_hash": first["content_hash"]}).status_code == 409


def test_edit_staged_file_changes_hash_so_old_hash_is_stale(api):
    p = mkproject(api)
    tid = api.post("/api/tasks", json={"request": SAMPLE, "project_id": p["id"]}).json()["id"]
    wait_for(api, tid, {"awaiting_approval"})
    prop = api.get("/api/approvals").json()[0]
    path = prop["files"][0]["path"]
    r = api.put(f"/api/proposals/{prop['id']}/files", json={"path": path, "content": "x = 1\n"})
    assert r.status_code == 200, r.text
    new = r.json()  # editing never mutates the reviewed version: it stages a new one
    assert new["id"] != prop["id"] and new["version"] == prop["version"] + 1 and new["content_hash"] != prop["content_hash"]
    assert api.get(f"/api/proposals/{new['id']}/file", params={"path": path}).json()["content"] == "x = 1\n"
    assert "x = 1" not in api.get(f"/api/proposals/{prop['id']}/file", params={"path": path}).json()["content"]
    assert api.post(f"/api/proposals/{prop['id']}/approve", json={"content_hash": prop["content_hash"]}).status_code == 409
    bad = api.put(f"/api/proposals/{prop['id']}/files", json={"path": "../evil.py", "content": "x"})
    assert bad.status_code == 400


def test_cancel_pause_retry_endpoints(api):
    tid = api.post("/api/tasks", json={"request": "Research the current state of local-first software [mock:timeout]"}).json()["id"]
    for _ in range(200):  # wait until the hanging provider call is in flight
        if api.get(f"/api/tasks/{tid}").json()["running"] and api.ctx.store.subtasks(tid):
            break
        time.sleep(0.05)
    assert api.post(f"/api/tasks/{tid}/cancel").status_code == 200
    d = wait_for(api, tid, {"cancelled"})["task"]
    assert d["status"] == "cancelled"
    assert api.post("/api/tasks/missing/cancel").status_code == 404
    assert api.post("/api/tasks/missing/retry").status_code == 404
    done = api.post("/api/tasks", json={"request": "Summarize unit testing in a short document"}).json()["id"]
    wait_for(api, done, {"completed"})
    assert api.post(f"/api/tasks/{done}/resume").status_code == 409
    assert api.post(f"/api/tasks/{done}/retry").status_code == 409


def test_failed_task_can_be_retried_over_http(api):
    tid = api.post("/api/tasks", json={"request": "Research the latest python release and write a summary [mock:fail-research]"}).json()["id"]
    wait_for(api, tid, {"failed"})
    api.ctx.store.set_task(tid, request="Research the latest python release and write a summary")
    assert api.post(f"/api/tasks/{tid}/retry").status_code == 200
    wait_for(api, tid, {"completed"})


def test_projects_memory_and_export(api):
    assert api.post("/api/projects", json={"name": "Bad", "root_dir": "/definitely/not/here"}).status_code == 400
    assert api.post("/api/projects", json={"name": "Bad", "root_dir": "/"}).status_code == 400
    p = mkproject(api)
    m = api.post(f"/api/projects/{p['id']}/memory", json={"kind": "decision", "title": "T", "body": "B", "status": "confirmed"})
    assert m.status_code == 201
    mid = m.json()["id"]
    assert api.patch(f"/api/memory/{mid}", json={"body": "B2"}).json()["body"] == "B2"
    assert api.patch(f"/api/memory/{mid}", json={"status": "weird"}).status_code in (400, 422)
    assert len(api.get(f"/api/projects/{p['id']}/export").json()["memory"]) == 1
    assert api.delete(f"/api/memory/{mid}").status_code == 204
    assert api.get("/api/projects/none/export").status_code == 404


def test_settings_validation_and_no_secrets(api):
    s = api.get("/api/settings").json()
    assert s["mode"] == "demo" and set(s["credentials"]) == {"openai", "anthropic", "perplexity", "local"}
    assert "api_key" not in str(s).lower().replace("api_key_env", "")
    assert api.put("/api/settings", json={"routing_mode": "chaos"}).status_code == 400
    assert api.put("/api/settings", json={"limits": {"per_task_usd": -1}}).status_code == 400
    assert api.put("/api/settings", json={"limits": {"nonsense": 1}}).status_code == 400
    assert api.put("/api/settings", json={"providers_enabled": {"openai": "yes"}}).status_code == 400
    ok = api.put("/api/settings", json={"limits": {"per_task_usd": 0.5}, "providers_enabled": {"perplexity": False}})
    assert ok.status_code == 200 and ok.json()["limits"]["per_task_usd"] == 0.5 and ok.json()["providers_enabled"]["perplexity"] is False


def test_provider_validate_and_test_endpoints(api):
    assert api.post("/api/providers/nope/validate").status_code == 404
    v = api.post("/api/providers/perplexity/validate").json()
    assert v["ok"] is True and "Demo" in v["message"]
    assert api.post("/api/providers/openai/test", json={}).status_code == 400  # needs explicit consent
    t = api.post("/api/providers/openai/test", json={"deep": True}).json()
    assert t["ok"] is True and t["billable"] is False


def test_claim_override_and_agents_and_dashboard(api):
    tid = api.post("/api/tasks", json={"request": "Research the current state of local-first software and list sources"}).json()["id"]
    d = wait_for(api, tid, {"completed"})
    cid = d["claims"][0]["id"]
    assert api.patch(f"/api/claims/{cid}", json={"status": "bogus"}).status_code == 400
    assert api.patch(f"/api/claims/{cid}", json={"status": "conflicting", "note": "checked by hand"}).status_code == 200
    c = [c for c in api.get(f"/api/tasks/{tid}").json()["claims"] if c["id"] == cid][0]
    assert c["status"] == "conflicting" and c["note"].startswith("manual:")
    agents = api.get("/api/agents").json()
    assert {a["provider"] for a in agents} == {"openai", "anthropic", "perplexity", "local"}
    dash = api.get("/api/dashboard").json()
    assert dash["mode"] == "demo" and dash["recently_completed"] and "usage" in dash and len(dash["providers"]) == 4
    assert api.get("/api/tools").status_code == 200


def test_spa_fallback_does_not_leak_files(api):
    assert api.get("/..%2f..%2fetc/passwd").status_code in (200, 404)
    assert b"root:" not in api.get("/..%2f..%2fetc/passwd").content


def test_project_markdown_export(api):
    p = mkproject(api)
    api.post(f"/api/projects/{p['id']}/memory", json={"kind": "decision", "title": "T", "body": "B", "status": "confirmed"})
    r = api.get(f"/api/projects/{p['id']}/export", params={"format": "markdown"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown") and "# PROJECT.md: P" in r.text
