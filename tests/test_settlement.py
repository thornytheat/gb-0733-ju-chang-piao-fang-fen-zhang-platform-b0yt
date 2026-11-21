import io
import json
import zipfile

import pytest

from app.modules.m8_settlement.rules import share_cents, tier_for
from app.modules.m8_settlement.service import closing_period, report
from app.pipeline import compute


def revised_sources(files):
    """在样例上追加一张海棠剧场大额票，便于关账后靠退款把该馆拉回低档。"""
    tickets = files['tickets.csv'].replace(
        b'00119,', b'00120,2026-11-04T09:23:00+08:00,2000.00,\xe5\x85\x83,paid,\xe6\xb5\x8b\xe8\xaf\x95\n00119,')
    rights = files['rights.csv'] + b'00120,A-LAKE,1\n'
    return {**files, 'tickets.csv': tickets, 'rights.csv': rights}


def cancelled_lake(files):
    shows = files['shows.csv'].decode('gb18030').replace(
        'A-LAKE,湖边来信,海棠剧场,open,2026-11-18T17:00:00+08:00,2026-11-18T21:00:00+08:00,',
        'A-LAKE,湖边来信,海棠剧场,open,2026-11-18T17:00:00+08:00,2026-11-18T21:00:00+08:00,2026-11-18T20:30:00+08:00')
    refund = b'R120-01,00120,A-LAKE,2026-11-19T10:00:00+08:00,2000.00,\xe5\x85\x83\n'
    return {**files, 'shows.csv': shows.encode('gb18030'), 'refunds.csv': files['refunds.csv'] + refund}


def test_tier_thresholds_and_share_round_down():
    assert tier_for(9_999)['tier'] == 'T1'
    assert tier_for(10_000)['tier'] == 'T2'
    assert tier_for(49_999)['tier'] == 'T2'
    assert tier_for(50_000)['tier'] == 'T3'
    assert tier_for(100_000)['tier'] == 'T4'
    assert share_cents(10_256, 1_000) == 1_025
    assert share_cents(1, 1_500) == 0


def test_sample_period_groups_venues_and_applies_tier(sample):
    rows = {row['venue']: row for row in sample['settlement']}
    assert {venue: (row['net_cents'], row['tier']) for venue, row in rows.items()} == {
        '北岸小剧场': (2_001, 'T1'), '海棠剧场': (10_256, 'T2'), '青瓦剧院': (136_708, 'T4'),
    }
    assert rows['青瓦剧院']['share_cents'] == 20_506


def test_before_close_everything_is_open_and_expected(client):
    run_id = client.post('/api/runs/sample').json()['run_id']
    data = client.get(f'/api/settlement/{run_id}').json()
    assert all(row['status'] == 'open' and row['confirmed_share_cents'] is None for row in data['items'])
    assert data['totals']['confirmed_share_cents'] == 0
    assert data['totals']['current_period'] == '2026-11'


def test_close_locks_statement_and_reclose_does_not_add_records(client, upload, files):
    run_id = upload(revised_sources(files)).json()['run_id']
    first = client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'}).json()
    assert first['reused'] is False and len(first['statement_ids']) == 3
    again = client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'}).json()
    assert again['reused'] is True and again['statement_ids'] == first['statement_ids']
    data = client.get(f'/api/settlement/{run_id}').json()
    assert data['totals']['closed_periods'] == ['2026-11']
    assert data['totals']['confirmed_share_cents'] > 0


def test_rerun_after_close_reuses_statements_without_new_records(client, upload, files):
    run_id = upload(revised_sources(files)).json()['run_id']
    closed = client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'}).json()
    replay = client.post(f'/api/runs/{run_id}/rerun')
    assert replay.status_code == 200 and replay.json()['reused'] is True
    data = client.get(f'/api/settlement/{run_id}').json()
    assert [row['statement_id'] for row in data['items'] if row['status'] == 'closed'] == closed['statement_ids']


def test_refund_tier_drop_keeps_closed_statement_and_carries_difference(client, upload, files):
    """关账后退款把海棠剧场从 T4 拉回 T2：当期结算单不动，差额转入下一期。"""
    run_id = upload(revised_sources(files)).json()['run_id']
    client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'})
    revised = upload(cancelled_lake(revised_sources(files))).json()['run_id']
    data = client.get(f'/api/settlement/{revised}').json()
    venue = next(row for row in data['items'] if row['venue'] == '海棠剧场')
    assert venue['status'] == 'closed'
    assert (venue['confirmed_tier'], venue['tier']) == ('T4', 'T2')
    assert venue['tier_changed'] is True
    assert venue['confirmed_share_cents'] == 31_538
    assert venue['expected_share_cents'] == 1_025
    assert venue['adjustment_cents'] == -30_513
    assert venue['adjustment_period'] == '2026-12'
    assert data['totals']['adjustment_cents'] == -30_513
    assert data['totals']['current_period'] == '2026-12'
    carried = client.get(f'/api/settlement/{revised}', params={'view': 'adjustments'}).json()['items']
    assert [(row['tier_changed'], row['amount_cents']) for row in carried] == [(True, -30_513)]


