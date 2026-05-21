import { MaterialityBadge } from "@/components/materiality-badge";
import { TransparencyPanel } from "@/components/transparency-panel";

const transactions = [
  ["2026-05-20", "Test Vendor", "Software", "$84.00", "low"],
  ["2026-05-19", "Test Counterparty", "Equipment", "$1,245.00", "review"],
  ["2026-05-18", "Foreign Supplier", "Review", "$5,600.00", "block"]
] as const;

export default function TransactionsPage() {
  return (
    <section className="mx-auto max-w-6xl px-4 py-6">
      <h1 className="text-2xl font-semibold">Transactions</h1>
      <div className="mt-4 overflow-hidden rounded-md border border-line bg-paper">
        <table className="w-full border-collapse text-sm">
          <thead className="bg-field text-left">
            <tr>
              {["Date", "Counterparty", "Category", "Amount", "Policy"].map((h) => (
                <th key={h} className="border-b border-line px-3 py-2 font-semibold">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {transactions.map(([date, name, category, amount, level]) => (
              <tr key={`${date}-${name}`}>
                <td className="border-b border-line px-3 py-2">{date}</td>
                <td className="border-b border-line px-3 py-2">{name}</td>
                <td className="border-b border-line px-3 py-2">{category}</td>
                <td className="border-b border-line px-3 py-2">{amount}</td>
                <td className="border-b border-line px-3 py-2"><MaterialityBadge level={level} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <TransparencyPanel />
      </div>
    </section>
  );
}
