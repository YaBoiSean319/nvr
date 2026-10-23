/** One way to talk to the NVR API: same-origin cookies, JSON, typed errors. */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly body: unknown,
  ) {
    super(detail);
    this.name = "ApiError";
  }

  get unauthorized(): boolean {
    return this.status === 401;
  }
}

type Listener = () => void;
const sessionExpiredListeners = new Set<Listener>();

/** Notified when any request reports that the session is no longer valid. */
export function onSessionExpired(listener: Listener): () => void {
  sessionExpiredListeners.add(listener);
  return () => sessionExpiredListeners.delete(listener);
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Do not announce a 401 as an expired session (sign-in and setup checks). */
  quietUnauthorized?: boolean;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, quietUnauthorized } = options;
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      signal,
      credentials: "same-origin",
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "The NVR could not be reached. Check the network connection and try again.", null);
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  const parsed: unknown = text ? safeJson(text) : null;
  if (!response.ok) {
    if (response.status === 401 && !quietUnauthorized) sessionExpiredListeners.forEach(listener => listener());
    throw new ApiError(response.status, describeError(response.status, parsed), parsed);
  }
  return parsed as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function describeError(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map(item => (item as { msg?: string; loc?: unknown[] }).msg?.replace(/^Value error, /, "") ?? "Invalid value")
      .join(" ");
  }
  if (status === 403) return "You do not have permission to do that.";
  if (status === 404) return "That item no longer exists.";
  if (status >= 500) return "The NVR could not complete the request. Try again shortly.";
  return `Request failed (HTTP ${status}).`;
}

/**
 * Keep only the newest response when requests overlap (rapid seeks, typing filters):
 * each call aborts the previous one and older results can never overwrite newer ones.
 */
export function latestOnly<A extends unknown[], T>(run: (signal: AbortSignal, ...args: A) => Promise<T>) {
  let controller: AbortController | null = null;
  return async (...args: A): Promise<T | undefined> => {
    controller?.abort();
    const current = new AbortController();
    controller = current;
    try {
      const result = await run(current.signal, ...args);
      return controller === current ? result : undefined;
    } catch (error) {
      if (current.signal.aborted) return undefined;
      throw error;
    }
  };
}
