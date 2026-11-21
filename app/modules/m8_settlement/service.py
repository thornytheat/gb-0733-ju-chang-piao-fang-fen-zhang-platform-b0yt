"""场馆阶梯分成结算。

关账口径（本项目的明确决定）：
- 关账时按当期净票房锁定档位与分成，写出结算记录，之后不再改写。
- 同一期再次关账（含同源重跑）复用原结算记录，不新增、不覆盖。
- 关账后新增退款导致净票房回落档位时，当期结算单保持原样；
  按新净票房重算与原确认分成的差额记为「待调整金额」，计入下一期调整。
- 调整额只作独立行项，不并入下一期净票房，因此不会二次改变下一期档位。
- 下一期若已关账，调整继续顺延到之后第一个未关账的月份，不追溯改写。
"""
from collections import Counter, defaultdict

from app.manifest import checksum
from app.modules.m8_settlement.rules import (DECISION, next_period, period_of, share_cents, tier_card,
                                             tier_for)


def settlement_rows(snapshot):
    """现算的每期每馆净票房。旧账册没有持久化该字段时按场次月份重算。"""
    if snapshot.get('settlement') is not None:
        return snapshot['settlement']
    shows = {row['show']: row for row in snapshot['shows']}
    return provisional(snapshot['funding']['ledger'], shows)


def closing_period(rows, statements):
    """待关账期：有净票房或需要承接调整、且尚未关账的最早一期。"""
    closed = closed_periods(statements)
    targets = {row['adjustment_period'] for row in rows if row['adjustment_cents']}
    with_data = {row['period'] for row in rows if row['has_data']}
    candidates = sorted((with_data | targets) - closed)
    return candidates[0] if candidates else ''


def provisional(ledger, shows):
    totals = defaultdict(int)
    for row in ledger:
        totals[(period_of(shows[row['show']]['opens']), row['venue'])] += row['net_cents']
    rows = []
    for (period, venue), net_cents in sorted(totals.items()):
        tier = tier_for(net_cents)
        rows.append({'period': period, 'venue': venue, 'net_cents': net_cents, **tier,
                     'share_cents': share_cents(net_cents, tier['rate_bp'])})
    return rows


def closed_periods(statements):
    return {row['period'] for row in statements}


def realized_by_source(statements):
    realized = Counter()
    for statement in statements:
        for item in statement.get('adjustments', []):
            realized[item['source_statement_id']] += item['amount_cents']
    return realized


def next_open_period(period, closed):
    """下一期；若下一期也已关账则继续顺延，绝不回写已关账期。"""
    candidate = next_period(period)
    while candidate in closed:
        candidate = next_period(candidate)
    return candidate


def outstanding_adjustments(snapshot, statements):
    """每条已关账结算单尚未结转的差额。差额为正表示补付场馆，为负表示扣回。"""
    live = {(row['period'], row['venue']): row for row in settlement_rows(snapshot)}
    realized = realized_by_source(statements)
    closed = closed_periods(statements)
    items = []
    for statement in statements:
        current = live.get((statement['period'], statement['venue']))
        expected = current['share_cents'] if current else statement['share_cents']
        amount = expected - statement['share_cents'] - realized[statement['statement_id']]
        if not amount:
            continue
        items.append({
            'source_statement_id': statement['statement_id'],
            'source_period': statement['period'],
            'venue': statement['venue'],
            'target_period': next_open_period(statement['period'], closed),
            'amount_cents': amount,
            'confirmed_share_cents': statement['share_cents'],
            'expected_share_cents': expected,
            'confirmed_tier': statement['tier'],
            'current_tier': current['tier'] if current else statement['tier'],
        })
    items.sort(key=lambda item: (item['target_period'], item['venue'], item['source_period']))
    return items


def plan_close(snapshot, statements, period, closed_at, run_id):
    """计算关账某期应写入的结算记录；已关账期返回空列表。"""
    if period in closed_periods(statements):
        return []
    live = {row['venue']: row for row in settlement_rows(snapshot) if row['period'] == period}
    incoming = defaultdict(list)
    for item in outstanding_adjustments(snapshot, statements):
        if item['target_period'] == period:
            incoming[item['venue']].append(item)
    venues = sorted(set(live) | set(incoming))
    planned = []
    for venue in venues:
        current = live.get(venue)
        net_cents = current['net_cents'] if current else 0
        tier = current or {'tier': '', 'tier_label': '无净票房', 'threshold_cents': 0,
                           'rate_bp': 0, 'rate_label': '—'}
        basis = share_cents(net_cents, tier['rate_bp'])
        adjustments = [{key: item[key] for key in ('source_statement_id', 'source_period', 'amount_cents',
                                                   'confirmed_share_cents', 'expected_share_cents')}
                       for item in incoming.get(venue, [])]
        planned.append({
            'statement_id': 'st_' + statement_identifier(snapshot['manifest']['book'], period, venue),
            'book': snapshot['manifest']['book'], 'period': period, 'venue': venue,
            'run_id': run_id, 'publication_id': snapshot['manifest']['publication_id'],
            'closed_at': closed_at, 'net_at_close_cents': net_cents, 'has_data': bool(current),
            'tier': tier['tier'], 'tier_label': tier['tier_label'],
            'threshold_cents': tier['threshold_cents'], 'rate_bp': tier['rate_bp'],
            'rate_label': tier['rate_label'], 'share_cents': basis,
            'adjustment_cents': sum(item['amount_cents'] for item in adjustments),
            'adjustments': adjustments,
        })
    return planned


