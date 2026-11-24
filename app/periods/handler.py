"""跨期工资期间分摊的页面与接口。"""
from flask import Blueprint, Response, jsonify, render_template, request

from app.config import MODULES, SHORT, TITLE
from app.errors import DomainError
from app.periods import service
from app.periods.export import period_csv
from app.web import payload

bp = Blueprint('periods', __name__)

PERIOD_SAMPLES = {'cross_month.csv', 'supplement.csv'}


def _payload(sample_default):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise DomainError('PAYLOAD', '请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'], str):
        raise DomainError('CSV', 'CSV内容必须是文本')
    sample = data.get('sample', sample_default)
    if sample not in PERIOD_SAMPLES:
        raise DomainError('SAMPLE', '期间账样例名称无效: %s' % sample)
    from pathlib import Path
    from app.workflow import load as _load  # 复用样例加载限制
    if 'csv' in data:
        content = data['csv'].encode()
    else:
        path = Path(__file__).resolve().parents[2] / 'sample_data' / sample
        content = path.read_bytes()
    return content, data.get('parameters', {}) or {}


@bp.get('/periods')
def screen():
    ledgers = service.list_periods()
    selected = request.args.get('period')
    current_ledger = None
    if selected:
        current_ledger = service.get_period(selected)
    elif ledgers:
        current_ledger = service.get_period(ledgers[-1]['period'])
    return render_template('periods.html', title=TITLE, short=SHORT, modules=MODULES,
                           current=8, ledgers=ledgers, ledger=current_ledger)


@bp.get('/api/periods')
def list_all():
    return jsonify(service.list_periods())


@bp.get('/api/periods/<period>')
def detail(period):
    return jsonify(service.get_period(period))


@bp.post('/api/periods/import')
def import_batch():
    content, parameters = _payload('cross_month.csv')
    return jsonify(service.submit(content, parameters, 'import'))


@bp.post('/api/periods/supplement')
def supplement():
    content, parameters = _payload('supplement.csv')
    return jsonify(service.submit(content, parameters, 'supplement'))


@bp.post('/api/periods/<period>/seal')
def seal(period):
    return jsonify(service.seal(period))


@bp.get('/api/periods/<period>/export.csv')
def export(period):
    ledger = service.get_period(period)
    if ledger['body'] is None:
        raise DomainError('EMPTY', '期间尚无快照')
    return Response(
        period_csv(ledger), mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition':
                 'attachment; filename=period-%s-v%s.csv' % (period, ledger['version'])})
