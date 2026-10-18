from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Config
from app.context import build_context
from app.db import Database
from app.locking import InstanceLock
from tests.conftest import no_sleep


def make_client(cfg):
    ctx = build_context(cfg, sleep=no_sleep)
    return TestClient(create_app(ctx, cfg), base_url="http://localhost:8787"), ctx


def test_startup_and_shutdown_are_clean(cfg):
    client, ctx = make_client(cfg)
    with client:
        assert client.get("/api/health").json()["ok"] is True
    assert ctx.engine._runners == {}


def test_database_created_and_persists_across_reopen(tmp_path):
    p = tmp_path / "x.sqlite3"
    db = Database(p)
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES('p1','n','a','b')")
    db.close()
    db2 = Database(p)
    assert db2.one("SELECT name FROM projects WHERE id='p1'")["name"] == "n"
    tables = {r["name"] for r in db2.all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "subtasks", "calls", "proposals", "approvals", "claims", "sources", "memory_items", "research_cache"} <= tables
    db2.close()


def test_second_instance_cannot_take_the_same_data_dir(tmp_path):
    a, b = InstanceLock(tmp_path / "l"), InstanceLock(tmp_path / "l")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_api_requires_session_token(cfg):
    client, _ = make_client(cfg)
    with client:
        assert client.get("/api/tasks").status_code == 401
        assert client.get("/api/tasks", headers={"X-ACC-Token": "wrong"}).status_code == 401
        tok = client.get("/api/session").json()["token"]
        assert client.get("/api/tasks", headers={"X-ACC-Token": tok}).status_code == 200


def test_foreign_host_header_is_rejected(cfg):
    client, _ = make_client(cfg)
    with client:
        assert client.get("/api/health", headers={"Host": "evil.example.com"}).status_code == 403


def test_cross_origin_requests_are_rejected_even_with_token(cfg):
    client, _ = make_client(cfg)
    with client:
        tok = client.get("/api/session").json()["token"]
        r = client.post("/api/tasks", json={"request": "hello world"}, headers={"X-ACC-Token": tok, "Origin": "http://evil.example.com"})
        assert r.status_code == 403
        assert client.get("/api/session", headers={"Origin": "http://evil.example.com"}).status_code == 403


def test_no_cors_headers_are_ever_sent(cfg):
    client, _ = make_client(cfg)
    with client:
        r = client.get("/api/health", headers={"Origin": "http://localhost:8787"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_settings_never_expose_api_keys(tmp_path):
    cfg = Config(mode="live", data_dir=tmp_path / "d", allowed_origins=["http://localhost:8787"],
                 openai_api_key="sk-secretsecretsecret123456", anthropic_api_key="sk-ant-secretsecret1234567", perplexity_api_key="pplx-secretsecret123456")
    client, _ = make_client(cfg)
    with client:
        tok = client.get("/api/session").json()["token"]
        body = client.get("/api/settings", headers={"X-ACC-Token": tok}).text
        for secret in ("sk-secretsecret", "sk-ant-secret", "pplx-secret"):
            assert secret not in body
        assert '"configured"' in body


def test_security_headers_present(cfg):
    client, _ = make_client(cfg)
    with client:
        r = client.get("/api/health")
        assert r.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in r.headers["content-security-policy"]


def test_project_root_cannot_be_a_filesystem_root(cfg):
    client, _ = make_client(cfg)
    with client:
        h = {"X-ACC-Token": client.get("/api/session").json()["token"]}
        assert client.post("/api/projects", json={"name": "x", "root_dir": "/"}, headers=h).status_code == 400
        assert client.post("/api/projects", json={"name": "x", "root_dir": "/definitely/not/here"}, headers=h).status_code == 400


def test_secrets_are_redacted_from_logs(ctx):
    from app.security import register_secrets
    register_secrets(["supersecretvalue-12345"])
    t = ctx.store.create_task("hello there", None, "auto", {})
    ctx.store.log(t["id"], "error", "boom with supersecretvalue-12345 and sk-ant-abcdefghijklmnop")
    msg = ctx.store.logs(t["id"])[0]["message"]
    assert "supersecretvalue" not in msg and "sk-ant-abcdef" not in msg
