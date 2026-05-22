// NextAuth.js v5 (beta) wired to Keycloak `uchub` realm.
// uc_uid is the canonical user identifier across the Magic Unicorn ecosystem
// (set as a user-attribute mapper in Keycloak; falls back to sub if missing).
//
// Token refresh: Keycloak access tokens are short-lived (default 5min). When
// the JWT callback sees a token within the 30s leeway window, it refreshes
// via the refresh_token grant. If the refresh fails (refresh token expired
// or revoked), session is marked with an error and middleware sends the
// user to /login on the next protected request.

import NextAuth, { type DefaultSession } from "next-auth";
import KeycloakProvider from "next-auth/providers/keycloak";

declare module "next-auth" {
  interface Session {
    user: {
      uc_uid?: string;
      keycloak_sub?: string;
    } & DefaultSession["user"];
    accessToken?: string;
    error?: "RefreshAccessTokenError";
  }
}

const issuer =
  process.env.KEYCLOAK_REALM_URL ??
  "https://auth.unicorncommander.ai/realms/uchub";
const clientId = process.env.KEYCLOAK_CLIENT_ID ?? "accounting-ops-app";
const clientSecret = process.env.KEYCLOAK_CLIENT_SECRET ?? "";

// Refresh ~30 seconds before expiry to avoid race conditions on the next call.
const REFRESH_LEEWAY_MS = 30_000;

async function refreshAccessToken(token: Record<string, unknown>): Promise<Record<string, unknown>> {
  const refreshToken = token.refreshToken as string | undefined;
  if (!refreshToken) {
    return { ...token, error: "RefreshAccessTokenError" };
  }
  try {
    const params = new URLSearchParams();
    params.set("grant_type", "refresh_token");
    params.set("refresh_token", refreshToken);
    params.set("client_id", clientId);
    if (clientSecret) params.set("client_secret", clientSecret);

    const res = await fetch(`${issuer}/protocol/openid-connect/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: params.toString(),
      cache: "no-store"
    });
    const refreshed = (await res.json()) as {
      access_token?: string;
      expires_in?: number;
      refresh_token?: string;
      refresh_expires_in?: number;
      error?: string;
    };
    if (!res.ok || !refreshed.access_token) {
      throw new Error(refreshed.error || `HTTP ${res.status}`);
    }
    return {
      ...token,
      accessToken: refreshed.access_token,
      accessTokenExpires: Date.now() + (refreshed.expires_in ?? 300) * 1000,
      // Keycloak rotates refresh tokens on every refresh — capture the new one.
      refreshToken: refreshed.refresh_token ?? refreshToken,
      error: undefined
    };
  } catch (err) {
    console.error("[auth] token refresh failed:", err);
    return { ...token, error: "RefreshAccessTokenError" };
  }
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: true,
  providers: [
    KeycloakProvider({
      issuer,
      clientId,
      clientSecret,
      authorization: {
        params: { scope: "openid email profile" }
      }
    })
  ],
  callbacks: {
    async jwt({ token, account, profile }) {
      const t = token as Record<string, unknown>;

      // (1) Initial sign-in: capture all the fields once.
      if (account && profile) {
        const claims = profile as Record<string, unknown>;
        t.uc_uid = (claims.uc_uid as string | undefined) ?? (claims.sub as string);
        t.keycloak_sub = claims.sub as string;
        t.accessToken = account.access_token;
        t.refreshToken = account.refresh_token;
        t.accessTokenExpires = account.expires_at
          ? account.expires_at * 1000
          : undefined;
        return t;
      }

      // (2) Token still fresh: return as-is.
      const expiresAt = t.accessTokenExpires as number | undefined;
      if (expiresAt && Date.now() < expiresAt - REFRESH_LEEWAY_MS) {
        return t;
      }

      // (3) Token expired or near-expiry: refresh.
      return refreshAccessToken(t);
    },
    async session({ session, token }) {
      const t = token as Record<string, unknown>;
      session.user.uc_uid = t.uc_uid as string | undefined;
      session.user.keycloak_sub = t.keycloak_sub as string | undefined;
      session.accessToken = t.accessToken as string | undefined;
      if (t.error) {
        session.error = t.error as "RefreshAccessTokenError";
      }
      return session;
    }
  },
  pages: {
    signIn: "/login",
    error: "/login"
  }
});
