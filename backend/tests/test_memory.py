import pytest

from app.tools.base import ToolContext
from tests.conftest import run_task


def test_crud_and_validation(ctx, project):
    m = ctx.memory.add(project["id"], "decision", "Use SQLite", "Local-first", "confirmed")
    assert ctx.memory.get(m["id"])["status"] == "confirmed" and m["source"] == "user"
    ctx.memory.update(m["id"], body="Local-first, single file")
    assert ctx.memory.get(m["id"])["body"].endswith("single file")
    with pytest.raises(ValueError):
        ctx.memory.add(project["id"], "bogus", "x", "y")
    with pytest.raises(ValueError):
        ctx.memory.update(m["id"], status="bogus")
    ctx.memory.delete(m["id"])
    assert ctx.memory.get(m["id"]) is None


def test_retrieval_separates_confirmed_from_unconfirmed(ctx, project):
    ctx.memory.add(project["id"], "finding", "Parser quirk", "the parser rejects tabs in headers", "confirmed")
    ctx.memory.add(project["id"], "finding", "Parser guess", "maybe the parser rejects tabs because of csv dialect", "hypothesis")
    ctx.memory.add(project["id"], "finding", "Unrelated", "nothing about cooking", "confirmed")
    r = ctx.memory.retrieve(project["id"], "why does the parser reject tabs")
    assert [i["title"] for i in r["confirmed"]] == ["Parser quirk"]
    assert [i["title"] for i in r["unconfirmed"]] == ["Parser guess"]
    text = ctx.memory.render_context(project, r)
    assert "CONFIRMED PROJECT MEMORY" in text and "NOT facts" in text


def test_retrieval_is_scoped_to_the_project(ctx, project):
    other = ctx.store.create_project("Other")
    ctx.memory.add(other["id"], "decision", "Secret plan", "tabs parser", "confirmed")
    assert ctx.memory.retrieve(project["id"], "tabs parser") == {"confirmed": [], "unconfirmed": []}


async def test_tools_cannot_create_confirmed_memory(ctx, project):
    tctx = ctx.tool_context(project)
    res = await ctx.tools.invoke("memory_add", {"title": "t", "body": "b", "status": "confirmed"}, tctx)
    assert res["ok"] is False and res["error"]["code"] == "not_permitted"
    ok = await ctx.tools.invoke("memory_add", {"title": "t", "body": "b"}, tctx)
    assert ok["ok"] and ok["result"]["status"] == "hypothesis"
    assert ctx.memory.get(ok["result"]["id"])["source"] == "tool"


def test_export_contains_project_and_items(ctx, project):
    ctx.memory.add(project["id"], "instruction", "Style", "Use type hints", "confirmed")
    exp = ctx.memory.export(project["id"])
    assert exp["project"]["id"] == project["id"] and len(exp["memory"]) == 1 and exp["exported_at"]


async def test_memory_reaches_prompts_with_labels(ctx, project):
    ctx.memory.add(project["id"], "instruction", "Greeting style", "Always greet greeting people politely", "confirmed")
    ctx.memory.add(project["id"], "finding", "Greeting hunch", "greeting people might need localisation greeting", "hypothesis")
    await run_task(ctx, "Write a python function that greets people with a greeting", project["id"])
    prompts = [c.prompt for c in ctx.registry.get("anthropic").calls if c.purpose == "implementation"]
    assert prompts and "CONFIRMED PROJECT MEMORY" in prompts[0] and "Greeting style" in prompts[0]


def test_markdown_export_labels_unconfirmed_items(ctx, project):
    ctx.memory.add(project["id"], "decision", "Use SQLite", "single file", "confirmed")
    ctx.memory.add(project["id"], "finding", "Maybe slow", "might be slow", "hypothesis")
    md = ctx.memory.export_markdown(project["id"])
    assert md.startswith("# PROJECT.md: Sample") and "## Confirmed memory" in md
    assert md.index("Use SQLite") < md.index("Unconfirmed: hypotheses") < md.index("Maybe slow")