def test_adjustment_is_booked_next_period_without_changing_its_tier(client, upload, files):
    run_id = upload(revised_sources(files)).json()['run_id']
    client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'})
    revised = upload(cancelled_lake(revised_sources(files))).json()['run_id']
    client.post(f'/api/settlement/{revised}/close', data={'period': '2026-12'})
    data = client.get(f'/api/settlement/{revised}').json()
    december = next(row for row in data['items'] if row['period'] == '2026-12')
    assert december['net_cents'] == 0
    assert december['settled_adjustment_cents'] == -30_513
    assert december['settled_total_cents'] == -30_513
    assert data['totals']['adjustment_cents'] == 0
    assert data['totals']['closed_periods'] == ['2026-11', '2026-12']


def test_reclosing_a_closed_period_after_other_attempts_stays_idempotent(client, upload, files):
    run_id = upload(revised_sources(files)).json()['run_id']
    first = client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'}).json()
    empty = client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-12'})
    assert (empty.status_code, empty.json()['code']) == (409, 'PERIOD_EMPTY')
    late = client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'})
    assert late.status_code == 200 and late.json()['reused'] is True
    assert late.json()['statement_ids'] == first['statement_ids']
    assert client.get(f'/api/settlement/{run_id}').json()['totals']['closed_periods'] == ['2026-11']


def test_cannot_close_a_period_that_has_nothing_to_settle(client, upload, files):
    run_id = upload(revised_sources(files)).json()['run_id']
    response = client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-09'})
    assert (response.status_code, response.json()['code']) == (409, 'PERIOD_EMPTY')
    assert response.json()['detail'] == {'period': '2026-09', 'expected': '2026-11'}


def test_invalid_period_is_rejected(client):
    run_id = client.post('/api/runs/sample').json()['run_id']
    assert client.get(f'/api/settlement/{run_id}', params={'period': '2026-13'}).json()['code'] == 'PERIOD_INVALID'
    assert client.post(f'/api/settlement/{run_id}/close', data={'period': 'November'}).status_code == 422


def test_settlement_voucher_downloads_each_period(client, upload, files):
    run_id = upload(revised_sources(files)).json()['run_id']
    client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'})
    revised = upload(cancelled_lake(revised_sources(files))).json()['run_id']
    client.post(f'/api/settlement/{revised}/close', data={'period': '2026-12'})
    for period in ('2026-11', '2026-12'):
        response = client.get(f'/api/settlement/{revised}/export', params={'period': period})
        assert response.status_code == 200 and response.content.startswith(b'PK')
        assert response.headers['content-disposition'] == \
            f'attachment; filename="settlement-{revised}-{period}.zip"'
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            assert manifest['period'] == period and manifest['closed'] is True
            assert set(manifest['hashes']) == set(archive.namelist()) - {'manifest.json'}
            rows = archive.read('adjustments.csv').decode('utf-8-sig').splitlines()
    assert len(rows) == 2


def test_voucher_before_close_marks_pending_transfer(client, upload, files):
    run_id = upload(revised_sources(files)).json()['run_id']
    client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'})
    revised = upload(cancelled_lake(revised_sources(files))).json()['run_id']
    response = client.get(f'/api/settlement/{revised}/export', params={'period': '2026-12'})
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        rows = archive.read('adjustments.csv').decode('utf-8-sig').splitlines()
        assert manifest['closed'] is False
        assert rows[0].startswith('direction')
        assert '待转入' in rows[1] and '-30513' in rows[1]


def test_home_page_shows_tiers_confirmed_share_pending_and_voucher_link(client):
    run_id = client.post('/api/runs/sample').json()['run_id']
    client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'})
    html = client.get(f'/?run={run_id}').text
    for text in ('场馆结算', '档四', '已结分成', '待调整', '2026-11结算凭证'):
        assert text in html
    assert f'/api/settlement/{run_id}/export' in html
    assert '已关账 2026-11' in html


def test_preview_never_writes_settlements(client):
    assert client.post('/api/settlement/sample/close', data={'period': '2026-11'}).status_code == 404
    assert client.get('/api/settlement/sample').json()['totals']['closed_periods'] == []


