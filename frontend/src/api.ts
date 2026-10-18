export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

let token: string | null = null;

async function getToken(force = false): Promise<string> {
  if (!token || force) {
    const r = await fetch("/api/session");
    if (!r.ok) throw new ApiError("Cannot reach the AI Command Center server. Is it running?", r.status);
    token = (await r.json()).token as string;
  }
  return token;
}

export const sessionToken = () => getToken(false);

export function resetToken(): void {
  token = null;
}

async function send(path: string, init: RequestInit | undefined, force: boolean): Promise<Response> {
  const t = await getToken(force);
  return fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", "X-ACC-Token": t, ...(init?.headers ?? {}) },
  });
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await send(path, init, false);
    if (r.status === 401) r = await send(path, init, true);
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError("Cannot reach the AI Command Center server. Is it running?", 0);
  }
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(detail, r.status);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

export const post = <T,>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
export const put = <T,>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const patch = <T,>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const del = <T,>(path: string) => api<T>(path, { method: "DELETE" });
