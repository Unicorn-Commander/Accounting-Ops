// Chat message shape — used by the popout widget and the /chat page.
//
// The previous version of this module exported a hard-coded seed conversation
// (CHAT_MESSAGES) so the chat UI had something to render before the live
// orchestrator chat endpoint shipped. That seed conversation was demo-only
// fake data and confused testers — removed 2026-05-30. Chat is now empty
// by default and will render real sessions once the backend endpoint exists.

export interface ChatMessage {
  role: "agent" | "user";
  agent?: string;
  body: string;
}

/** Always empty — kept as a stable export so existing call sites don't break. */
export const CHAT_MESSAGES: ChatMessage[] = [];
