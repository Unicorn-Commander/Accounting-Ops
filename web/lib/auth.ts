import crypto from "crypto";
import NextAuth from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";
import KeycloakProvider from "next-auth/providers/keycloak";
import { authConfig } from "@/lib/auth.config";

const authMode = (process.env.AO_AUTH_MODE ?? "keycloak").toLowerCase();
const isLocalAuth = authMode === "local";

const issuer =
  process.env.KEYCLOAK_REALM_URL ??
  "https://keycloak.example.com/realms/uchub";
const clientId = process.env.KEYCLOAK_CLIENT_ID ?? "accounting-ops-app";
const clientSecret = process.env.KEYCLOAK_CLIENT_SECRET ?? "";

const LOCAL_ADMIN_EMAIL = (process.env.AO_LOCAL_ADMIN_EMAIL ?? "").toLowerCase();
const LOCAL_ADMIN_UC_UID = process.env.AO_LOCAL_ADMIN_UC_UID ?? "admin";
const LOCAL_ADMIN_PASSWORD_HASH = process.env.AO_LOCAL_ADMIN_PASSWORD_HASH ?? "";

async function verifyLocalPassword(password: string): Promise<boolean> {
  if (!LOCAL_ADMIN_PASSWORD_HASH) return false;
  const parts = LOCAL_ADMIN_PASSWORD_HASH.split(":");
  if (parts.length !== 3 || parts[0] !== "scrypt") return false;
  const [, saltHex, keyHex] = parts;
  try {
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
    return {
      id: LOCAL_ADMIN_UC_UID,
      email,
      uc_uid: LOCAL_ADMIN_UC_UID
    } as { id: string; email: string; uc_uid: string };
  }
});

export const { handlers, auth, signIn, signOut } = NextAuth({
  ...authConfig,
  providers: isLocalAuth ? [localCredentialsProvider] : [keycloakProvider]
});
