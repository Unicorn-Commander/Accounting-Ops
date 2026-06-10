import { AppShell } from "@/components/app-shell";
import {
  Card,
  EmptyState,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
import { Users } from "lucide-react";
import { getActiveContext } from "@/lib/active-context";
import { getContacts, getContactSources } from "@/lib/contacts";
import { ImportContactsButton } from "@/components/import-contacts-button";

export const dynamic = "force-dynamic";

const CAN_EDIT_ROLES = [
  "accountant",
  "cpa",
  "admin",
  "owner",
  "client_owner",
  "platform_admin"
];

export default async function ContactsPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const [data, sources] = workspace
    ? await Promise.all([
        getContacts(workspace.id, { limit: 500 }),
        getContactSources(workspace.id)
      ])
    : [null, { file: true, contact_ops: false }];
  const canEdit = workspace ? CAN_EDIT_ROLES.includes(workspace.role) : false;
  const contactOps = sources.contact_ops;
  const contacts = data?.items ?? [];

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Contacts"
          subtitle="Customers you bill and vendors you pay. Import from your phone, Google, Outlook, or a spreadsheet."
          action={
            workspace && canEdit ? (
              <ImportContactsButton
                workspaceId={workspace.id}
                contactOpsAvailable={contactOps}
              />
            ) : undefined
          }
        />

        {!workspace ? (
          <EmptyState
            variant="no-data"
            icon={<Users size={28} className="text-ink-disabled" aria-hidden="true" />}
            title="No workspace selected."
            description="Pick or create a workspace first, then import your contacts here."
          />
        ) : contacts.length === 0 ? (
          <EmptyState
            variant="no-data"
            icon={<Users size={28} className="text-ink-disabled" aria-hidden="true" />}
            title="No contacts yet."
            description={
              canEdit
                ? "Import a .vcf (macOS / iPhone / Google / Outlook) or a .csv to populate your customers and vendors. You review and tag each before anything is saved."
                : "No contacts have been added to this workspace yet."
            }
            primaryAction={
              canEdit ? (
                <ImportContactsButton
                  workspaceId={workspace.id}
                  contactOpsAvailable={contactOps}
                />
              ) : undefined
            }
          />
        ) : (
          <Card>
            <div className="mb-3 flex items-center justify-between">
              <p className="text-xs text-ink-tertiary">
                {data?.total ?? contacts.length} contact
                {(data?.total ?? contacts.length) === 1 ? "" : "s"}
              </p>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead className="text-left text-[11px] uppercase tracking-wider text-ink-tertiary">
                  <tr className="border-b border-subtle">
                    <th className="py-2 pr-3">Name</th>
                    <th className="py-2 pr-3">Kind</th>
                    <th className="py-2 pr-3">Email</th>
                    <th className="py-2 pr-3">Phone</th>
                    <th className="py-2">Source</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-subtle">
                  {contacts.map((c) => (
                    <tr key={c.id}>
                      <td className="py-2 pr-3">
                        <div className="font-medium text-ink-primary">
                          {c.display_name}
                        </div>
                        {c.company && c.company !== c.display_name ? (
                          <div className="text-xs text-ink-tertiary">
                            {c.company}
                          </div>
                        ) : null}
                      </td>
                      <td className="py-2 pr-3">
                        <StatusChip
                          tone={c.kind === "vendor" ? "pending" : "credit"}
                        >
                          {c.kind}
                        </StatusChip>
                      </td>
                      <td className="py-2 pr-3 text-ink-secondary">
                        {c.email ?? <span className="text-ink-disabled">—</span>}
                      </td>
                      <td className="py-2 pr-3 text-ink-secondary">
                        {c.phone ?? <span className="text-ink-disabled">—</span>}
                      </td>
                      <td className="py-2 text-ink-tertiary">
                        {c.source ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </PageShell>
    </AppShell>
  );
}
