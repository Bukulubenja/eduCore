/**
 * Server-side calls to the eduCore API.
 *
 * Only ever imported by route handlers. Nothing here runs in the browser, so
 * the API's base URL and the caller's tokens stay on the server.
 */

export const API_BASE =
  process.env.EDUCORE_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

export type ApiResult<T = unknown> = {
  status: number;
  body: T;
};

export async function callApi<T = unknown>(
  path: string,
  init: RequestInit & { accessToken?: string | null } = {},
): Promise<ApiResult<T>> {
  const { accessToken, headers, ...rest } = init;

  const response = await fetch(`${API_BASE}/api/v1${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...headers,
    },
    cache: "no-store",
  });

  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // The API answers in JSON or problem+json. Anything else is a proxy or
      // gateway page, and passing it through as a string keeps the failure
      // legible instead of turning it into a parse error.
      body = { detail: text.slice(0, 500) };
    }
  }

  return { status: response.status, body: body as T };
}

/** Exchange a refresh token for a new pair. Returns null if it was rejected. */
export async function refreshTokens(refresh: string) {
  const { status, body } = await callApi<{
    access_token: string;
    refresh_token: string;
  }>("/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refresh }),
  });

  return status === 200 ? body : null;
}