def statement_identifier(book, period, venue):
    return checksum(f'{book}|{period}|{venue}'.encode())[:24]


def merge(snapshot, statements):
    """把当前批次的现算结果与已关账记录并列，产出页面、接口和凭证共用行。"""
    live = {(row['period'], row['venue']): row for row in settlement_rows(snapshot)}
    fixed = {(row['period'], row['venue']): row for row in statements}
    outstanding = {(item['source_period'], item['venue']): item
                   for item in outstanding_adjustments(snapshot, statements)}
    realized = realized_by_source(statements)
    carried_out = Counter()
    for statement in statements:
        for item in statement.get('adjustments', []):
            carried_out[item['source_statement_id']] += item['amount_cents']
    rows = []
    for period, venue in sorted(set(live) | set(fixed)):
        current, statement = live.get((period, venue)), fixed.get((period, venue))
        if current:
            net_cents, tier = current['net_cents'], current['tier']
            tier_label, rate_bp, rate_label = current['tier_label'], current['rate_bp'], current['rate_label']
            expected = current['share_cents']
        else:
            net_cents, tier = statement['net_at_close_cents'], statement['tier']
            tier_label, rate_bp, rate_label = statement['tier_label'], statement['rate_bp'], statement['rate_label']
            expected = statement['share_cents']
        pending = outstanding.get((period, venue))
        confirmed = statement['share_cents'] if statement else None
        rows.append({
            'period': period, 'venue': venue, 'status': 'closed' if statement else 'open',
            'has_data': bool(current) or bool(statement and statement.get('has_data')), 'net_cents': net_cents, 'tier': tier,
            'tier_label': tier_label, 'rate_bp': rate_bp, 'rate_label': rate_label,
            'expected_share_cents': expected,
            'statement_id': statement['statement_id'] if statement else '',
            'closed_at': statement['closed_at'] if statement else '',
            'closed_run_id': statement['run_id'] if statement else '',
            'net_at_close_cents': statement['net_at_close_cents'] if statement else None,
            'confirmed_tier': statement['tier'] if statement else '',
            'confirmed_tier_label': statement['tier_label'] if statement else '',
            'confirmed_rate_label': statement['rate_label'] if statement else '',
            'confirmed_share_cents': confirmed,
            'realized_adjustment_cents': realized[statement['statement_id']] if statement else 0,
            'settled_adjustment_cents': statement['adjustment_cents'] if statement else 0,
            'settled_total_cents': (statement['share_cents'] + statement['adjustment_cents']) if statement else None,
            'adjustment_cents': pending['amount_cents'] if pending else 0,
            'carried_out_cents': carried_out[statement['statement_id']] if statement else 0,
            'adjustment_period': pending['target_period'] if pending else '',
            'tier_changed': bool(statement) and statement['tier'] != tier,
        })
    return rows


