# Accounting-Ops roles

These are the nine workspace roles enforced by `app/tenancy.py`. Higher ranks
inherit the lower-rank permissions, except that proposal/draft routes explicitly
use the `bookkeeper` floor while approval and posting routes retain their higher
floor.

| Role | Rank | Enforced capability summary |
| --- | ---: | --- |
| viewer | 0 | Read-only workspace and entity access. |
| client_user | 1 | Viewer access plus client-facing decisions and document upload. |
| bookkeeper | 2 | Proposes categorizations, drafts journal/tax/work-product items, links receipts, and requests CPA review; proposals queue and never auto-post. |
| accountant | 3 | Bookkeeper capabilities plus approval/decision and journal-post authority. |
| cpa | 3 | Same enforced rank and capabilities as accountant. |
| admin | 4 | Accountant capabilities plus administrative controls, including period close and member/rule management where routed. |
| owner | 5 | Full workspace owner authority. |
| client_owner | 5 | Owner-equivalent client workspace authority. |
| platform_admin | 100 | Platform-wide operator authority. |

cpa is identical to accountant today

Bookkeepers cannot approve or decide approvals, bulk-decide, post journal
entries, close periods, manage members, connect or manage bank credentials, or
change rules that auto-post.

## Client workspaces a practice sets up

**Team → Clients → Add client** (`POST /api/me/clients`) provisions a workspace
for a client of the caller's practice and seats the caller as its **`owner`**,
so the practice can run the books end to end: connect the client's bank
(admin floor), invite its own bookkeepers and the client (owner floor for
elevated seats and manual links), close periods, export. If the practitioner
enters the business owner's email, that person is invited as
**`client_owner`** — the same rank, so the client holds full authority over
their own data while the practice operates it. The registry records who set
the workspace up (`workspace.onboarded_by_user_id`), and a user who has
onboarded at least one active client reads as the `cpa` persona for the
landing router even though their seats are `owner`.

A practitioner invited **into an existing customer's workspace** still gets
whatever seat the owner grants (typically `cpa`); nothing here changes that.
