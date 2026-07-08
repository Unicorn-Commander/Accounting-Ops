"use server";

// Server action backing the first-run /welcome flow.
//
// Calls the (already-deployed) provisioning endpoint with the caller's uchub
// Bearer, sets the active-workspace cookie, then redirects into the existing
// /setup walkthrough. The redirect MUST live OUTSIDE the try/catch — Next's
// redirect() throws a control-flow signal that a catch block would swallow.

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type WelcomeState = { error?: string };

export async function createFirstWorkspace(
  _prev: WelcomeState,
  formData: FormData,
): Promise<WelcomeState> {
  const displayName = String(formData.get("display_name") ?? "").trim();
  if (!displayName) return { error: "Please enter a name for your workspace." };
  const session = await auth();
  if (!session?.accessToken) redirect("/login");

  let workspaceId: string;
  try {
    const res = await fetch(`${INTERNAL_BASE}/api/onboarding/provision-first-workspace`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ display_name: displayName }),
      cache: "no-store",
    });
    if (!res.ok) {
      return { error: `Could not create your workspace (error ${res.status}). Please try again.` };
    }
    const data = (await res.json()) as { workspace_id: string };
    workspaceId = data.workspace_id;
  } catch {
    return { error: "Could not reach the server. Please try again." };
  }

  const jar = await cookies();
  jar.set("aops_workspace", workspaceId, {
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
    sameSite: "lax",
  });
  redirect("/setup?from_welcome=true");
}
