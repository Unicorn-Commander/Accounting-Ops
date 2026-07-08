import type { DefaultSession, NextAuthConfig } from "next-auth";

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

const authMode = (process.env.AO_AUTH_MODE ?? "keycloak").toLowerCase();
const isLocalAuth = authMode === "local";

const issuer =
  process.env.KEYCLOAK_REALM_URL ??
  "https://keycloak.example.com/realms/uchub";
const clientId = process.env.KEYCLOAK_CLIENT_ID ?? "accounting-ops-app";
const clientSecret = process.env.KEYCLOAK_CLIENT_SECRET ?? "";

const LOCAL_ADMIN_UC_UID = process.env.AO_LOCAL_ADMIN_UC_UID ?? "admin";
const LOCAL_AUTH_SECRET = process.env.AO_LOCAL_AUTH_SECRET ?? "";
const LOCAL_TOKEN_AUDIENCE =
  process.env.ACCEPTED_AUDIENCE ??
  process.env.KEYCLOAK_CLIENT_ID ??
  "accounting-ops-app";
const LOCAL_TOKEN_ISSUER = "ao-local";
const LOCAL_TOKEN_TTL_MS = 8 * 60 * 60 * 1000;

const REFRESH_LEEWAY_MS = 30_000;

async function mintLocalToken(
  ucUid: string
): Promise<{ token: string; expires: number } | null> {
  if (!LOCAL_AUTH_SECRET) {
    console.error("[auth] AO_LOCAL_AUTH_SECRET unset — cannot mint local token");
    return null;
  }
  const { SignJWT } = await import("jose");
  const secret = new TextEncoder().encode(LOCAL_AUTH_SECRET);
  const nowSec = Math.floor(Date.now() / 1000);
  const expiresMs = Date.now() + LOCAL_TOKEN_TTL_MS;
  const token = await new SignJWT({
    uc_uid: ucUid,
    scope: "accounting:read accounting:write accounting:admin"
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer(LOCAL_TOKEN_ISSUER)
    .setSubject(ucUid)
    .setAudience(LOCAL_TOKEN_AUDIENCE)
    .setIssuedAt(nowSec)
    .setExpirationTime(Math.floor(expiresMs / 1000))
    .sign(secret);
  return { token, expires: expiresMs };
}

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
      refreshToken: refreshed.refresh_token ?? refreshToken,
      error: undefined
    };
  } catch (err) {
    console.error("[auth] token refresh failed:", err);
    return { ...token, error: "RefreshAccessTokenError" };
  }
}

export const authConfig = {
  trustHost: true,
  providers: [],
  callbacks: {
    async jwt({ token, account, profile, user }) {
      const t = token as Record<string, unknown>;

      if (isLocalAuth) {
        if (account?.provider === "credentials" && user) {
          const u = user as { uc_uid?: string; email?: string };
          const ucUid = u.uc_uid ?? LOCAL_ADMIN_UC_UID;
          t.local = true;
          t.uc_uid = ucUid;
          t.email = u.email;
          const minted = await mintLocalToken(ucUid);
          if (minted) {
            t.accessToken = minted.token;
            t.accessTokenExpires = minted.expires;
          }
          return t;
        }
        if (t.local) {
          const expiresAt = t.accessTokenExpires as number | undefined;
          if (expiresAt && Date.now() < expiresAt - REFRESH_LEEWAY_MS) {
            return t;
          }
          const minted = await mintLocalToken((t.uc_uid as string) ?? LOCAL_ADMIN_UC_UID);
          if (minted) {
            t.accessToken = minted.token;
            t.accessTokenExpires = minted.expires;
          }
          return t;
        }
        return t;
      }

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

      const expiresAt = t.accessTokenExpires as number | undefined;
      if (expiresAt && Date.now() < expiresAt - REFRESH_LEEWAY_MS) {
        return t;
      }

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
} satisfies NextAuthConfig;
