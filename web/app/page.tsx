import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { auth } from "@/lib/auth";
import { getActiveContext } from "@/lib/active-context";
import { PremiumLanding } from "@/components/premium-landing";

export const metadata: Metadata = {
  title: "Accounting-Ops — Books that can answer for themselves",
  description:
    "Accounting-Ops is a verifiable financial operating system — append-only, tamper-evident books where every number shows its source. Agents do the work, you keep the authority: reconciliation, reporting, invoicing, and tax preparation on your own infrastructure.",
  alternates: { canonical: "https://accounting-ops.magicunicorn.dev/" },
  openGraph: {
    title: "Accounting-Ops — Books that can answer for themselves",
    description:
      "Every number gets a source, a decision trail, and someone accountable. Your books. Your infrastructure. Your agents.",
    type: "website",
    images: ["/landing-assets/hero-dashboard.webp"]
  }
};

export default async function LandingPage() {
  // Signed-in users go into the product: straight to the dashboard when a
  // workspace exists, into first-run onboarding when none does. Only
  // unauthenticated visitors see the marketing landing.
  const session = await auth();
  if (session?.accessToken) {
    const ctx = await getActiveContext();
    redirect(ctx.activeWorkspace ? "/dashboard" : "/welcome");
  }

  return <PremiumLanding />;
}
