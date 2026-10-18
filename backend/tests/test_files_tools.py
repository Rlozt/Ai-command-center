import os
from tests.conftest import make_symlink

import pytest

from app.security import PathError, check_public_http_url, safe_join, validate_relative
from app.tools.base import ToolContext
from app.tools.webcheck import HttpFetcher, excerpt_in_text
import httpx


def tctx(ctx, project):
    return ctx.tool_context(project, None)


async def test_read_and_search_inside_project(ctx, project, project_dir):
    (project_dir / "src").mkdir()
    (project_dir / "src" / "a.py").write_text("def hello():\n    return 'world'\n")
    (project_dir / "notes.md").write_text("# Notes\nhello there\n")
    r = await ctx.tools.invoke("read_file", {"path": "src/a.py"}, tctx(ctx, project))
    assert r["ok"] and "def hello" in r["result"]["text"]
    s = await ctx.tools.invoke("search_files", {"query": "hello"}, tctx(ctx, project))
    assert s["ok"] and {h["path"] for h in s["result"]["hits"]} == {"src/a.py", "notes.md"}
    ls = await ctx.tools.invoke("list_files", {}, tctx(ctx, project))
    assert set(ls["result"]["files"]) == {"src/a.py", "notes.md"}


@pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", "..\\..\\x", ".env", "sub/../../x"])
async def test_reads_outside_the_permitted_directory_are_denied(ctx, project, project_dir, path):
    (project_dir.parent / "secret.txt").write_text("top secret")
    r = await ctx.tools.invoke("read_file", {"path": path}, tctx(ctx, project))
    assert not r["ok"] and r["error"]["code"] in ("path_denied", "invalid_input")
    assert "top secret" not in str(r)


