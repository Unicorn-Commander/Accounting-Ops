// NextAuth.js v5 (beta) wired to Keycloak `uchub` realm.
// uc_uid is the canonical user identifier across the Magic Unicorn ecosystem
// (set as a user-attribute mapper in Keycloak; falls back to sub if missing).

import NextAuth, { type DefaultSession } from "next-auth";
import KeycloakProvider from "next-auth/providers/keycloak";

declare module "next-auth" {
  interface Session {
    user: {
      uc_uid?: string;
      keycloak_sub?: string;
    } & DefaultSession["user"];
    accessToken?: string;
  }
}

// JWT shape is augmented through the session() callback below; explicit
// next-auth/jwt module augmentation is omitted because the v5-beta module
// layout doesn't always expose the JWT type at a stable path.

const issuer =
  process.env.KEYCLOAK_REALM_URL ??
  "https://auth.unicorncommander.ai/realms/uchub";
const clientId = process.env.KEYCLOAK_CLIENT_ID ?? "accounting-ops-app";
const clientSecret = process.env.KEYCLOAK_CLIENT_SECRET ?? "";

export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: true,
  providers: [
    KeycloakProvider({
      issuer,
      clientId,
      clientSecret,
      // Always pull a fresh refresh token; defaults to false in v5 beta.
      authorization: {
        params: {
          scope: "openid email profile",
        },
      },
    }),
  ],
  callbacks: {
    async jwt({ token, account, profile }) {
      // On initial sign-in, capture uc_uid + access token + refresh token.
      const t = token as Record<string, unknown>;
      if (account && profile) {
        const claims = profile as Record<string, unknown>;
        t.uc_uid =
          (claims.uc_uid as string | undefined) ?? (claims.sub as string);
        t.keycloak_sub = claims.sub as string;
        t.accessToken = account.access_token;
        t.refreshToken = account.refresh_token;
        t.accessTokenExpires = account.expires_at
          ? account.expires_at * 1000
          : undefined;
      }
      return token;
    },
    async session({ session, token }) {
      const t = token as Record<string, unknown>;
      session.user.uc_uid = t.uc_uid as string | undefined;
      session.user.keycloak_sub = t.keycloak_sub as string | undefined;
      session.accessToken = t.accessToken as string | undefined;
      return session;
    },
  },
  pages: {
    signIn: "/login",
    error: "/login",
  },
});
