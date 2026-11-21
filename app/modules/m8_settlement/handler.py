import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import Response

from app.browse import Browse, snapshot_for
from app.errors import Problem
from app.manifest import utc_now
from app.modules.m8_settlement.bundle import settlement_voucher, statements_for
from app.modules.m8_settlement.service import closing_period, closed_periods, report
from app.store import book_of

router = APIRouter(prefix='/api/settlement')
PERIOD = re.compile(r'\d{4}-(0[1-9]|1[0-2])')


def valid_period(value):
    if not PERIOD.fullmatch(value):
        raise Problem(422, 'PERIOD_INVALID', {'period': value})
    return value


@router.get('/{run_id}')
def settlement(request: Request, run_id: str, browse: Browse = Depends(),
               period: Annotated[str, Query(max_length=7)] = ''):
    snapshot = snapshot_for(request, run_id)
    result = report(snapshot, statements_for(request.app.state.store, snapshot))
    if period:
        period = valid_period(period)
        result = {**result, 'rows': [row for row in result['rows'] if row['period'] == period],
                  'venues': [row for row in result['venues'] if row['period'] == period],
                  'adjustments': [row for row in result['adjustments']
                                  if row['target_period'] == period or row['source_period'] == period],
                  'periods': [period]}
    rows = browse.choose({'accepted': result['rows'], 'venues': result['venues'],
                          'adjustments': result['adjustments'], 'tiers': result['tiers']})
    return browse.page(rows, totals=result['totals'], periods=result['periods'],
                       closed_periods=result['closed_periods'], tiers=result['tiers'],
                       decision=result['decision'])


@router.post('/{run_id}/close')
def close_period(request: Request, run_id: str, period: Annotated[str, Form()]):
    period = valid_period(period)
    store = request.app.state.store
    snapshot = store.get(run_id)
    statements = statements_for(store, snapshot)
    if period not in closed_periods(statements):
        expected = closing_period(report(snapshot, statements)['rows'], statements)
        if expected != period:
            raise Problem(409, 'PERIOD_EMPTY', {'period': period, 'expected': expected})
    book = book_of(snapshot)
    statements, reused = store.close_period(book, period, snapshot, run_id, utc_now())
    if not statements:
        raise Problem(422, 'PERIOD_EMPTY', {'period': period})
    return {'run_id': run_id, 'period': period, 'reused': reused,
            'statement_ids': [item['statement_id'] for item in statements]}


@router.get('/{run_id}/export')
def export(request: Request, run_id: str, period: Annotated[str, Query(max_length=7)] = ''):
    snapshot = snapshot_for(request, run_id)
    statements = statements_for(request.app.state.store, snapshot)
    if not period:
        result = report(snapshot, statements)
        closed = result['totals']['closed_periods']
        period = result['totals']['current_period'] or (closed[-1] if closed else '')
        if not period:
            raise Problem(422, 'PERIOD_EMPTY', {'period': ''})
    period = valid_period(period)
    data = settlement_voucher(snapshot, statements, period)
    return Response(data, media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="settlement-{run_id}-{period}.zip"'})
