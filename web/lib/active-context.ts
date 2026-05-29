// Server-side helpers to resolve the active workspace + entity from cookies.
//
// Default-entity policy (when no cookie is set):
//   1. If only one entity, use it.
//   2. Otherwise pick the first entity whose `kind` is NOT "personal".
//      (Rationale: personal entities are usually empty / new. The "real
//      business" entity is where the user wants to land on first load.)
//   3. If every entity is personal, fall back to the first one alphabetically.
//
// Users override this by selecting an entity in the EntitySelector; the
// choice is persisted via the `aops_entity` cookie (30-day Max-Age).

import { cookies } from "next/headers";
import { getMyWorkspaces, getWorkspaceEntities, type Workspace, type Entity } from "@/lib/api";

export type ActiveContext = {
  workspaces: Workspace[];
  activeWorkspace: Workspace | null;
  entities: Entity[];
  activeEntity: Entity | null;
  isConsolidated: boolean;
};

function pickDefaultEntity(entities: Entity[]): Entity | null {
  if (entities.length === 0) return null;
  if (entities.length === 1) return entities[0];

  // Prefer the first non-personal entity (alphabetical order from API).
  const nonPersonal = entities.find((e) => e.kind !== "personal");
  if (nonPersonal) return nonPersonal;

  // All entities are personal — fall back to first one.
  return entities[0];
}

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

  let activeEntity: Entity | null = null;
  if (!isConsolidated) {
    if (cookieEntityId) {
      // Honor cookie if entity still exists in this workspace.
      activeEntity =
        entities.find((e) => e.id === cookieEntityId) ?? pickDefaultEntity(entities);
    } else {
      // No cookie — default by policy.
      activeEntity = pickDefaultEntity(entities);
    }
  }

  return { workspaces, activeWorkspace, entities, activeEntity, isConsolidated };
}
