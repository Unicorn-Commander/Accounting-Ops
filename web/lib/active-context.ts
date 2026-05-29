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

// Kind-priority ordering for default-entity selection.  Lower index = higher
// priority.  Rationale: when a user has multiple entities, the operating
// business (C-Corp / S-Corp / Sole-Prop) is almost always where their data
// and attention sit.  Partnerships and personal entities are usually side-cars.
const _KIND_PRIORITY: Record<string, number> = {
  c_corp:       0,
  s_corp:       1,
  sole_prop:    2,
  llc:          3,
  partnership:  4,
  trust:        5,
  estate:       6,
  personal:     7,
};

function _kindRank(kind: string): number {
  return kind in _KIND_PRIORITY ? _KIND_PRIORITY[kind] : 99;
}

function pickDefaultEntity(entities: Entity[]): Entity | null {
  if (entities.length === 0) return null;
  if (entities.length === 1) return entities[0];

  // Sort by (kind-priority, display_name) and take the top.  This means a
  // user with [Aaron Personal, Brightwater Labs (partnership), Magic
  // Unicorn Inc (c_corp), Majiks JV (partnership)] lands on Magic Unicorn
  // Inc — the C-Corp — not on the alphabetically-first partnership.
  const ranked = [...entities].sort((a, b) => {
    const ra = _kindRank(a.kind);
    const rb = _kindRank(b.kind);
    if (ra !== rb) return ra - rb;
    return a.display_name.localeCompare(b.display_name);
  });
  return ranked[0] ?? null;
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
