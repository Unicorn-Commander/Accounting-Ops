import { Send } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function ChatPage() {
  return (
    <section className="mx-auto flex max-w-6xl flex-col px-4 py-6">
      <h1 className="text-2xl font-semibold">Chat</h1>
      <div className="mt-4 min-h-[420px] rounded-md border border-line bg-paper p-4">
        <div className="max-w-xl rounded-md bg-field p-3 text-sm">
          Placeholder agent conversation surface for Brigade handoff in Phase 1.
        </div>
      </div>
      <form className="mt-3 flex gap-2">
        <input className="h-10 min-w-0 flex-1 rounded-md border border-line bg-paper px-3 text-sm" placeholder="Ask Accounting-Ops" />
        <Button aria-label="Send">
          <Send size={16} /> Send
        </Button>
      </form>
    </section>
  );
}