async def test_symlinks_cannot_leak_files_from_outside(ctx, project, project_dir, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside data")
    make_symlink(outside, project_dir / "leak.txt")
    r = await ctx.tools.invoke("read_file", {"path": "leak.txt"}, tctx(ctx, project))
    assert not r["ok"]
    ls = await ctx.tools.invoke("list_files", {}, tctx(ctx, project))
    assert "leak.txt" not in ls["result"]["files"]
    s = await ctx.tools.invoke("search_files", {"query": "outside"}, tctx(ctx, project))
    assert s["result"]["hits"] == []


async def test_secret_files_are_invisible_even_inside_the_project(ctx, project, project_dir):
    (project_dir / ".env").write_text("OPENAI_API_KEY=sk-leak")
    (project_dir / "key.pem").write_text("-----BEGIN-----")
    ls = await ctx.tools.invoke("list_files", {}, tctx(ctx, project))
    assert ls["result"]["files"] == []
    s = await ctx.tools.invoke("search_files", {"query": "sk-leak"}, tctx(ctx, project))
    assert s["result"]["hits"] == []


async def test_no_project_directory_means_no_file_access(ctx):
    proj = ctx.store.create_project("nodir")
    r = await ctx.tools.invoke("read_file", {"path": "a.txt"}, tctx(ctx, proj))
    assert not r["ok"] and r["error"]["code"] == "no_root"
    r = await ctx.tools.invoke("list_files", {}, ToolContext(ctx.cfg, ctx.store, {}, None))
    assert r["error"]["code"] == "no_root"


async def test_tool_errors_are_structured_not_exceptions(ctx, project):
    assert (await ctx.tools.invoke("nope", {}, tctx(ctx, project)))["error"]["code"] == "unknown_tool"
    assert (await ctx.tools.invoke("read_file", {}, tctx(ctx, project)))["error"]["code"] == "invalid_input"
    assert (await ctx.tools.invoke("read_file", {"path": "missing.txt"}, tctx(ctx, project)))["error"]["code"] == "not_found"


async def test_document_formats(ctx, project, project_dir):
    import docx
    d = docx.Document()
    d.add_paragraph("Quarterly plan text")
    d.save(project_dir / "plan.docx")
    (project_dir / "page.html").write_text("<html><script>evil()</script><body><p>Visible</p></body></html>")
    (project_dir / "t.csv").write_text("a,b\n1,2\n")
    r = await ctx.tools.invoke("read_file", {"path": "plan.docx"}, tctx(ctx, project))
    assert "Quarterly plan text" in r["result"]["text"]
    r = await ctx.tools.invoke("read_file", {"path": "page.html"}, tctx(ctx, project))
    assert "Visible" in r["result"]["text"] and "evil" not in r["result"]["text"]
    r = await ctx.tools.invoke("read_file", {"path": "t.csv"}, tctx(ctx, project))
    assert r["result"]["format"] == "csv"
    (project_dir / "x.bin").write_bytes(b"\x00\x01\x02")
    assert not (await ctx.tools.invoke("read_file", {"path": "x.bin"}, tctx(ctx, project)))["ok"]


def test_every_tool_declares_schemas_and_a_permission_boundary(ctx):
    for t in ctx.tools.describe():
        assert t["input_schema"] and t["output_schema"] and t["boundary"] and t["permission"] in ("read", "stage", "execute", "network", "memory")
    assert {"read_file", "search_files", "list_files", "stage_files", "diff_text", "memory_search", "memory_add", "web_verify", "task_report", "project_instructions"} <= set(ctx.tools.names())


async def test_stage_and_diff_tools(ctx, project, project_dir):
    t = ctx.store.create_task("tool stage", project["id"], "auto", {})
    c = ctx.tool_context(project, t["id"])
    r = await ctx.tools.invoke("stage_files", {"files": [{"path": "x/y.txt", "content": "hi"}]}, c)
    assert r["ok"] and not (project_dir / "x").exists()
    bad = await ctx.tools.invoke("stage_files", {"files": [{"path": "../y.txt", "content": "hi"}]}, c)
    assert bad["error"]["code"] == "staging_rejected"
    d = await ctx.tools.invoke("diff_text", {"path": "f", "old": "a\n", "new": "b\n"}, c)
    assert "-a" in d["result"]["diff"] and "+b" in d["result"]["diff"]


# ---- SSRF guard and verification helpers
@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://localhost:8787/api", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd",
                                 "ftp://example.com/x", "http://user:pw@example.com/", "http://10.0.0.5/", "http://[::1]/"])
def test_ssrf_guard_blocks_private_and_non_http_urls(url):
    with pytest.raises(ValueError):
        check_public_http_url(url)


async def test_fetcher_rechecks_every_redirect_hop():
    def handler(request):
        if request.url.host == "good.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9999/admin"})
        return httpx.Response(200, text="should never be reached", headers={"content-type": "text/plain"})

    seen = []

    def check(url):
        seen.append(url)
        if "127.0.0.1" in url:
            raise ValueError("private address")
        return url

    f = HttpFetcher(transport=httpx.MockTransport(handler), check_url=check)
    res = await f.fetch("http://good.example/start")
    assert not res.ok and "blocked" in res.error and seen[-1].startswith("http://127.0.0.1")


async def test_fetcher_strips_html_and_handles_http_errors():
    f = HttpFetcher(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html><style>x{}</style><p>Hello   world</p></html>", headers={"content-type": "text/html"})),
                    check_url=lambda u: u)
    assert (await f.fetch("http://a.example/")).text == "Hello   world"
    f = HttpFetcher(transport=httpx.MockTransport(lambda r: httpx.Response(404)), check_url=lambda u: u)
    res = await f.fetch("http://a.example/")
    assert not res.ok and res.status == 404


def test_excerpt_matching():
    page = "The  release notes say: Python 3.14 introduces   deferred evaluation of annotations by default."
    assert excerpt_in_text("Python 3.14 introduces deferred evaluation of annotations", page)
    assert not excerpt_in_text("Python 3.14 removes the GIL entirely for everyone", page)
    assert not excerpt_in_text("short", page)
