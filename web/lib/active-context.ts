// Server-side helpers to resolve the active workspace + entity from cookies.

import { cookies } from "next/headers";
import { getMyWorkspaces, getWorkspaceEntities, type Workspace, type Entity } from "@/lib/api";

export type ActiveContext = {
  workspaces: Workspace[];
  activeWorkspace: Workspace | null;
  entities: Entity[];
  activeEntity: Entity | null;
  isConsolidated: boolean;
};

export async function getActiveContext(): Promise<ActiveContext> {
  const workspaces = await getMyWorkspaces();
  if (workspaces.length === 0) {
    return {
      workspaces: [],
      activeWorkspace: null,
      entities: [],
      activeEntity: null,
      isConsolidated: false
    };
  }

  const cookieStore = await cookies();
  const cookieWorkspaceId = cookieStore.get("aops_workspace")?.value;
  const cookieEntityId = cookieStore.get("aops_entity")?.value;

  const activeWorkspace =
    workspaces.find((w) => w.id === cookieWorkspaceId) ?? workspaces[0];

  const entities = await getWorkspaceEntities(activeWorkspace.id);

  const isConsolidated = cookieEntityId === "consolidated";
  const activeEntity = isConsolidated
    ? null
    : entities.find((e) => e.id === cookieEntityId) ?? entities[0] ?? null;

  return { workspaces, activeWorkspace, entities, activeEntity, isConsolidated };
}
