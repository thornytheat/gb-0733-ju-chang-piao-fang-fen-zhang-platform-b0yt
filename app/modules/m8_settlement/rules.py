"""场馆阶梯分成规则。

档位按「每期净票房」确定：净票房 = 该场馆该期原分账 − 已入账停演退款。
分成 = 净票房 × 档位比例，按分向下取整，不足一分的尾差留在场馆侧。
演示费率卡可通过替换 TIERS 接入真实合同；比例使用整数基点，避免浮点误差。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import REPORT_ZONE

PERIOD_ZONE = ZoneInfo(REPORT_ZONE)

# (档位编号, 档位名称, 净票房下限（分）, 分成比例（基点）)
TIERS = (
    ('T1', '档一', 0, 800),          # 净票房 < ¥100
    ('T2', '档二', 10_000, 1_000),   # ¥100 – ¥499.99
    ('T3', '档三', 50_000, 1_200),   # ¥500 – ¥999.99
    ('T4', '档四', 100_000, 1_500),  # ≥ ¥1,000
)


def period_of(iso_timestamp):
    """业务时刻按上海时区归入自然月，如 2026-11。"""
    return datetime.fromisoformat(iso_timestamp).astimezone(PERIOD_ZONE).strftime('%Y-%m')


def next_period(period):
    year, month = (int(part) for part in period.split('-'))
    return f'{year + month // 12:04d}-{month % 12 + 1:02d}'


def rate_label(basis_points):
    whole, fraction = divmod(basis_points, 100)
    if fraction == 0:
        return f'{whole}%'
    return f'{whole}.{fraction:02d}'.rstrip('0').rstrip('.') + '%'


def tier_for(net_cents):
    chosen = TIERS[0]
    for tier in TIERS:
        if net_cents >= tier[2]:
            chosen = tier
    code, label, threshold, rate = chosen
    return {'tier': code, 'tier_label': label, 'threshold_cents': threshold,
            'rate_bp': rate, 'rate_label': rate_label(rate)}


def share_cents(net_cents, basis_points):
    """按基点计提，向下取整；结果恒为整数分。"""
    return net_cents * basis_points // 10_000


def tier_card():
    return [{'tier': code, 'tier_label': label, 'threshold_cents': threshold,
             'rate_bp': rate, 'rate_label': rate_label(rate)}
            for code, label, threshold, rate in TIERS]

DECISION = ('关账后不改写当期结算单。退款导致净票房回落档位时，差额作为待调整金额'
            '转入下一未关账期，不并入该期净票房，因此不会再次改变档位。')
