"""Setup Assistant persona package.

The Setup Assistant is a conversational chat agent that walks a new
workspace owner through provisioning their first entity + COA + initial
connector recommendations.

Lives under cpa_team/ rather than tax_specialists/ because it is NOT a tax
filer — it is a hospitality / on-boarding agent. The Tax Resolver (also in
cpa_team/) is the closest cousin: both are coordinator-style agents rather
than form-line specialists.

This package intentionally has no Python runner — the assistant is only
ever invoked via the in-app /chat surface (web/app/setup/page.tsx pre-binds
the persona slug ``setup-assistant``).
"""
