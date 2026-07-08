// NextAuth.js v5 (beta). Two mutually-exclusive login backends, selected by
// AO_AUTH_MODE (mirrors the backend app/config.py flag):
//
//   keycloak (default) — SSO against the Keycloak `uchub` realm. Unchanged
//     behavior: access tokens are short-lived and refreshed via the
//     refresh_token grant; uc_uid is the canonical user id (mapped in Keycloak,
//     falling back to sub).
//
//   local (Community Edition self-host) — a single env-configured admin logs in
//     with email + password (no Keycloak/uchub). On success we mint an HS256
//     bearer signed with AO_LOCAL_AUTH_SECRET whose claims mirror a Keycloak
//     token (sub/uc_uid + this resource server's audience) so app/auth.py's
//     local-token branch validates it and every downstream RBAC check resolves
//     identically. The token is re-minted before expiry; there is no refresh
//     grant (nothing to refresh against).

import NextAuth, { type DefaultSession } from "next-auth";
import KeycloakProvider from "next-auth/providers/keycloak";
import CredentialsProvider from "next-auth/providers/credentials";

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
  "https://auth.unicorncommander.ai/realms/uchub";
const clientId = process.env.KEYCLOAK_CLIENT_ID ?? "accounting-ops-app";
const clientSecret = process.env.KEYCLOAK_CLIENT_SECRET ?? "";

// Local-auth config (only consulted when AO_AUTH_MODE=local).
const LOCAL_ADMIN_EMAIL = (process.env.AO_LOCAL_ADMIN_EMAIL ?? "").toLowerCase();
const LOCAL_ADMIN_UC_UID = process.env.AO_LOCAL_ADMIN_UC_UID ?? "admin";
// Password hash format: `scrypt:<saltHex>:<derivedKeyHex>`. Generate with
// scripts/gen_local_admin_hash.py (see DEPLOY / .env.example).
const LOCAL_ADMIN_PASSWORD_HASH = process.env.AO_LOCAL_ADMIN_PASSWORD_HASH ?? "";
const LOCAL_AUTH_SECRET = process.env.AO_LOCAL_AUTH_SECRET ?? "";
// The audience the backend validates the local token against — must equal the
// backend's ACCEPTED_AUDIENCE (default = KEYCLOAK_CLIENT_ID / accounting-ops-app).
const LOCAL_TOKEN_AUDIENCE =
  process.env.ACCEPTED_AUDIENCE ??
  process.env.KEYCLOAK_CLIENT_ID ??
  "accounting-ops-app";
const LOCAL_TOKEN_ISSUER = "ao-local";
// Local bearer lifetime (ms). Re-minted transparently on each near-expiry
// session read; 8h keeps re-signs infrequent without long-lived tokens.
const LOCAL_TOKEN_TTL_MS = 8 * 60 * 60 * 1000;

// Refresh ~30 seconds before expiry to avoid race conditions on the next call.
const REFRESH_LEEWAY_MS = 30_000;

/** Verify a plaintext password against the `scrypt:<salt>:<key>` env hash. */
async function verifyLocalPassword(password: string): Promise<boolean> {
  if (!LOCAL_ADMIN_PASSWORD_HASH) return false;
  const parts = LOCAL_ADMIN_PASSWORD_HASH.split(":");
  if (parts.length !== 3 || parts[0] !== "scrypt") return false;
  const [, saltHex, keyHex] = parts;
  try {
    // node:crypto is only reachable from the Node route handler (authorize),
    // never the edge middleware — so this dynamic import stays out of the edge
    // bundle.
    const crypto = await import("node:crypto");
    const salt = Buffer.from(saltHex, "hex");
    const expected = Buffer.from(keyHex, "hex");
    const derived = crypto.scryptSync(password, salt, expected.length);
    return (
      expected.length === derived.length &&
      crypto.timingSafeEqual(derived, expected)
    );
  } catch (err) {
    console.error("[auth] local password verify failed:", err);
    return false;
  }
}

/** Sign a fresh HS256 local bearer for the given uc_uid. jose is edge-safe. */
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
      // Keycloak rotates refresh tokens on every refresh — capture the new one.
      refreshToken: refreshed.refresh_token ?? refreshToken,
      error: undefined
    };
  } catch (err) {
    console.error("[auth] token refresh failed:", err);
    return { ...token, error: "RefreshAccessTokenError" };
  }
}

const keycloakProvider = KeycloakProvider({
  issuer,
  clientId,
  clientSecret,
  authorization: {
    params: { scope: "openid email profile" }
  }
});

const localCredentialsProvider = CredentialsProvider({
  id: "credentials",
  name: "Local admin",
  credentials: {
    email: { label: "Email", type: "email" },
    password: { label: "Password", type: "password" }
  },
  async authorize(credentials) {
    const email = String(credentials?.email ?? "").toLowerCase();
    const password = String(credentials?.password ?? "");
    if (!email || !password) return null;
    if (!LOCAL_ADMIN_EMAIL || email !== LOCAL_ADMIN_EMAIL) return null;
    if (!(await verifyLocalPassword(password))) return null;
    // Returned object seeds the jwt callback's `user` on initial sign-in.
    return {
      id: LOCAL_ADMIN_UC_UID,
      email,
      uc_uid: LOCAL_ADMIN_UC_UID
    } as { id: string; email: string; uc_uid: string };
  }
});

export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: true,
  providers: isLocalAuth ? [localCredentialsProvider] : [keycloakProvider],
  callbacks: {
    async jwt({ token, account, profile, user }) {
      const t = token as Record<string, unknown>;

      // ---- Local mode ----
      if (isLocalAuth) {
        // (1) Initial credentials sign-in: seed identity + mint the bearer.
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
        // (2) Subsequent reads: re-mint if the bearer is near expiry.
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

      // ---- Keycloak mode (unchanged) ----
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
