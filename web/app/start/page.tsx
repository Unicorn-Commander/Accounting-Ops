import { redirect } from "next/navigation";
import { getPersona } from "@/lib/api";

// Login-time landing router. Keycloak sends users here after sign-in (the
// default callbackUrl in /login). A CPA who carries memberships across many
// client workspaces lands on the cross-client practice view; everyone else
// lands on their single-workspace dashboard. This runs ONCE per sign-in (a
// cheap membership-only persona check), not on every navigation.
export const dynamic = "force-dynamic";

export default async function StartPage() {
  const persona = await getPersona();
  if (persona?.persona === "cpa") {
    redirect("/clients");
  }
  redirect("/dashboard");
}
