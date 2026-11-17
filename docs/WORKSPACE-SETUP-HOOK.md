# Ops-Center Workspace-Setup Hook

## How to Switch on the Ops-Center Workspace-Setup Hook

The Ops-Center workspace-setup hook enables Control-Plane provisioning of workspaces and JIT membership. Follow these environment variables to switch it on.

## (1) Environment Variables

### `PROVISIONING_ENABLED`
- Set to one of: `1`, `true`, `yes`, `on`
- **Code**: `app/security/provisioning_token.py:22`
- **Effect**: Router not mounted if unset

### `PROVISIONING_JWT_SECRET`
- Must contain at least 32 bytes
- **Code**: `app/security/provisioning_token.py:34-37`
- **Effect**: Router not mounted if insufficient length; logs warning

### `PROVISIONING_JWT_AUDIENCE`
- Default: `accounting-ops` if not set
- **Code**: `app/security/provisioning_token.py:100`
- **Effect**: Audience claim validation in token verification

### `PROVISIONING_MEMBER_ROLE`
- Names the bookkeeping role ordinary members start with: `viewer`, `client_user`, `bookkeeper`, `accountant`, or `cpa`
- **Code**: `app/api/provisioning.py:242-243`
- **Effect**: Determines role for non-administrators via `member_start_role()`

### `OPS_CENTER_BASE_URL`
- Read through `get_settings().ops_center_base_url` (`app/security/entitlements.py:64-65`)
- If set, Ops-Center provides JIT answers; if empty, no Ops-Center URL means section (4) applies
- **Code**: `app/security/entitlements.py:64-65,70-71`
- **Effect**: Controls whether `ensure_jit_membership` calls Ops-Center; empty means Ops-Center is never reached

## (2) Ops-Center Token Format

### Claims
- **Algorithm**: HS256
- **Issuer**: `ops-center`
- **Audience**: `accounting-ops`
- **Issued At (`iat`)**: Unix timestamp
- **Expires At (`exp`)**: Unix timestamp, no more than 60 seconds after `iat`
- **JWT ID (`jti`)**: String, 1-255 characters; enforced via replay table
- **Not Before (`nbf`)**: Optional, rejects if future

### Code Locations
- **Algorithm & basic verification**: `app/security/provisioning_token.py:43`
- **Claim validation**: `app/security/provisioning_token.py:52-57`
- **JTI length check**: `app/security/provisioning_token.py:54`
- **Replay protection**: `app/security/provisioning_token.py:63-87`
- **Table `public.provisioning_replay`**: `app/security/provisioning_token.py:78`
- **Replayed token error**: `app/security/provisioning_token.py:78`

## (3) Routes

All routes are mounted under `/api/v1/provisioning/workspaces` only when `provisioning_available()` returns true.

### POST `/api/v1/provisioning/workspaces`
- **Purpose**: Create a workspace for an organization
- **Code**: `app/api/provisioning.py:131-141`
- **Idempotency-Key header**: Must be `{org_id}:accounting-ops`

### GET `/api/v1/provisioning/workspaces/{workspace_id}`
- **Purpose**: Read workspace status
- **Code**: `app/api/provisioning.py:144-150`

### PATCH `/api/v1/provisioning/workspaces/{workspace_id}`
- **Purpose**: Update control-plane state (active, suspended, archived)
- **Code**: `app/api/provisioning.py:153-167`

### DELETE `/api/v1/provisioning/workspaces/{workspace_id}/members/{subject}`
- **Purpose**: Remove a member from a workspace
- **Code**: `app/api/provisioning.py:170-180`

## (4) Changes When PROVISIONING_ENABLED is On

### With OPS_CENTER_BASE_URL Empty
- **Effect**: `ensure_jit_membership` calls Ops-Center; `OpsCenterEntitlementsClient.fetch` returns "unavailable" immediately (`app/security/entitlements.py:70-71`) because the base URL is unset, then `_without_an_answer` keeps every active member as is (code: `app/api/provisioning.py:282-307`)
- **Membership**: `_without_an_answer` keeps every active member as is; a person with no existing membership gets "unavailable" (try again)

### With OPS_CENTER_BASE_URL Set
- **Effect**: Ops-Center answers determine memberships
- **Admins**: Ops-Center owner/admin becomes books owner/admin via `role_after`
- **Code**: `app/api/provisioning.py:258-263`
- **Others**: Members with no seat start from `member_start_role`; existing roles preserved unless controlled-plane changes them

## (5) How to Verify the Hook is On

### Check Router Mount Status
- **With valid token**: POST to `/api/v1/provisioning/workspaces` returns 200/409
- **Without token**: POST returns 401 (unauthorized), NOT 404 (not found)
- **Code**: `app/security/provisioning_token.py:90-92` (404 when not available)
- **Effect**: Router mount is gated by `provisioning_available()` in `app/main.py:409`

### Manual Test
```bash
curl -X POST http://localhost:8000/api/v1/provisioning/workspaces \
  -H "Idempotency-Key: org-xyz:accounting-ops" \
  -d '{"org_id": "org-xyz", "display_name": "Test Workspace", "owner": {"subject": ""}}'
```

Expected with hook off: 404
Expected with hook on (no token): 401
Expected with hook on (valid token): 200/409
