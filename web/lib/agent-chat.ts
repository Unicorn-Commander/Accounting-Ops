// Shared seed conversation for the agent chat.
//
// Both the popout chat widget and the full /chat page render from here so the
// demo conversation stays in one place. Phase 1 replaces this with a live
// session against the orchestrator chat endpoint; the message shape mirrors
// what that endpoint is expected to return.

export interface ChatMessage {
  role: "agent" | "user";
  agent?: string;
  body: string;
}

export const CHAT_MESSAGES: ChatMessage[] = [
  {
    role: "agent",
    agent: "Accounting-Ops",
    body: "Good afternoon. Here's where things stand on Magic Unicorn Inc as of right now:\n\n- Cash position: $487,193 across 6 accounts\n- 23 transactions auto-posted today\n- 3 items waiting for your review (one is the Mercury ACH I flagged earlier)\n- Q2 estimated tax projection: $11,200, due June 16\n\nAnything you want to dig into?"
  },
  {
    role: "user",
    body: "What's the Mercury ACH thing?"
  },
  {
    role: "agent",
    agent: "recon-1.2",
    body: "Inbound $5,000 ACH on May 19, account ending 2847. Looks like an inter-entity transfer from Magic Unicorn Inc to Majiks JV (the description says 'Majiks contributor float'), but I can't find a matching outbound debit in Magic Unicorn Inc's accounts.\n\nTwo possibilities:\n1. It's actually external revenue we haven't catalogued yet (royalty advance? customer payment?)\n2. The outbound side is mis-categorized in MU Inc — search history shows a $5,000 'Misc Outflow' on May 18 that might be it.\n\nWant me to dig into option 2 and propose a recategorization?"
  },
  {
    role: "user",
    body: "Yeah, go ahead. If it's option 2, just queue the recategorization for my approval."
  },
  {
    role: "agent",
    agent: "recon-1.2",
    body: "Working on it. Pulling the May 18 'Misc Outflow' detail and matching counterparty + amount + date. Will queue a proposal in Reconciliation if confident, or flag this back if it's actually external revenue.\n\nETA: about 12 seconds. I'll ping you when ready."
  }
];
