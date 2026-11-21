from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.browse import snapshot_for
from app.config import REPORT_ZONE, ROOT
from app.modules.m8_settlement.bundle import statements_for
from app.modules.m8_settlement.service import report

router = APIRouter()
templates = Jinja2Templates(directory=ROOT / 'app' / 'templates')
MODULES = (
    ('intake', '票源导入'), ('rights', '套票权益'), ('scans', '入场核销'), ('allocation', '权重分账'),
    ('refunds', '退款冲正'), ('reconcile', '场馆勾稽'), ('settlement', '场馆结算'), ('evidence', '结算凭证'),
)


def money(amount):
    integer, decimal = divmod(abs(amount), 100)
    return f'{"−" if amount < 0 else ""}{integer:,}.{decimal:02d}'


templates.env.filters['money'] = money


@router.get('/', response_class=HTMLResponse)
def home(request: Request, run: str = '', notice: str = ''):
    snapshot = snapshot_for(request, run) if run else request.app.state.store.latest()
    snapshot = snapshot or request.app.state.preview
    manifest = snapshot['manifest']
    local_time = datetime.fromisoformat(manifest['generated_at']).astimezone(ZoneInfo(REPORT_ZONE))
    return templates.TemplateResponse(request, 'index.html', {
        'snapshot': snapshot, 'manifest': manifest, 'funding': snapshot['funding'],
        'preview': manifest['run_id'] == 'sample', 'modules': MODULES, 'local_time': local_time.strftime('%m-%d %H:%M:%S'),
        'settlement': report(snapshot, statements_for(request.app.state.store, snapshot)),
        'notice': {'published': '批次已发布', 'reused': '已复用原批次',
                   'closed': '本期已关账', 'close-reused': '本期此前已关账，未新增结算记录'}.get(notice, ''),
    })