def report(snapshot, statements):
    rows = merge(snapshot, statements)
    closed = closed_periods(statements)
    pending = [row for row in rows if row['adjustment_cents']]
    closing = closing_period(rows, statements)
    candidates = sorted(({row['period'] for row in rows if row['has_data']}
                         | {row['adjustment_period'] for row in pending}) - closed)
    by_venue = {}
    for row in rows:
        venue = by_venue.setdefault(row['venue'], {
            'venue': row['venue'], 'periods': 0, 'net_cents': 0, 'expected_share_cents': 0,
            'confirmed_share_cents': 0, 'adjustment_cents': 0, 'period': '', 'tier': '',
            'tier_label': '', 'rate_label': '', 'status': '', 'adjustment_period': '',
        })
        venue['periods'] += 1
        venue['net_cents'] += row['net_cents']
        venue['expected_share_cents'] += row['expected_share_cents']
        venue['confirmed_share_cents'] += row['confirmed_share_cents'] or 0
        venue['adjustment_cents'] += row['adjustment_cents']
        if row['has_data'] and row['period'] >= venue['period']:
            venue.update(period=row['period'], tier=row['tier'], tier_label=row['tier_label'],
                         rate_label=row['rate_label'], status=row['status'])
        if row['adjustment_cents'] and not venue['adjustment_period']:
            venue['adjustment_period'] = row['adjustment_period']
    carried = defaultdict(int)
    for row in rows:
        if row['adjustment_cents']:
            carried[row['adjustment_period']] += row['adjustment_cents']
    voucher = closing or (sorted(closed)[-1] if closed else '')
    closing_incoming = sum(row['adjustment_cents'] for row in pending if row['adjustment_period'] == closing)
    totals = {
        'current_period': closing, 'has_closing': bool(closing), 'incoming_cents': closing_incoming,
        'net_cents': sum(row['net_cents'] for row in rows),
        'expected_share_cents': sum(row['expected_share_cents'] for row in rows),
        'confirmed_share_cents': sum(row['confirmed_share_cents'] or 0 for row in rows),
        'settled_adjustment_cents': sum(row['settled_adjustment_cents'] for row in rows),
        'settled_total_cents': sum(row['settled_total_cents'] or 0 for row in rows),
        'adjustment_cents': sum(row['adjustment_cents'] for row in rows),
        'open_periods': candidates,
        'closed_periods': sorted(closed),
        'voucher_period': voucher,
        'carried_cents': dict(sorted(carried.items())),
    }
    adjustments = [{'source_period': row['period'], 'venue': row['venue'],
                    'target_period': row['adjustment_period'], 'amount_cents': row['adjustment_cents'],
                    'confirmed_share_cents': row['confirmed_share_cents'],
                    'expected_share_cents': row['expected_share_cents'],
                    'confirmed_tier': row['confirmed_tier'], 'current_tier': row['tier'],
                    'statement_id': row['statement_id'], 'tier_changed': row['tier_changed']}
                   for row in pending]
    return {'rows': rows, 'venues': list(by_venue.values()), 'totals': totals,
            'periods': sorted({row['period'] for row in rows}), 'closed_periods': sorted(closed),
            'adjustments': adjustments, 'tiers': tier_card(), 'decision': DECISION}


def period_export(snapshot, statements, period):
    """本期结算凭证：本期各馆结算单、本期转入的调整、本期尚未结转的差额。"""
    rows = [row for row in merge(snapshot, statements) if row['period'] == period]
    own = [statement for statement in statements if statement['period'] == period]
    closed = bool(own)
    adjustments = []
    for statement in own:
        for item in statement.get('adjustments', []):
            adjustments.append({
                'direction': '转入', 'venue': statement['venue'], 'source_period': item['source_period'],
                'target_period': period, 'amount_cents': item['amount_cents'],
                'confirmed_share_cents': item['confirmed_share_cents'],
                'expected_share_cents': item['expected_share_cents'],
                'source_statement_id': item['source_statement_id'], 'carried': True})
    own_ids = {statement['statement_id'] for statement in own}
    for target in statements:
        if target['period'] <= period:
            continue
        for item in target.get('adjustments', []):
            if item['source_statement_id'] not in own_ids:
                continue
            adjustments.append({
                'direction': '转出', 'venue': target['venue'], 'source_period': period,
                'target_period': target['period'], 'amount_cents': item['amount_cents'],
                'confirmed_share_cents': item['confirmed_share_cents'],
                'expected_share_cents': item['expected_share_cents'],
                'source_statement_id': item['source_statement_id'], 'carried': True})
    pending = {item['source_statement_id']: item for item in outstanding_adjustments(snapshot, statements)}
    for statement in own:
        item = pending.get(statement['statement_id'])
        if item:
            adjustments.append({
                'direction': '待结转', 'venue': statement['venue'], 'source_period': period,
                'target_period': item['target_period'], 'amount_cents': item['amount_cents'],
                'confirmed_share_cents': statement['share_cents'],
                'expected_share_cents': item['expected_share_cents'],
                'source_statement_id': statement['statement_id'], 'carried': False})
    if not closed:
        for item in outstanding_adjustments(snapshot, statements):
            if item['target_period'] == period:
                adjustments.append({
                    'direction': '待转入', 'venue': item['venue'], 'source_period': item['source_period'],
                    'target_period': period, 'amount_cents': item['amount_cents'],
                    'confirmed_share_cents': item['confirmed_share_cents'],
                    'expected_share_cents': item['expected_share_cents'],
                    'source_statement_id': item['source_statement_id'], 'carried': False})
    totals = {
        'net_cents': sum(row['net_cents'] for row in rows),
        'expected_share_cents': sum(row['expected_share_cents'] for row in rows),
        'confirmed_share_cents': sum(row['confirmed_share_cents'] or 0 for row in rows),
        'adjustment_in_cents': sum(item['amount_cents'] for item in adjustments if item['direction'] == '转入'),
        'adjustment_out_cents': sum(item['amount_cents'] for item in adjustments
                                    if item['direction'] in {'转出', '待结转'}),
        'settled_total_cents': sum(row['settled_total_cents'] or 0 for row in rows),
    }
    return {'period': period, 'closed': closed, 'rows': rows, 'adjustments': adjustments,
            'totals': totals}
