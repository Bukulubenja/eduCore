"use client";

/** Browser-side calls. Always through /api/proxy, never straight to the API. */

export class ApiError extends Error {
  status: number;
  type: string;

  constructor(status: number, type: string, message: string) {
    super(message);
    this.status = status;
    this.type = type;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/proxy${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });

  const body = await response.json().catch(() => null);

  if (!response.ok) {
    if (response.status === 401) {
      // The refresh token is gone or was revoked. Send them to sign in rather
      // than leaving a screen of empty panels with no explanation.
      window.location.href = "/login";
    }
    throw new ApiError(
      response.status,
      body?.type ?? "error",
      body?.detail ?? "Something went wrong. Try again.",
    );
  }

  return body as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
};
