# Template-aware posting for payroll, rent and contractors

`create_entity_with_coa` records the chosen template in append-only `decision_audit`. Posting and recategorization now share resolution for `Payroll · Wages`, `Rent · Facilities`, and `Contractor · 1099-NEC`:

- No successful provisioning audit: retain legacy `CATEGORY_TO_ACCOUNT` numbers.
- A single recorded supported template (`c_corp`, `s_corp`, `llc`): use explicit semantic mapping5660/5720/5630, even if an older default account also exists.
- Conflicting, null or unsupported recorded template: refuse. Do not infer from entity kind, tax notes, account names or neighboring numbers.
- The template target must be exactly one active entity-scoped account, with exact canonical form/name/type/normal balance from the source template. A changed target refuses pending chart review.
- Other categories retain existing resolution. Materiality, period locks, authorization, idempotency, hash chaining and append-only correction behavior are unchanged.

This is intentionally limited to the three reviewed categories. It is not a general template inference engine. An intentionally customized chart or unsupported template needs an explicit reviewed mapping design; silently treating it as a supported template is unsafe. Historical journal entries are never rewritten.

Validation:84 focused tests across new resolution, period-close, setup-assistant, posting tool hardening; additional period-close recategorization suite recorded in rollout evidence. Read-only live candidate proof for both Rivergreen entities resolves all six category/entity combinations to5660/5720/5630; no ledger writes. Ruff on posting.py reports the same three preexisting warnings as main; new tests pass lint.
