import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { MembersClient } from "@/components/members-client";
import { getActiveContext } from "@/lib/active-context";
import {
  getWorkspaceEntities,
  getWorkspaceInvitations,
  getWorkspaceMembers,
} from "@/lib/api";

const ADMIN_ROLES = ["owner", "client_owner", "admin", "platform_admin"];

export default async function MembersSettingsPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canAdmin = workspace ? ADMIN_ROLES.includes(workspace.role) : false;
  const canOwner =
    workspace && ["owner", "client_owner", "platform_admin"].includes(workspace.role);

  // We only fetch members + invites when the caller is admin+ — the API
  // would reject otherwise and the client would render an empty state.
  const [members, invitations, entities] = workspace && canAdmin
    ? await Promise.all([
        getWorkspaceMembers(workspace.id),
        getWorkspaceInvitations(workspace.id, false),
        getWorkspaceEntities(workspace.id),
      ])
    : [[], [], []];

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Members"
          subtitle="Invite teammates and external CPAs. Set their role and (optionally) restrict them to specific entities."
        />

        {!workspace ? (
          <Card>
            <p className="text-sm text-ink-tertiary">No active workspace.</p>
          </Card>
        ) : !canAdmin ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              Admin access is required to manage members. Ask the workspace owner to
              invite you with the <code>admin</code> role.
            </p>
          </Card>
        ) : (
          <MembersClient
            workspaceId={workspace.id}
            workspaceName={workspace.display_name}
            initialMembers={members}
            initialInvitations={invitations}
            entities={entities}
            canOwner={Boolean(canOwner)}
          />
        )}
      </PageShell>
    </AppShell>
  );
}
