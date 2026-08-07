import { NextResponse } from "next/server";

import { callApi, refreshTokens } from "@/lib/django";
import { readSession, writeSession } from "@/lib/session";

/**
 * The only route the browser talks to.
 *
 * It attaches the access token server-side and, on a 401, spends the refresh
 * token once and retries. The client never holds a credential, and a token
 * expiring mid-session is invisible to the person using it -- which matters
 * on a 15-minute access token and a screen someone leaves open all morning.
 */

type Context = { params: Promise<{ path: string[] }> };

async function forward(request: Request, context: Context) {
  const { path } = await context.params;
  const target = `/${path.join("/")}${new URL(request.url).search}`;

  const session = await readSession();
  if (!session.access && !session.refresh) {
    return NextResponse.json(
      { type: "unauthenticated", title: "Sign in required", status: 401 },
      { status: 401 },
    );
  }

  const body =
    request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await request.text();

  let result = await callApi(target, {
    method: request.method,
    body,
    accessToken: session.access,
  });

  if (result.status === 401 && session.refresh) {
    const renewed = await refreshTokens(session.refresh);
    if (!renewed) {
      // Refresh rejected: either expired, or replayed and the whole family was
      // revoked. Either way this session is over and the client should be told
      // plainly rather than looped.
      return NextResponse.json(
        { type: "session-expired", title: "Signed out", status: 401 },
        { status: 401 },
      );
    }

    await writeSession(renewed);
    result = await callApi(target, {
      method: request.method,
      body,
      accessToken: renewed.access_token,
    });
  }

  return NextResponse.json(result.body, { status: result.status });
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
