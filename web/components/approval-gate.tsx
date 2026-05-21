import { Check, X } from "lucide-react";
import { Button } from "@/components/ui/button";

export function ApprovalGate({ title, amount }: { title: string; amount: string }) {
  return (
    <section className="border-y border-line bg-paper py-4">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4">
        <div>
          <h2 className="text-base font-semibold">{title}</h2>
          <p className="text-sm text-ink/70">{amount}</p>
        </div>
        <div className="flex gap-2">
          <Button aria-label="Approve">
            <Check size={16} /> Approve
          </Button>
          <Button aria-label="Reject">
            <X size={16} /> Reject
          </Button>
        </div>
      </div>
    </section>
  );
}
