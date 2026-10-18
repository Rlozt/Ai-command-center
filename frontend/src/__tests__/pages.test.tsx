import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "../pages/Dashboard";
import { Approvals } from "../pages/Approvals";
import { Settings } from "../pages/Settings";
import { TaskInput } from "../components/TaskInput";
import { dashboard, mockApi, proposal, settings } from "./helpers";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Dashboard", () => {
  it("shows a loading state, then the three agents", async () => {
    mockApi({ "/api/dashboard": { body: dashboard() }, "/api/projects": { body: [] } });
    render(<Dashboard />);
    expect(screen.getByText(/Loading dashboard/)).toBeInTheDocument();
    expect(await screen.findByText(/OpenAI · /)).toBeInTheDocument();
    expect(screen.getByText(/Perplexity · /)).toBeInTheDocument();
    expect(screen.getByText(/Claude · /)).toBeInTheDocument();
  });

  it("shows an error with a retry when the server is unreachable", async () => {
    mockApi({ "/api/dashboard": { status: 500, body: { detail: "database is locked" } }, "/api/projects": { body: [] } });
    render(<Dashboard />);
    expect(await screen.findByRole("alert")).toHaveTextContent("database is locked");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("labels demo mode so nobody mistakes mock output for real work", async () => {
    mockApi({ "/api/dashboard": { body: dashboard({ mode: "demo" }) }, "/api/projects": { body: [] } });
    render(<Dashboard />);
    expect(await screen.findByText(/offline mock providers/)).toBeInTheDocument();
  });

  it("lists active tasks with their status", async () => {
    const task = { id: "t_1", title: "Compare options", status: "running", outcome: "", paused: false, pause_reason: "", created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z", subtasks_done: 1, subtasks_total: 3, agents: ["perplexity"], error: "", cost: 0, demo: false, local: false };
    mockApi({ "/api/dashboard": { body: dashboard({ current_tasks: [task] }) }, "/api/projects": { body: [] } });
    render(<Dashboard />);
    expect(await screen.findByText("Compare options")).toBeInTheDocument();
    expect(screen.getByText(/1\/3 subtasks/)).toBeInTheDocument();
  });
});

describe("Approvals", () => {
  it("explains the empty state", async () => {
    mockApi({ "/api/approvals": { body: [] } });
    render(<Approvals />);
    expect(await screen.findByText("Nothing to approve")).toBeInTheDocument();
  });

  it("approving sends the exact content hash that was reviewed", async () => {
    const calls = mockApi({ "/api/approvals": { body: [proposal()] }, "/api/proposals/p_1/approve": { body: { ok: true } } });
    render(<Approvals />);
    await screen.findByText("greet.py");
    await userEvent.click(screen.getByRole("button", { name: "Approve and apply" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/proposals/p_1/approve")).toBe(true));
    const call = calls.find((c) => c.url === "/api/proposals/p_1/approve")!;
    expect(JSON.parse(call.init!.body as string)).toMatchObject({ content_hash: "abcdef1234567890", acknowledge_issues: false });
    expect((call.init!.headers as Record<string, string>)["X-ACC-Token"]).toBe("test-token");
  });

  it("shows the server's message when the proposal changed (409) and does not hide it", async () => {
    mockApi({ "/api/approvals": { body: [proposal()] }, "/api/proposals/p_1/approve": { status: 409, body: { detail: "the proposal changed since you reviewed it" } } });
    render(<Approvals />);
    await screen.findByText("greet.py");
    await userEvent.click(screen.getByRole("button", { name: "Approve and apply" }));
    expect(await screen.findByText(/changed since you reviewed it/)).toBeInTheDocument();
  });

  it("blocks approval of a proposal with unresolved issues until the user acknowledges them", async () => {
    const calls = mockApi({ "/api/approvals": { body: [proposal({ unresolved_issues: 2 })] }, "/api/proposals/p_1/approve": { body: {} } });
    render(<Approvals />);
    await screen.findByText(/unresolved review issues/);
    const btn = screen.getByRole("button", { name: "Approve and apply" });
    expect(btn).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox"));
    expect(btn).toBeEnabled();
    await userEvent.click(btn);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/approve"))).toBe(true));
    expect(JSON.parse(calls.find((c) => c.url.endsWith("/approve"))!.init!.body as string).acknowledge_issues).toBe(true);
  });

  it("reject sends the note and asks for a revision", async () => {
    const calls = mockApi({ "/api/approvals": { body: [proposal()] }, "/api/proposals/p_1/reject": { body: {} } });
    render(<Approvals />);
    await screen.findByText("greet.py");
    fireEvent.change(screen.getByPlaceholderText(/Optional note/), { target: { value: "add a docstring" } });
    await userEvent.click(screen.getByRole("button", { name: "Reject and request revision" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/reject"))).toBe(true));
    expect(JSON.parse(calls.find((c) => c.url.endsWith("/reject"))!.init!.body as string)).toEqual({ note: "add a docstring", request_revision: true });
  });
});

describe("TaskInput", () => {
  it("does not submit very short requests and submits real ones", async () => {
    const calls = mockApi({ "/api/tasks": { status: 201, body: { id: "t_9" } } });
    const onCreated = vi.fn();
    render(<TaskInput projects={[]} onCreated={onCreated} />);
    const box = screen.getByLabelText(/Describe what you want done/);
    await userEvent.type(box, "hi");
    fireEvent.submit(box.closest("form")!);
    expect(calls.filter((c) => c.url === "/api/tasks")).toHaveLength(0);
    await userEvent.clear(box);
    await userEvent.type(box, "Research three scheduling approaches");
    fireEvent.submit(box.closest("form")!);
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("t_9"));
  });

  it("shows the server error when a task is rejected", async () => {
    mockApi({ "/api/tasks": { status: 404, body: { detail: "project not found" } } });
    render(<TaskInput projects={[]} onCreated={() => {}} />);
    const box = screen.getByLabelText(/Describe what you want done/);
    await userEvent.type(box, "Do something useful");
    fireEvent.submit(box.closest("form")!);
    expect(await screen.findByText(/project not found/)).toBeInTheDocument();
  });
});

describe("Settings", () => {
  it("shows which credentials are missing and names the env var, never a key", async () => {
    mockApi({ "/api/settings": { body: settings() } });
    render(<Settings />);
    expect(await screen.findByText(/missing: set OPENAI_API_KEY/)).toBeInTheDocument();
    expect(screen.getByText(/missing: set PERPLEXITY_API_KEY/)).toBeInTheDocument();
    expect(screen.getByText("configured")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/sk-[A-Za-z0-9]/);
    expect(screen.queryByLabelText(/api key/i)).toBeNull();
  });

  it("explains that subscriptions do not include API access", async () => {
    mockApi({ "/api/settings": { body: settings() } });
    render(<Settings />);
    expect(await screen.findByText(/subscription does not include API access/)).toBeInTheDocument();
  });

  it("saving sends limits and provider toggles, and surfaces validation errors", async () => {
    const calls = mockApi({ "/api/settings": (_url, init) => (init?.method === "PUT" ? { status: 400, body: { detail: "per_task_usd must be non-negative" } } : { body: settings() }) });
    render(<Settings />);
    await screen.findByText("Providers");
    await userEvent.click(screen.getByLabelText("Enable Perplexity"));
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(await screen.findByText(/must be non-negative/)).toBeInTheDocument();
    const put = calls.find((c) => c.init?.method === "PUT")!;
    expect(JSON.parse(put.init!.body as string).providers_enabled.perplexity).toBe(false);
  });
});

describe("Local model", () => {
  it("settings names LOCAL_MODEL, shows the local option as free, and has no daily dollar limit for it", async () => {
    const base = settings();
    const s = settings({
      providers_enabled: { ...base.providers_enabled, local: false },
      providers_status: [...base.providers_status, { provider: "local", role: "Local model", enabled: false, configured: false, ready: false, demo: false, model: "", detail: "", validated: null }],
      credentials: { ...base.credentials, local: "missing" }, models: { ...base.models, local: "" },
    });
    mockApi({ "/api/settings": { body: s } });
    render(<Settings />);
    expect(await screen.findByText(/missing: set LOCAL_MODEL/)).toBeInTheDocument();
    expect(screen.getByText("Ollama")).toBeInTheDocument();
    expect(screen.getByText("none (free)")).toBeInTheDocument();
    expect(screen.queryByLabelText("Local model daily limit")).toBeNull();
  });

  it("dashboard shows the local card without crashing and counts local calls separately from demo calls", async () => {
    const d = dashboard();
    d.providers = [...d.providers, { provider: "local", role: "Local model", enabled: true, configured: true, ready: true, demo: false, model: "llama3.1:8b", detail: "", validated: null }];
    d.usage = { ...d.usage, local_calls_today: 7 };
    mockApi({ "/api/dashboard": { body: d }, "/api/projects": { body: [] } });
    render(<Dashboard />);
    expect(await screen.findByText(/Ollama · /)).toBeInTheDocument();
    expect(screen.getByText("Ollama calls today (not billed here)")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });
});
