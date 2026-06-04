"""Read-only analytics aggregators over the per-tenant ledger surfaces.

Nothing in this package mutates state. Each module exposes a pure function that
takes already-fetched rows (so it is unit-testable without a database) plus a
thin caller in ``app/api`` that fetches those rows inside ``workspace_session``
(RLS + search_path pinned) and hands them to the aggregator.
"""
