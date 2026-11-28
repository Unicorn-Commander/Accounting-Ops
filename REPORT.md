# AO-RACE-TEST-B2 — force the advisory-lock overlap in the demote/remove race

## Scope
Test-only fix for `test_demote_and_remove_owner_race_does_not_deadlock` in
`tests/integration/test_provisioning_contract_pg.py`. The Round 6 (Finding 2)
requirement is to force the demote to HOLD the `pg_advisory_xact_lock` (seed
218) while the removal blocks on the same lock, then assert the deterministic
outcome.

## What was wrong with the previous test
The old test only signalled that the demote had *started* its advisory-lock
statement (`demote_started` set when the SQL text matched). It did not hold
the lock: the demote's transaction committed before the removal's own lock
wait could overlap, so the "race" was not forced. The removal sometimes ran to
completion before the demote's commit, producing a non-deterministic winner.

## What changed
- `PausedDemoteSession(AsyncSession)` mirrors the working
  `PausedAcceptSession` (TEST-A): it pauses right after the demote's advisory-
  lock statement returns (signalled via `held`, gated by `release`), holding
  the lock — the transaction stays open — until the test sets `release`.
  Because `pg_advisory_xact_lock` is transaction-scoped, the lock is held
  across the pause.
- The test waits for `held`, starts the removal, sleeps 0.5s and asserts the
  removal is *not* done (it is blocked on the shared lock). It then sets
  `release`, gathers both tasks, and asserts:
  - no deadlock, no 500, no unexpected 4xx;
  - exactly one active owner remains (the keeper);
  - T's active membership is gone;
  - T's scoped PAT is revoked (the winning removal's side effect).
- Removed the now-unused `sqlalchemy.exc.OperationalError` import.

## Why the demote wins
With the AO-RACE-2b lock order (both paths take the advisory lock before any
FOR UPDATE row lock) the removal cannot reach its row locks while the demote
still holds them. The demote commits the demotion first; the removal then runs
its own last-owner count against the demoted state — T no longer counts as an
owner, so the last-owner 400 does not fire and the removal succeeds removing
the demoted T.

## Test results
Full file (local Postgres 16, `127.0.0.1:5432`):

```
80 passed in 12.75s
```

`ruff check tests/integration/test_provisioning_contract_pg.py`:

```
All checks passed!
```

## Negative proof
Temporarily removed the `acquire_last_owner_lock` call from
`app/security/member_removal.py` (the AO-RACE-2b lock order). The test failed
deterministically:

```
AssertionError: the removal finished before blocking on the shared advisory
lock; the race window was not forced
assert not True
assert True == <Task finished ... result={'ok': True, 'removed_user_id':
'57262b57-ac1...-6b909cd3965e', 'revoked_pat_count': 1,
'static_service_token_count': 0}>.done
```

Without the lock, the removal runs straight through (its last-owner count
passes because the demote's demotion is not yet committed — it is still paused
on the lock it would have been waiting for). The file was restored
byte-for-byte:

```
$ md5sum -c mr.md5
app/security/member_removal.py: OK
```

After restoration, the full file passes again:

```
80 passed in 12.75s
```

## Files changed
- `tests/integration/test_provisioning_contract_pg.py` (test-only)

## Not changed
- `app/` (no application code)
- `ci.yml`
- TEST-A, TEST-C, and all other tests
