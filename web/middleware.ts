import { auth } from "@/lib/auth";
import { NextResponse } from "next/server";

// Routes that don't require auth.
const PUBLIC = [
  "/",                  // marketing landing
  "/login",             // sign-in page
  "/api/auth",          // NextAuth callbacks
  "/_next",             // Next.js internals
  "/favicon.ico"
];

function isPublic(pathname: string): boolean {
  return PUBLIC.some(
    (p) => pathname === p || pathname.startsWith(p + "/") || pathname.startsWith(p + "?")
  );
}

export default auth((req) => {
  const { pathname, search } = req.nextUrl;

  if (isPublic(pathname)) return NextResponse.next();

  // Detect a session whose Keycloak refresh-token has failed. Without
  // this check the page renders with a stale access token, every
  // server-side API call 401s, and the user sees an empty dashboard
  // instead of being kicked back to sign-in.
  const sessionWithError = req.auth as
    | { error?: string }
    | null
    | undefined;
  if (sessionWithError?.error === "RefreshAccessTokenError") {
    const loginUrl = new URL("/login", req.nextUrl);
    loginUrl.searchParams.set("reason", "session_expired");
    if (pathname !== "/") {
      loginUrl.searchParams.set("callbackUrl", pathname + search);
    }
    return NextResponse.redirect(loginUrl);
  }

  if (req.auth) return NextResponse.next();

  // Not authenticated → bounce to /login with callback URL preserved.
  const loginUrl = new URL("/login", req.nextUrl);
  if (pathname !== "/") {
    loginUrl.searchParams.set("callbackUrl", pathname + search);
  }
  return NextResponse.redirect(loginUrl);
});

export const config = {
  matcher: [
    // Exclude static + image + favicon. Everything else gets the auth check.
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|webp|gif)$).*)"
  ]
};
