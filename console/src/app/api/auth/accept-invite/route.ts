import { NextResponse } from "next/server";

import { callApi } from "@/lib/django";
import { writeSession } from "@/lib/session";

type TokenResponse = {
  access_token?: string;
  refresh_token?: string;
  school_id?: string;
  detail?: string;
  errors?: { field: string; detail: string }[];
};

const GENERIC_ERROR = "This invitation link is invalid or has expired.";

/** Pre-flight check: is this token still redeemable? Never consumes it. */
export async function GET(request: Request) {
  const token = new URL(request.url).searchParams.get("token") ?? "";
  if (!token) {
    return NextResponse.json({ valid: false });
  }

  const { status, body } = await callApi<{ valid?: boolean }>(
    `/auth/accept-invite?token=${encodeURIComponent(token)}`,
  );

  return NextResponse.json({ valid: status === 200 && body.valid === true });
}

export async function POST(request: Request) {
  const { token, password } = await request.json();

  const { status, body } = await callApi<TokenResponse>("/auth/accept-invite", {
    method: "POST",
    body: JSON.stringify({ token, password }),
  });

  if (status !== 200 || !body.access_token || !body.refresh_token) {
    // A weak password gets a specific, actionable message; an invalid token
    // gets the same generic message every time, whatever actually made it
    // invalid -- matching the API's own anti-enumeration discipline.
    const passwordError = body.errors?.find((e) => e.field === "password");
    return NextResponse.json(
      { detail: passwordError?.detail ?? body.detail ?? GENERIC_ERROR },
      { status: status === 200 ? 400 : status },
    );
  }

  await writeSession({
    access_token: body.access_token,
    refresh_token: body.refresh_token,
    school_id: body.school_id,
  });

  return NextResponse.json({ ok: true });
}
