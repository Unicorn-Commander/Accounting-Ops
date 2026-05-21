import { Archive, FileDown } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function CpaExportPage() {
  return (
    <section className="mx-auto max-w-6xl px-4 py-6">
      <h1 className="text-2xl font-semibold">CPA Export</h1>
      <div className="mt-4 rounded-md border border-line bg-paper p-4">
        <div className="flex items-center gap-3">
          <Archive size={20} />
          <div>
            <h2 className="text-base font-semibold">Working papers bundle</h2>
            <p className="text-sm text-ink/70">QBO IIF, Xero CSV, plain-text journal, and reasoning traces are Phase 3 outputs.</p>
          </div>
        </div>
        <div className="mt-4">
          <Button aria-label="Prepare export">
            <FileDown size={16} /> Prepare export
          </Button>
        </div>
      </div>
    </section>
  );
}
