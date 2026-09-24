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
