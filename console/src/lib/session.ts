import { cookies } from "next/headers";

/**
 * Session handling for the console.
 *
 * Tokens live in httpOnly cookies set by this app's own route handlers, never
 * in localStorage. Doc 06 treats a 30-day refresh token as a credential, and a
 * credential reachable from page JavaScript is one XSS away from a persistent
 * account takeover that survives a password change.
 *
 * The browser therefore never sees a token at all: it calls /api/proxy/*, and
 * the server attaches the Authorization header.
 */

export const ACCESS_COOKIE = "ec_at";
export const REFRESH_COOKIE = "ec_rt";
export const SCHOOL_COOKIE = "ec_school";
export const ROLES_COOKIE = "ec_roles";

const secure = process.env.NODE_ENV === "production";

const BASE = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure,
  path: "/",
};

export type Tokens = {
  access_token: string;
  refresh_token: string;
  school_id?: string;
};

/**
 * Read the `rls` (role codes) claim straight off the access token, with no
 * signature check.
 *
 * This is presentation only -- which nav links to show, which forms to
 * render read-only -- never an authorisation decision. The server re-checks
 * every real permission on every request regardless of what this returns
 * (see `proxy.ts`'s own note on the same principle for the cookie-presence
 * check). Verifying the signature here would buy nothing: the token just
 * came from this app's own trusted backend over HTTPS, not from the user.
 */
function decodeRoles(accessToken: string): string[] {
  try {
    const payload = accessToken.split(".")[1];
    const json = Buffer.from(payload, "base64url").toString("utf-8");
    const claims = JSON.parse(json) as { rls?: unknown };
    return Array.isArray(claims.rls) ? claims.rls.filter((r) => typeof r === "string") : [];
  } catch {
    return [];
  }
}

export async function readSession() {
  const jar = await cookies();
  return {
    access: jar.get(ACCESS_COOKIE)?.value ?? null,
    refresh: jar.get(REFRESH_COOKIE)?.value ?? null,
    schoolId: jar.get(SCHOOL_COOKIE)?.value ?? null,
  };
}

export async function writeSession(tokens: Tokens) {
  const jar = await cookies();
  // Access tokens last 15 minutes server-side; the cookie is given the same
  // life so a stale one is dropped rather than sent and rejected.
  jar.set(ACCESS_COOKIE, tokens.access_token, { ...BASE, maxAge: 15 * 60 });
  jar.set(REFRESH_COOKIE, tokens.refresh_token, { ...BASE, maxAge: 30 * 86400 });
  if (tokens.school_id) {
    // Readable by the client: it names which school is on screen, which is not
    // a secret and saves a round trip on every page.
    jar.set(SCHOOL_COOKIE, tokens.school_id, {
      ...BASE,
      httpOnly: false,
      maxAge: 30 * 86400,
    });
  }
  // Same non-secret, readable-by-client rationale as SCHOOL_COOKIE above:
  // role codes aren't sensitive, and every page that reads this for a UI
  // decision (nav filtering, read-only forms) is still enforced for real by
  // the API on the actual request.
  jar.set(ROLES_COOKIE, JSON.stringify(decodeRoles(tokens.access_token)), {
    ...BASE,
    httpOnly: false,
    maxAge: 30 * 86400,
  });
}

export async function clearSession() {
  const jar = await cookies();
  for (const name of [ACCESS_COOKIE, REFRESH_COOKIE, SCHOOL_COOKIE, ROLES_COOKIE]) {
    jar.delete(name);
  }
}
