from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID
import pytest
from app.tools.journal_tools import _derived_sign_matched


@pytest.mark.parametrize('category,canonical', [('Payroll · Wages','5660·Salaries & Wages'), ('Rent · Facilities','5720·Rent'), ('Contractor · 1099-NEC','5630·Contractor · 1099-NEC')])
@pytest.mark.parametrize('amount,expected', [(5000,False),(-5000,True)])
async def test_tool_sign_uses_actual_resolved_account(category,canonical,amount,expected):
    session=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda:SimpleNamespace(account_type='expense'))))
    eid=UUID('7cb65859-d907-482d-95b4-9c0266be623f')
    with patch('app.journal.posting._resolve_category_account',new=AsyncMock(return_value=canonical)) as resolve:
        assert await _derived_sign_matched(session,entity_id=eid,category=category,amount_cents=amount) is expected
    resolve.assert_awaited_once_with(session,entity_id=str(eid),category=category)
    sql,params=session.execute.call_args.args
    assert 'canonical_form = :target' in str(sql)
    assert params == {'eid':str(eid),'target':canonical}


async def test_unresolved_template_does_not_invent_sign():
    session=SimpleNamespace(execute=AsyncMock())
    with patch('app.journal.posting._resolve_category_account',new=AsyncMock(return_value=None)):
        assert await _derived_sign_matched(session,entity_id=UUID(int=1),category='Payroll · Wages',amount_cents=5000) is True
    session.execute.assert_not_awaited()
