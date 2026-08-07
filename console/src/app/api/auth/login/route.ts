import { NextResponse } from "next/server";

import { callApi } from "@/lib/django";
import { writeSession } from "@/lib/session";

type TokenResponse = {
  access_token?: string;
  refresh_token?: string;
  school_id?: string;
  choose_membership?: boolean;
  memberships?: { id: string; school: string; school_id: string }[];
  detail?: string;
};

export async function POST(request: Request) {
  const { email, password, membership_id } = await request.json();

  const { status, body } = await callApi<TokenResponse>("/auth/token", {
    method: "POST",
    body: JSON.stringify({ email, password, membership_id }),
  });

  // 300: this person teaches at more than one school and the API refuses to
  // guess. Pass the choice through rather than picking, or someone marks the
  // wrong school's register and never finds out.
  if (status === 300) {
    return NextResponse.json(body, { status: 200 });
  }

  if (status !== 200 || !body.access_token || !body.refresh_token) {
    return NextResponse.json(
      { detail: body.detail ?? "Email or password is incorrect." },
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
