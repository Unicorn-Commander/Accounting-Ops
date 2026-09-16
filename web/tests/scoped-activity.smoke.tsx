import React from "react";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentActivitySection, scopedActivityRows } from "../components/agent-activity-section";
import type { AIActivityResponse, AIActivityRow } from "../lib/ai-activity";

const row = {
  id: "audit-1", workspace_id: "demo-workspace", entity_id: "demo-entity",
  entity_display_name: "Rivergreen Coffee Co.", agent_id: "demo-agent",
  action_label: "Posted journal entry", tool_name: "post_journal_entry",
  outcome: "queued_for_confirm", reasoning_trace: "Synthetic <request>",
  created_at: "2026-09-16T01:00:00.000Z",
} as AIActivityRow;
const activity: AIActivityResponse = { rows: [row], as_of: row.created_at, total_returned: 1 };
assert.equal(scopedActivityRows(activity, "demo-workspace", "demo-entity")?.length, 1);
assert.equal(scopedActivityRows(activity, "foreign-workspace"), null);
assert.equal(scopedActivityRows(activity, "demo-workspace", "foreign-entity"), null);
const html = renderToStaticMarkup(<AgentActivitySection activity={activity} workspaceId="demo-workspace" />);
assert.ok(html.includes("Journal entry request"));
assert.ok(!html.includes("Posted journal entry"));
assert.ok(html.includes("2026-09-16 01:00:00 UTC"));
assert.ok(html.includes("Synthetic &lt;request&gt;"));
for (const fake of ["Acme", "Soylent", "47 auto-approved", "thinking"])
  assert.ok(!html.includes(fake));
const unavailable = renderToStaticMarkup(<AgentActivitySection activity={activity} workspaceId="other" />);
assert.ok(unavailable.includes("Activity is unavailable"));
assert.ok(!unavailable.includes("Rivergreen Coffee Co."));
const empty = renderToStaticMarkup(<AgentActivitySection activity={{...activity, rows: []}} workspaceId="demo-workspace" />);
assert.ok(empty.includes("No recorded agent activity"));
assert.ok(!empty.includes("Activity is unavailable"));
console.log("Scoped activity rendering: passed");
