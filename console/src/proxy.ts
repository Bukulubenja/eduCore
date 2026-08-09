import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { REFRESH_COOKIE } from "@/lib/session";

/**
 * Route guard.
 *
 * Named `proxy`, not `middleware`: the file convention was renamed in
 * Next.js 16 and the old name is deprecated.
 *
 * This only checks that a session cookie exists -- it is a redirect, not an
 * authorisation decision. Every real check happens server-side in the API,
 * which re-evaluates permissions against the database on every request.
 */
export function proxy(request: NextRequest) {
  const signedIn = Boolean(request.cookies.get(REFRESH_COOKIE)?.value);
  const { pathname, search } = request.nextUrl;
  const onLoginPage = pathname === "/login";

  if (!signedIn && !onLoginPage) {
    const url = new URL("/login", request.url);
    if (pathname !== "/") {
      url.searchParams.set("next", `${pathname}${search}`);
    }
    return NextResponse.redirect(url);
  }

  if (signedIn && onLoginPage) {
    return NextResponse.redirect(new URL("/", request.url));
  }

  return NextResponse.next();
}

export const config = {
  // `manifest.webmanifest` and `sw.js` must stay reachable with no session:
  // a browser's install-banner and service-worker-registration checks fetch
  // both unauthenticated, and a 307 to `/login` for either silently breaks
  // "add to home screen" with no error a developer would ever see in the UI.
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|manifest.webmanifest|sw.js).*)",
  ],
};