def test_legacy_snapshot_without_settlement_field_is_recomputed(files):
    snapshot = compute('legacy', 'unit', files)
    snapshot.pop('settlement')
    rows = report(snapshot, [])['rows']
    assert {row['venue']: row['tier'] for row in rows} == {
        '北岸小剧场': 'T1', '海棠剧场': 'T2', '青瓦剧院': 'T4',
    }
    assert closing_period(rows, []) == '2026-11'


def test_closed_statement_survives_venue_without_current_data(files):
    """关账后又补报新快照、该馆当期无净票房时，原结算单仍须在报表中留痕。"""
    base = compute('tour-demo', 'unit', revised_sources(files))
    statement = {'statement_id': 'st_keep', 'book': 'tour-demo', 'period': '2026-11',
                 'venue': '海棠剧场', 'run_id': 'unit', 'publication_id': 'pub',
                 'closed_at': '2026-11-30T00:00:00+00:00', 'net_at_close_cents': 210_256,
                 'has_data': True, 'tier': 'T4', 'tier_label': '档四', 'threshold_cents': 100_000,
                 'rate_bp': 1_500, 'rate_label': '15%', 'share_cents': 31_538,
                 'adjustment_cents': 0, 'adjustments': []}
    snapshot = {**base, 'settlement': [row for row in base['settlement'] if row['venue'] != '海棠剧场']}
    result = report(snapshot, [statement])
    row = next(row for row in result['rows'] if row['venue'] == '海棠剧场')
    assert (row['status'], row['confirmed_share_cents'], row['confirmed_tier']) == ('closed', 31_538, 'T4')


def test_concurrent_close_writes_one_set_of_statements(tmp_path, files):
    from concurrent.futures import ThreadPoolExecutor

    from app.store import Store

    store = Store(tmp_path / 'close.sqlite3')
    store.initialize()
    snapshot, _ = store.commit('race-book', revised_sources(files))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda _: store.close_period('race-book', '2026-11', snapshot, 'race-run', '2026-11-30T00:00:00+00:00'),
            range(6)))
    fresh = [planned for planned, reused in results if not reused]
    assert len(fresh) == 1
    statements = store.settlements('race-book')
    assert len(statements) == 3
    assert {statement['closed_at'] for statement in statements} == {'2026-11-30T00:00:00+00:00'}


def test_failed_close_rolls_back_every_statement(tmp_path, files):
    import sqlite3

    from app.store import Store

    store = Store(tmp_path / 'rollback.sqlite3')
    store.initialize()
    snapshot, _ = store.commit('rollback-book', revised_sources(files))
    with sqlite3.connect(store.path) as db:
        db.executescript("""
            CREATE TRIGGER fail_venue BEFORE INSERT ON settlements
            WHEN NEW.venue = '青瓦剧院'
            BEGIN SELECT RAISE(ABORT, 'close stopped'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match='close stopped'):
        store.close_period('rollback-book', '2026-11', snapshot, 'r', '2026-11-30T00:00:00+00:00')
    assert store.settlements('rollback-book') == []
    with sqlite3.connect(store.path) as db:
        db.execute('DROP TRIGGER fail_venue')
    planned, reused = store.close_period('rollback-book', '2026-11', snapshot, 'r', '2026-11-30T00:00:00+00:00')
    assert not reused and len(planned) == 3


def test_settlement_api_filters_by_period_and_search(client, upload, files):
    run_id = upload(revised_sources(files)).json()['run_id']
    client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'})
    page = client.get(f'/api/settlement/{run_id}', params={'q': '青瓦', 'limit': 1})
    assert page.json()['total'] == 1 and page.json()['items'][0]['tier'] == 'T4'
    scoped = client.get(f'/api/settlement/{run_id}', params={'period': '2026-11'})
    assert scoped.json()['total'] == 3
    unknown = client.get(f'/api/settlement/{run_id}', params={'period': '2026-13'}).json()
    assert unknown['code'] == 'PERIOD_INVALID'


def test_period_export_for_unclosed_period_shows_pending_and_carried(client, upload, files):
    from app.modules.m8_settlement.service import period_export

    run_id = upload(revised_sources(files)).json()['run_id']
    client.post(f'/api/settlement/{run_id}/close', data={'period': '2026-11'})
    revised = upload(cancelled_lake(revised_sources(files))).json()['run_id']
    snapshot = client.app.state.store.get(revised)
    statements = client.app.state.store.settlements(snapshot['manifest']['book'])
    pending = period_export(snapshot, statements, '2026-12')
    assert pending['closed'] is False
    assert [(row['direction'], row['amount_cents']) for row in pending['adjustments']] == [('待转入', -30_513)]
    november = period_export(snapshot, statements, '2026-11')
    assert november['closed'] is True
    assert [(row['direction'], row['amount_cents']) for row in november['adjustments']] == [('待结转', -30_513)]
    assert november['totals']['adjustment_out_cents'] == -30_513
