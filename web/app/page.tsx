import Link from "next/link";
import { redirect } from "next/navigation";
import { APP_VERSION_LABEL } from "@/lib/version";
import {
  ArrowUpRight,
  CircleDashed,
  Cpu,
  FileCheck,
  Layers,
  Lock,
  Network,
  Plug,
  Scale,
  ServerCog,
  Terminal
} from "lucide-react";
import { SiteHeader } from "@/components/site-header";
import { auth } from "@/lib/auth";
import { getActiveContext } from "@/lib/active-context";

export default async function LandingPage() {
  // Signed-in users with no workspace yet get routed straight into the
  // self-service first-run flow. Unauthenticated visitors and already-onboarded
  // users (who have a workspace) fall through to the marketing landing page.
  const session = await auth();
  if (session?.accessToken) {
    const ctx = await getActiveContext();
    if (!ctx.activeWorkspace) redirect("/welcome");
  }

  return (
    <>
      <SiteHeader variant="marketing" />
      <main className="overflow-x-clip">
        <Hero />
        <Positioning />
        <Features />
        <HowItWorks />
        <Audience />
        <Comparison />
        <Open />
        <Footer />
      </main>
    </>
  );
}

function Hero() {
  return (
    <section className="relative">
      <div className="grid-bg absolute inset-0" />
      <div className="absolute inset-x-0 top-0 h-[420px] bg-gradient-to-b from-brand-500/[0.07] to-transparent" />
      <div className="relative mx-auto max-w-7xl px-6 pt-24 pb-28 lg:pt-32 lg:pb-32">
        <div className="mx-auto max-w-3xl text-center">
          <span className="inline-flex items-center gap-2 rounded-full border border-subtle bg-surface/60 px-3 py-1 text-[11px] font-medium uppercase tracking-wider text-ink-tertiary">
            <span className="h-1.5 w-1.5 rounded-full bg-credit shadow-[0_0_6px_hsl(157_64%_52%_/_0.8)]" />
            {APP_VERSION_LABEL} // early access, opinionated, not for everyone
          </span>
          <h1 className="mt-7 text-balance text-5xl font-semibold tracking-tight md:text-6xl lg:text-7xl">
            Your books. Your machine. Your rules.
          </h1>
          <p className="mt-7 text-pretty text-lg leading-relaxed text-ink-secondary md:text-xl">
            Accounting-Ops is the agent-first financial operations platform for operators who refuse to send their books to a frontier AI provider. Self-hostable. Multi-entity from day one. Audit-defensible by construction.
          </p>
          <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
            <a
              href="mailto:aaron@magicunicorn.tech?subject=Accounting-Ops dev preview"
              className="inline-flex items-center gap-2 rounded-md bg-brand-500 px-5 py-2.5 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600 transition"
            >
              Request dev-preview access
              <ArrowUpRight size={15} />
            </a>
            <Link
              href="/dashboard"
              className="inline-flex items-center gap-2 rounded-md border border-strong bg-surface px-5 py-2.5 text-sm font-medium text-ink-primary hover:bg-elevated transition"
            >
              Read the architecture
            </Link>
          </div>
        </div>

        <div className="relative mx-auto mt-20 max-w-5xl">
          <div className="absolute -inset-x-12 -inset-y-8 bg-gradient-to-tr from-brand-500/10 via-transparent to-agent-500/10 blur-2xl" />
          <div className="relative overflow-hidden rounded-xl border border-default bg-surface shadow-card">
            <div className="flex items-center gap-2 border-b border-subtle px-4 py-2.5">
              <span className="h-2.5 w-2.5 rounded-full bg-debit/60" />
              <span className="h-2.5 w-2.5 rounded-full bg-pending/60" />
              <span className="h-2.5 w-2.5 rounded-full bg-credit/60" />
              <span className="ml-3 font-mono text-[11px] text-ink-tertiary">
                accounting-ops.magicunicorn.dev
              </span>
              <span className="ml-auto flex items-center gap-1.5 text-[10px] text-ink-tertiary">
                <span className="h-1.5 w-1.5 rounded-full bg-agent-500 animate-pulse-soft shadow-agent-glow" />
                3 agents working
              </span>
            </div>
            <div className="grid grid-cols-1 gap-px bg-elevated md:grid-cols-3">
              <PreviewKpi label="Cash position" value="$487,193" delta="+$18.2k week" tone="credit" />
              <PreviewKpi label="Runway" value="14.2 mo" delta="stable" tone="ink" />
              <PreviewKpi label="Awaiting approval" value="3" delta="2 material" tone="pending" />
            </div>
            <div className="border-t border-subtle p-4">
              <div className="mb-3 flex items-center justify-between text-[11px] uppercase tracking-wider text-ink-tertiary">
                <span>Agent activity ticker</span>
                <span className="font-mono">last 60s</span>
              </div>
              <ul className="space-y-1.5 font-mono text-[12px]">
                <PreviewActivity time="14:22:08" agent="recon-1.2" verdict="APPROVE" text="Matched 47 stripe payouts to invoice batch" />
                <PreviewActivity time="14:22:09" agent="compliance" verdict="NEEDS_REVIEW" text="Flagged $4,180 vendor payment for 1099-NEC threshold (cumulative $612)" />
                <PreviewActivity time="14:22:12" agent="bookkeeper" verdict="APPROVE" text="Categorized Anthropic.com to SaaS Subscriptions" />
                <PreviewActivity time="14:22:14" agent="tax-rules" verdict="THINKING" text="Computing Q1 NJ sales tax nexus..." pulse />
              </ul>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function PreviewKpi({
  label,
  value,
  delta,
  tone
}: {
  label: string;
  value: string;
  delta: string;
  tone: "credit" | "pending" | "ink";
}) {
  const deltaColor =
    tone === "credit" ? "text-credit" : tone === "pending" ? "text-pending" : "text-ink-tertiary";
  return (
    <div className="bg-surface p-5">
      <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">{label}</p>
      <p className="mt-2 font-mono text-2xl tracking-tight text-ink-primary">{value}</p>
      <p className={`mt-1 text-xs ${deltaColor}`}>{delta}</p>
    </div>
  );
}

function PreviewActivity({
  time,
  agent,
  verdict,
  text,
  pulse
}: {
  time: string;
  agent: string;
  verdict: string;
  text: string;
  pulse?: boolean;
}) {
  const verdictMap: Record<string, string> = {
    APPROVE: "text-credit",
    NEEDS_REVIEW: "text-pending",
    THINKING: "text-agent-500",
    REJECT: "text-debit",
    BLOCKED: "text-blocked"
  };
  return (
    <li className="flex items-start gap-2.5">
      {pulse ? (
        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-agent-500 animate-pulse-soft shadow-agent-glow" />
      ) : (
        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-ink-disabled" />
      )}
      <span className="text-ink-tertiary">{time}</span>
      <span className={`shrink-0 font-medium ${verdictMap[verdict]}`}>{agent}</span>
      <span className="text-ink-secondary">{"->"}</span>
      <span className="truncate text-ink-secondary">{text}</span>
      <span className={`ml-auto shrink-0 text-[10px] uppercase tracking-wider ${verdictMap[verdict]}`}>{verdict}</span>
    </li>
  );
}

function Positioning() {
  return (
    <section className="border-y border-subtle bg-surface/40">
      <div className="mx-auto max-w-4xl px-6 py-14">
        <p className="text-pretty text-center text-xl leading-relaxed text-ink-secondary md:text-2xl">
          We sit above the model providers and below the human reviewer. Not a bookkeeping service. Not your CPA&apos;s tool. <span className="text-ink-primary">The substrate your agents actually run on when the numbers have to be right.</span>
        </p>
      </div>
    </section>
  );
}

function Features() {
  return (
    <section className="mx-auto max-w-7xl px-6 py-24">
      <h2 className="text-3xl font-semibold tracking-tight md:text-4xl">Built for operators, not buyers.</h2>
      <p className="mt-3 max-w-2xl text-lg text-ink-secondary">
        Six core decisions that distinguish Accounting-Ops from every closed-SaaS AI bookkeeping product.
      </p>
      <div className="mt-12 grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
        <Feature
          icon={<Terminal size={18} />}
          title="Agent-first by construction"
          body="Brigade agents do the work. The app is observation, approval, and chat. Nothing posts to your ledger until a human signs off. No magic. No surprises."
        />
        <Feature
          icon={<ServerCog size={18} />}
          title="Sovereignty as the default"
          body="Self-host on your own hardware. Run fully air-gapped if you need to. Your journal, your encryption keys, your egress policy. We don't see your books and never will."
        />
        <Feature
          icon={<Layers size={18} />}
          title="Multi-entity from day one"
          body="Intercompany eliminations, consolidated reporting, and per-entity policy. Built for operators running five revenue streams across three legal entities, not bolted on at Series B."
        />
        <Feature
          icon={<Scale size={18} />}
          title="Vertical compliance, not generic"
          body="Per-tenant policy engine for FERS 80%, 1099-NEC, multi-state sales-tax nexus, HIPAA segregation, IOLTA trust, DCAA/CAS, and QSBS §1202 tracking. Rules ship as files you can read."
        />
        <Feature
          icon={<Lock size={18} />}
          title="Audit-defensible journal"
          body="Append-only, cryptographically chained, immutable. Every entry traces back to the source document, the agent that drafted it, the policy that validated it, and the human who approved it."
        />
        <Feature
          icon={<Plug size={18} />}
          title="MCP and A2A native"
          body="Other apps' agents call into Accounting-Ops over MCP. Our agents call out over A2A. Same skill files behind Claude, Gemini, or on-prem Qwen. Swap runtimes without rewriting your books."
        />
      </div>
    </section>
  );
}

function Feature({ icon, title, body }: { icon: React.ReactNode; title: string; body: string }) {
  return (
    <div className="glow-border rounded-xl border border-default bg-surface p-6 transition hover:border-strong">
      <div className="flex h-9 w-9 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/30">
        {icon}
      </div>
      <h3 className="mt-4 text-base font-semibold">{title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-ink-secondary">{body}</p>
    </div>
  );
}

function HowItWorks() {
  return (
    <section className="border-t border-subtle bg-gradient-to-b from-transparent to-surface/50">
      <div className="mx-auto max-w-7xl px-6 py-24">
        <h2 className="text-3xl font-semibold tracking-tight md:text-4xl">How it works.</h2>
        <p className="mt-3 max-w-2xl text-lg text-ink-secondary">
          Three steps. The agent does the work. You approve the parts that matter.
        </p>
        <div className="mt-12 grid grid-cols-1 gap-6 md:grid-cols-3">
          <Step n={1} title="Connect your sources" body="Banks, payment processors, payroll, invoicing, contracts, receipts. Ingest stays on your machine. We never proxy through our cloud." icon={<Network size={18} />} />
          <Step n={2} title="Agents draft, you approve" body="Brigade agents classify transactions, draft journal entries, reconcile accounts, and flag anomalies against your vertical's rules. Every draft cites its source and its policy." icon={<Cpu size={18} />} />
          <Step n={3} title="The ledger commits" body="Once you approve, entries land in an append-only journal with a cryptographic chain back to the document, the agent, and the runtime. Auditors love it. So do you." icon={<FileCheck size={18} />} />
        </div>
      </div>
    </section>
  );
}

function Step({ n, title, body, icon }: { n: number; title: string; body: string; icon: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-default bg-surface p-7">
      <div className="flex items-center justify-between">
        <span className="font-mono text-xs text-ink-tertiary">{String(n).padStart(2, "0")}</span>
        <div className="text-agent-500">{icon}</div>
      </div>
      <h3 className="mt-5 text-lg font-semibold">{title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-ink-secondary">{body}</p>
    </div>
  );
}

function Audience() {
  const items = [
    { h: "Polymath operators", p: "Five revenue streams, three entities, one brain. You stopped fitting in QuickBooks two years ago." },
    { h: "Federal contractors", p: "SDVOSB-aspirant or already there. DCAA-compliant timekeeping, CAS-flavored cost pools, indirect-rate math that survives a desk audit." },
    { h: "Healthcare practices", p: "PHI-segregated financial flows. HIPAA-aware data handling. Self-hosted so your accountant doesn't become a business associate." },
    { h: "Law firms", p: "IOLTA trust accounting that won't get you sanctioned. Per-matter ledgers. Three-way reconciliation as a first-class primitive." },
    { h: "VC-backed startups", p: "Preferred-stock waterfalls, §1202 QSBS holding-period tracking, 409A-aware equity events. The instrumentation your future Series B due-diligence wants." },
    { h: "Sovereignty-conscious enterprises", p: "You can't send your general ledger to a frontier AI provider. You shouldn't have to. Run Accounting-Ops behind your own firewall, on your own GPUs, against your own models." }
  ];
  return (
    <section className="mx-auto max-w-7xl px-6 py-24">
      <h2 className="text-3xl font-semibold tracking-tight md:text-4xl">Who it&apos;s for.</h2>
      <div className="mt-12 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => (
          <div key={item.h} className="rounded-xl border border-default bg-surface p-6 transition hover:border-strong hover:bg-surface/80">
            <h3 className="text-base font-semibold">{item.h}</h3>
            <p className="mt-2 text-sm leading-relaxed text-ink-secondary">{item.p}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function Comparison() {
  const lines: Array<[string, string]> = [
    ["Not Pilot or Bench", "Those are bookkeeping services with a portal. We're software you run."],
    ["Not QuickBooks or Xero", "Those are your CPA's tool. We're your AI's substrate."],
    ["Not Digits, Puzzle, or Truewind", "Those are closed-SaaS with one model behind a logo. We're multi-runtime, self-hostable, and the skill files are yours to read."],
    ["Not Codat or Plaid", "Those are pipes. We're the layer where the pipes turn into a defensible ledger."]
  ];
  return (
    <section className="border-t border-subtle bg-surface/30">
      <div className="mx-auto max-w-7xl px-6 py-24">
        <h2 className="text-3xl font-semibold tracking-tight md:text-4xl">Why not the obvious alternatives.</h2>
        <ul className="mt-10 grid grid-cols-1 gap-x-12 gap-y-6 md:grid-cols-2">
          {lines.map(([h, p]) => (
            <li key={h} className="flex gap-4">
              <span className="mt-1 inline-block h-2 w-2 shrink-0 rounded-full bg-brand-500/60" />
              <div>
                <p className="text-sm font-semibold">{h}.</p>
                <p className="mt-1 text-sm text-ink-secondary">{p}</p>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function Open() {
  return (
    <section className="mx-auto max-w-4xl px-6 py-24 text-center">
      <CircleDashed className="mx-auto h-7 w-7 text-agent-500" />
      <h2 className="mt-5 text-3xl font-semibold tracking-tight md:text-4xl">Open by construction.</h2>
      <p className="mt-5 text-base leading-relaxed text-ink-secondary md:text-lg">
        Built on a fork of Anthropic&apos;s Apache 2.0 financial-services agent scaffolding, extended with the policy engine, multi-entity model, and immutable journal. Skill files are plain text. The audit log is a database you can grep. If a fact about your business can&apos;t be explained, it can&apos;t be posted.
      </p>
      <p className="mt-6 text-sm text-ink-tertiary">
        Apache 2.0 where we can. Source-available where we have to. No black boxes in your books.
      </p>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-subtle bg-canvas">
      <div className="mx-auto max-w-7xl px-6 py-12">
        <div className="flex flex-wrap items-center justify-between gap-6">
          <div className="flex items-center gap-3 text-sm">
            <span className="h-1.5 w-1.5 rounded-full bg-credit shadow-[0_0_6px_hsl(157_64%_52%_/_0.8)]" />
            <span className="text-ink-secondary">{APP_VERSION_LABEL} // commits land daily</span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] uppercase tracking-wider text-ink-tertiary">
            <span>SDVOSB-aspirant</span>
            <span className="text-ink-disabled">·</span>
            <span>C-Corp</span>
            <span className="text-ink-disabled">·</span>
            <span>HIPAA-aware</span>
            <span className="text-ink-disabled">·</span>
            <span>Air-gap capable</span>
          </div>
        </div>
        <p className="mt-8 text-sm text-ink-secondary">
          Built by <span className="text-ink-primary">Magic Unicorn Unconventional Technology &amp; Stuff Inc.</span> in Charleston, SC.
        </p>
        <p className="mt-2 text-base italic text-ink-tertiary">
          The books are yours. We just gave them better hands.
        </p>
      </div>
    </footer>
  );
}
