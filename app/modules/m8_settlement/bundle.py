import io
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.config import PREVIEW
from app.manifest import canonical, checksum, utc_now
from app.modules.m7_evidence.csv_out import make_csv
from app.modules.m8_settlement.service import period_export, report

STATEMENT_FIELDS = ('period', 'venue', 'status', 'tier', 'tier_label', 'rate_label', 'net_cents',
                    'expected_share_cents', 'confirmed_share_cents', 'settled_adjustment_cents',
                    'settled_total_cents', 'adjustment_cents', 'adjustment_period', 'tier_changed',
                    'statement_id', 'closed_at', 'closed_run_id')
ADJUSTMENT_FIELDS = ('direction', 'source_period', 'target_period', 'venue', 'amount_cents',
                     'confirmed_share_cents', 'expected_share_cents', 'source_statement_id', 'carried')
TIER_FIELDS = ('tier', 'tier_label', 'threshold_cents', 'rate_bp', 'rate_label')


def settlement_voucher(snapshot, statements, period):
    voucher = period_export(snapshot, statements, period)
    files = {
        'settlements.csv': make_csv(voucher['rows'], STATEMENT_FIELDS),
        'adjustments.csv': make_csv(voucher['adjustments'], ADJUSTMENT_FIELDS),
        'tiers.csv': make_csv(report(snapshot, statements)['tiers'], TIER_FIELDS),
    }
    manifest = {
        'run_id': snapshot['manifest']['run_id'], 'book': snapshot['manifest']['book'],
        'publication_id': snapshot['manifest']['publication_id'],
        'period': period, 'closed': voucher['closed'],
        'generated_at': utc_now(), 'rule_version': snapshot['manifest']['rule_version'],
        'decision': '关账后不改写当期结算单；退款导致的档位回落差额转入下一未关账期调整。',
        'totals': voucher['totals'],
        'hashes': {name: checksum(data) for name, data in files.items()},
    }
    files['manifest.json'] = canonical(manifest)
    stream = io.BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
    return stream.getvalue()


def statements_for(store, snapshot):
    if snapshot['manifest']['run_id'] == PREVIEW:
        return []
    return store.settlements(snapshot['manifest']['book'])
