import { GitPullRequestArrow } from "lucide-react";
import { TransparencyPanel } from "@/components/transparency-panel";

export default function ReconciliationQueuePage() {
  return (
    <section className="mx-auto max-w-6xl px-4 py-6">
      <h1 className="text-2xl font-semibold">Reconciliation Queue</h1>
      <div className="mt-4 rounded-md border border-line bg-paper">
        <div className="flex items-center gap-3 px-4 py-3">
          <GitPullRequestArrow size={18} />
          <div>
            <h2 className="text-base font-semibold">Receipt waiting for transaction match</h2>
            <p className="text-sm text-ink/70">Placeholder row for Phase 1 eBay/Plaid/receipt linking.</p>
          </div>
        </div>
        <TransparencyPanel />
      </div>
    </section>
  );
}
