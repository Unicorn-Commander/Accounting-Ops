import NextAuth from "next-auth";
import Keycloak from "next-auth/providers/keycloak";

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    Keycloak({
      issuer: process.env.KEYCLOAK_REALM_URL ?? "https://auth.unicorncommander.ai/realms/uchub",
      clientId: process.env.KEYCLOAK_CLIENT_ID ?? "accounting-ops-app",
      clientSecret: process.env.KEYCLOAK_CLIENT_SECRET ?? ""
    })
  ],
  callbacks: {
    jwt({ token, profile }) {
      if (profile?.sub) {
        token.uc_uid = profile.sub;
      }
      return token;
    },
    session({ session, token }) {
      session.user.uc_uid = token.uc_uid as string | undefined;
      return session;
    }
  }
});
