"""Synthetic read-only resolver tests; no live credentials or ledger writes."""
import pytest
from agents._shared.coa_templates import get_accounts
from app.journal.posting import _category_resolution, resolve_accounts_for_txn


class Result:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def all(self): return self.rows
    def first(self): return self.rows[0] if self.rows else None


class Session:
    def __init__(self, audits=(), rows=(), cash=True):
        self.audits, self.rows, self.cash = audits, rows, cash
        self.calls = []
    async def execute(self, sql, params):
        sql = str(sql)
        assert sql.lstrip().startswith('SELECT')
        assert params['eid'] == 'entity-A'
        self.calls.append((sql, params))
        if 'decision_audit' in sql:
            return Result([{'template': k} for k in self.audits])
        if 'linked_external_account_id' in sql:
            return Result([{'canonical_form': '1010·Cash', 'account_type': 'asset'}] if self.cash else [])
        return Result(self.rows)


def account(kind, number):
    a = next(a for a in get_accounts(kind) if a['number'] == number)
    return dict(canonical_form=number+'·'+a['name'], account_name=a['name'],
                account_type=a['type'], normal_balance=a['normal_balance'])


@pytest.mark.parametrize('kind', ['c_corp', 's_corp', 'llc'])
@pytest.mark.parametrize('category,number', [('Payroll · Wages','5660'), ('Rent · Facilities','5720'), ('Contractor · 1099-NEC','5630')])
async def test_recorded_template_beats_legacy_number(kind, category, number):
    row = account(kind, number)
    s = Session([kind], [row])
    assert await _category_resolution(s, entity_id='entity-A', category=category) == (row['canonical_form'], None)
    assert s.calls[-1][1]['num'] == number


@pytest.mark.parametrize('category,number', [('Payroll · Wages','6350'), ('Rent · Facilities','6600'), ('Contractor · 1099-NEC','6300'), ('SaaS · AI','6210')])
async def test_legacy_mapping_preserved(category, number):
    s = Session(rows=[{'canonical_form': number+'·Legacy'}])
    assert await _category_resolution(s, entity_id='entity-A', category=category) == (number+'·Legacy', None)
    assert s.calls[-1][1]['num'] == number


@pytest.mark.parametrize('audits', [['s_corp','llc'], ['sole_prop'], [None], ['invented']])
async def test_ambiguous_unsupported_missing_provenance_fails(audits):
    s = Session(audits, [account('s_corp','5660')])
    value, reason = await _category_resolution(s, entity_id='entity-A', category='Payroll · Wages')
    assert value is None and reason
    assert len(s.calls) == 1


@pytest.mark.parametrize('rows', [[], [account('s_corp','5660')]*2])
async def test_missing_inactive_or_duplicate_target_fails(rows):
    s = Session(['s_corp'], rows)
    value, reason = await _category_resolution(s, entity_id='entity-A', category='Payroll · Wages')
    assert value is None and reason
    assert 'is_active = true' in s.calls[-1][0]
    assert 'entity_id = :eid' in s.calls[-1][0]


@pytest.mark.parametrize('field', ['account_name','account_type','normal_balance','canonical_form'])
async def test_changed_template_semantics_fail(field):
    row = account('s_corp','5660')
    row[field] = 'different'
    value, reason = await _category_resolution(Session(['s_corp'],[row]), entity_id='entity-A', category='Payroll · Wages')
    assert value is None and reason


@pytest.mark.parametrize('amount', [-100,100])
async def test_direction_and_cash_link_remain(amount):
    row = account('llc','5720')
    pair = await resolve_accounts_for_txn(Session(['llc'],[row]),entity_id='entity-A',category='Rent · Facilities',amount_cents=amount,external_account_id='bank',source='test')
    assert pair.debit_account == (row['canonical_form'] if amount < 0 else '1010·Cash')
    assert pair.credit_account == ('1010·Cash' if amount < 0 else row['canonical_form'])


async def test_unlinked_cash_blocks_before_template_lookup():
    s = Session(['s_corp'],[account('s_corp','5660')],cash=False)
    assert await resolve_accounts_for_txn(s,entity_id='entity-A',category='Payroll · Wages',amount_cents=-100,external_account_id='bank',source='test') is None
    assert len(s.calls) == 1


async def test_unknown_category_cannot_resolve_from_notes_or_names():
    s = Session(['s_corp'],[account('s_corp','5660')])
    value, reason = await _category_resolution(s,entity_id='entity-A',category='Unknown')
    assert value is None and reason and not s.calls


async def test_correction_uses_same_template_resolution():
    from app.journal.posting import _resolve_category_account
    row = account('s_corp', '5630')
    assert await _resolve_category_account(Session(['s_corp'], [row]), entity_id='entity-A', category='Contractor · 1099-NEC') == row['canonical_form']
