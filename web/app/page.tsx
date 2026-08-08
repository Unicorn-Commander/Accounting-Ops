import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { auth } from "@/lib/auth";
import { getActiveContext } from "@/lib/active-context";
import { PremiumLanding } from "@/components/premium-landing";

export const metadata: Metadata = {
  title: "Accounting-Ops — Books that can answer for themselves",
  description:
    "Accounting-Ops is a verifiable financial operating system — append-only, tamper-evident books where every number shows its source. Agents do the work, you keep the authority: reconciliation, reporting, invoicing, and tax preparation on your own infrastructure.",
  // Customer production is unicorncommander.ai; magicunicorn.dev is the internal
  // deployment. Canonicalling to .dev pointed customers and crawlers at the
  // internal instance. metadataBase is required or Next resolves relative OG
  // images against http://localhost:3000.
  metadataBase: new URL("https://accounting-ops.unicorncommander.ai"),
  alternates: { canonical: "https://accounting-ops.unicorncommander.ai/" },
  openGraph: {
    title: "Accounting-Ops — Books that can answer for themselves",
    description:
      "Every number gets a source, a decision trail, and someone accountable. Your books. Your infrastructure. Your agents.",
    type: "website",
    url: "https://accounting-ops.unicorncommander.ai/",
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
