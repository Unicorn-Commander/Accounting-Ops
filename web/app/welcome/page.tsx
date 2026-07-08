// /welcome — first-run, self-service workspace provisioning.
//
// Server Component:
//  - If the caller already has an active workspace, they don't belong here →
//    bounce to /dashboard.
//  - Otherwise derive a friendly default workspace name from the signed-in
//    user's profile and render the centered single-field WelcomeClient.
//
// Deliberately does NOT use AppShell: AppShell assumes a workspace + sidebar,
// which by definition don't exist on this route.

import { redirect } from "next/navigation";
import { Sparkles } from "lucide-react";
import { Card } from "@/components/primitives";
import { auth } from "@/lib/auth";
import { getActiveContext } from "@/lib/active-context";
import { WelcomeClient } from "./welcome-client";

export const dynamic = "force-dynamic";

export default async function WelcomePage() {
  const ctx = await getActiveContext();
  if (ctx.activeWorkspace) redirect("/dashboard");

  const session = await auth();
  const defaultName =
    session?.user?.name ?? session?.user?.email?.split("@")[0] ?? "";

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-4">
      <Card className="w-full max-w-lg">
        <div className="mb-6 flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-500/15 text-brand-300">
            <Sparkles size={18} />
          </span>
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-ink-primary">
              Welcome to Accounting-Ops
            </h1>
            <p className="mt-1 text-sm text-ink-secondary">
              Let&apos;s set up your books — about a minute.
            </p>
          </div>
        </div>

        <WelcomeClient defaultName={defaultName} />
      </Card>
    </main>
  );
}
