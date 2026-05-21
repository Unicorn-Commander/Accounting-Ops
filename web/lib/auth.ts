// Phase 0: NextAuth wiring stubbed. Phase 1 wires real Keycloak OIDC handlers.
// Reasoning: the v0.1 dev preview shows the agent-first UX with placeholder data;
// real auth integration is a Phase 1 deliverable alongside the MCP tool implementations.

export const handlers = {
  GET: async () => new Response(
    JSON.stringify({
      status: "phase-1",
      message: "NextAuth Keycloak handlers wire in during Phase 1.",
      provider: process.env.KEYCLOAK_REALM_URL ?? "https://auth.unicorncommander.ai/realms/uchub"
    }),
    { status: 200, headers: { "content-type": "application/json" } }
  ),
  POST: async () => new Response(null, { status: 501 })
};

export async function auth(): Promise<null> {
  return null;
}

export async function signIn(): Promise<void> {}
export async function signOut(): Promise<void> {}
