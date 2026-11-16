## Changes Made

Created `docs/WORKSPACE-SETUP-HOOK.md` - A comprehensive guide for operators to switch on the Ops-Center workspace-setup hook.

## Verification

The documentation accurately reflects the code in:

- `app/security/provisioning_token.py:22` (PROVISIONING_ENABLED logic)
- `app/security/provisioning_token.py:34-37` (PROVISIONING_JWT_SECRET validation)
- `app/security/provisioning_token.py:100` (PROVISIONING_JWT_AUDIENCE)
- `app/api/provisioning.py:242-243` (PROVISIONING_MEMBER_ROLE)
- `app/api/provisioning.py:339-348` (OPS_CENTER_BASE_URL effect)
- `app/api/provisioning.py:131-141` (POST /api/v1/provisioning/workspaces)
- `app/api/provisioning.py:144-150` (GET /api/v1/provisioning/workspaces/{workspace_id})
- `app/api/provisioning.py:153-167` (PATCH /api/v1/provisioning/workspaces/{workspace_id})
- `app/api/provisioning.py:170-180` (DELETE /api/v1/provisioning/workspaces/{workspace_id}/members/{subject})
- `app/main.py:409` (Router mount condition)

All claims in the documentation are directly cited from the source code files.

## What Remains

- Operations.md: No list of docs found, so no link added as instructed
- CODE: No code was changed (task explicitly requested only docs)

## What Aaron Must Decide

- Review the WORKSPACE-SETUP-HOOK.md documentation for accuracy
- Add the new docs file to any documentation index if desired
- Deploy or distribute the documentation as needed
