import { ApprovalGate } from "@/components/approval-gate";
import { MaterialityBadge } from "@/components/materiality-badge";
import { TransparencyPanel } from "@/components/transparency-panel";

const rows = [
  ["Cash position", "$42,850.00", "Across connected accounts"],
  ["Open approvals", "3", "Materiality and specialty rules"],
  ["Unmatched receipts", "11", "Waiting for source transaction links"]
];

export default function DashboardPage() {
  return (
    <>
      <section className="bg-field py-6">
        <div className="mx-auto max-w-6xl px-4">
          <h1 className="text-2xl font-semibold">Dashboard</h1>
          <p className="mt-1 text-sm text-ink/70">Observation, approval, and agent collaboration surface.</p>
        </div>
      </section>
      <section className="mx-auto grid max-w-6xl gap-3 px-4 py-5 md:grid-cols-3">
        {rows.map(([label, value, detail]) => (
          <div key={label} className="rounded-md border border-line bg-paper p-4">
            <div className="text-sm text-ink/65">{label}</div>
            <div className="mt-2 text-2xl font-semibold">{value}</div>
            <div className="mt-1 text-sm text-ink/65">{detail}</div>
          </div>
        ))}
      </section>
      <ApprovalGate title="Approval queued by materiality rule" amount="$1,245.00 USD" />
      <section className="mx-auto max-w-6xl px-4 py-5">
        <div className="rounded-md border border-line bg-paper">
          <div className="flex items-center justify-between px-4 py-3">
            <h2 className="text-base font-semibold">Latest agent decision</h2>
            <MaterialityBadge level="review" />
          </div>
          <TransparencyPanel />
        </div>
      </section>
    </>
  );
}
