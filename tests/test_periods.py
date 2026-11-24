"""跨月夜班工资期间分摊、封存与补卡调整的端到端规则测试。"""
from decimal import Decimal
from pathlib import Path

import pytest

from app import period_store as ps
from app.errors import DomainError
from app.periods import service
from app.periods.export import period_csv

DATA = Path(__file__).resolve().parents[1] / 'sample_data'


@pytest.fixture
def cross_month():
    return (DATA / 'cross_month.csv').read_bytes()


@pytest.fixture
def supplement():
    return (DATA / 'supplement.csv').read_bytes()


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'periods.db'))
    yield


def _ledger(period):
    return service.get_period(period)['body']


# ---------- 裁决：午夜切段，两期片段逐项对应 ----------

def test_cross_month_shift_splits_at_midnight_into_two_periods(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    october = _ledger('2026-10'); november = _ledger('2026-11')
    e1_oct = [f for f in october['fragments'] if f['id'] == 'E001/2026-10-31']
    e1_nov = [f for f in november['fragments'] if f['id'] == 'E001/2026-10-31']
    # 同一 shift_id 串联两个期间
    assert {f['calendar_day'] for f in e1_oct} == {'2026-10-31'}
    assert {f['calendar_day'] for f in e1_nov} == {'2026-11-01'}
    assert sum(f['minutes'] for f in e1_oct) == 120
    assert sum(f['minutes'] for f in e1_nov) == 300
    view = next(v for v in october['shifts'] if v['shift_id'] == 'E001/2026-10-31')
    assert view['periods'] == ['2026-10', '2026-11'] and view['complete']


def test_fragment_rate_and_payable_line_up(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    november = _ledger('2026-11')
    rows = [f for f in november['fragments'] if f['id'] == 'E001/2026-10-31']
    # REST 倍率 2x：300 分钟拆成两段计薪，逐段金额合计 200
    assert all(f['code'] == 'REST' and f['multiplier'] == '2' for f in rows)
    assert sum(f['minutes'] for f in rows) == 300
    assert sum(Decimal(f['amount']) for f in rows) == Decimal('200.00')
    assert november['business']['total'] == '200.00'


def test_holiday_carries_across_period_boundary(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    september = _ledger('2026-09'); october = _ledger('2026-10')
    # 09-30 工作日 1x：120 分钟 = 40；10-01 法定假日 3x：300 分钟 = 300
    assert september['business']['total'] == '40.00'
    holiday = [f for f in october['fragments']
               if f['id'] == 'E002/2026-09-30' and f['code'] == 'HOLIDAY']
    assert sum(Decimal(f['amount']) for f in holiday) == Decimal('300.00')


def test_period_amounts_sum_to_global_payable(ledger_db,cross_month):
    result = service.submit(cross_month, {}, 'import')
    state = result  # 直接再跑一次全量流水线核对全局
    from app.periods.service import build_state, run_pipeline
    with ps.connection() as db:
        full = run_pipeline(build_state(db))
    period_sum = sum(
        (Decimal(l['body']['business']['total']) for l in service.list_periods()), Decimal(0))
    assert period_sum == Decimal(full['business']['total'])


# ---------- 片段缺失 / 对账不平 阻止发布 ----------

def test_missing_fragment_blocks_release(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    october = _ledger('2026-10')
    assert october['release_blocked']
    assert any(b['code'] == 'MISSING_SEGMENT' and b['shift_id'] == 'E003/2026-10-31'
               for b in october['blockers'])
    with pytest.raises(DomainError) as e:
        service.seal('2026-10')
    assert e.value.status == 422 and e.value.code == 'BLOCKED'


def test_complete_periods_can_seal(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    out = service.seal('2026-09')
    ledger = service.get_period('2026-09')
    assert ledger['status'] == 'sealed' and out['body_hash'] == ledger['body_hash']


def test_unbalanced_global_reconcile_blocks(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    with ps.connection() as db:
        # 篡改全局对账输入计数，制造不平；同连接重算保证事务可见
        db.execute('UPDATE period_batches SET input_count=input_count+1')
        service.recompute(db)
    for ledger in service.list_periods():
        assert any(b['code'] == 'GLOBAL_UNBALANCED' for b in ledger['body']['blockers'])


# ---------- 截止后补卡：封存不改写，未封存重核差额，重跑不重复 ----------

def test_supplement_keeps_sealed_period_immutable(ledger_db,cross_month, supplement):
    service.submit(cross_month, {}, 'import')
    sealed_hash = service.seal('2026-09')['body_hash']
    service.seal('2026-11')
    service.submit(supplement, {}, 'supplement')
    september = service.get_period('2026-09')
    assert september['status'] == 'sealed'
    assert september['body_hash'] == sealed_hash
    assert september['version'] == 1
    # 封存体内容也保持
    assert september['body']['business']['total'] == '40.00'


def test_supplement_rechecks_open_period_with_delta(ledger_db,cross_month, supplement):
    service.submit(cross_month, {}, 'import')
    service.seal('2026-09'); service.seal('2026-11')
    before = service.get_period('2026-10')
    assert before['version'] == 1
    service.submit(supplement, {}, 'supplement')
    october = service.get_period('2026-10')
    # 未封存期间重新核对并出新版，缺卡班次补齐后阻断消失
    assert october['version'] == 2 and not october['body']['release_blocked']
    e3 = next(v for v in october['body']['shifts'] if v['shift_id'] == 'E003/2026-10-31')
    assert e3['complete'] and e3['periods'] == ['2026-10', '2026-11']


def test_sealed_period_delta_diverts_to_next_open_period(ledger_db,cross_month, supplement):
    service.submit(cross_month, {}, 'import')
    service.seal('2026-09'); service.seal('2026-11')
    result = service.submit(supplement, {}, 'supplement')
    deltas = result['new_adjustments']
    # 11 月已封存，差额全部转入下一个未封存期间 12 月
    assert {a['source_period'] for a in deltas} == {'2026-11'}
    assert {a['target_period'] for a in deltas} == {'2026-12'}
    e1 = next(a for a in deltas if a['shift_id'] == 'E001/2026-10-31')
    assert e1['delta_amount'] == '40.00' and e1['delta_minutes'] == 60
    december = _ledger('2026-12')
    assert december['business']['adjustment_total'] == '240.00'
    assert december['business']['payable'] == '240.00'


def test_same_supplement_rerun_creates_no_duplicate_adjustment(ledger_db,cross_month, supplement):
    service.submit(cross_month, {}, 'import')
    service.seal('2026-09'); service.seal('2026-11')
    first = service.submit(supplement, {}, 'supplement')
    second = service.submit(supplement, {}, 'supplement')
    assert len(first['new_adjustments']) == 2
    assert second['idempotent'] is True and second['new_adjustments'] == []
    assert len(_ledger('2026-12')['adjustments']) == 2
    with ps.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM period_adjustments').fetchone()[0] == 2


def test_seal_recompute_does_not_duplicate_adjustments(ledger_db, cross_month, supplement):
    service.submit(cross_month, {}, 'import')
    service.seal('2026-09'); service.seal('2026-11')
    service.submit(supplement, {}, 'supplement')
    # 封存其它期间触发的重算不得再生调整
    service.seal('2026-10')
    with ps.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM period_adjustments').fetchone()[0] == 2
    assert len(_ledger('2026-12')['adjustments']) == 2


def test_sealed_period_rejects_direct_snapshot_write(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    service.seal('2026-09')
    with ps.connection() as db:
        with pytest.raises(DomainError) as e:
            ps.upsert_snapshot(db, '2026-09', {'tampered': True}, 5)
        assert e.value.status == 409 and e.value.code == 'SEALED'


# ---------- 重算失败恢复旧版 ----------

def test_recompute_failure_restores_old_version(ledger_db,cross_month, supplement, monkeypatch):
    service.submit(cross_month, {}, 'import')
    before = {l['period']: (l['version'], l['body_hash']) for l in service.list_periods()}
    batch_count = len(ps.load_batches(ps.connection()))
    monkeypatch.setattr(service, 'build_snapshots',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    with pytest.raises(RuntimeError):
        service.submit(supplement, {}, 'supplement')
    after = {l['period']: (l['version'], l['body_hash']) for l in service.list_periods()}
    assert after == before
    assert len(ps.load_batches(ps.connection())) == batch_count


# ---------- 页面与导出同版，原始打卡不丢 ----------

def test_export_uses_same_stored_snapshot_and_hash(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    ledger = service.get_period('2026-10')
    text = period_csv(ledger)
    assert ledger['body_hash'] in text
    for keyword in ('期间快照', '期间对账', '工作片段', '计薪片段', '班次串联', '封存校验'):
        assert keyword in text


def test_raw_punches_are_retained_including_quarantine(ledger_db,cross_month):
    service.submit(cross_month, {}, 'import')
    with ps.connection() as db:
        punches = ps.load_punches(db)
        batches = ps.load_batches(db)
    assert len(punches) == 5  # 五条逻辑打卡全部留痕（含后续被 m4 判缺卡的 IN）
    punch_ids = {p['id'] for p in punches}
    assert {'01001', '01002', '01003', '01004', '01005'} <= punch_ids
    # 缺 OUT 卡的隔离证据出现在归属期间快照，记录不丢失
    october = _ledger('2026-10')
    missing = [q for q in october['quarantine'] if q['code'] == 'MISSING' and q['id'] == '01005']
    assert missing
    # 原始字节完整留存，可重建
    import base64
    raw = base64.b64decode(batches[0]['source_b64'])
    assert raw == cross_month


def test_duplicate_same_key_dedup_conflict_isolated(ledger_db,cross_month):
    # 同 (id,version) 完全相同 -> 重复；内容冲突 -> 隔离
    service.submit(cross_month, {}, 'import')
    dup = cross_month  # 整批重提，同键同内容
    result = service.submit(dup, {}, 'import')
    assert result['idempotent'] is True
    with ps.connection() as db:
        statuses = [r['status'] for r in db.execute(
            'SELECT status FROM period_punches').fetchall()]
    assert 'duplicate' not in statuses  # 整批幂等拦截在批次层，未再写入


# ---------- HTTP 层 ----------

def test_http_period_flow(client, cross_month, supplement):
    assert client.get('/periods').status_code == 200
    assert client.post('/api/periods/import', json={}).status_code == 200
    listed = client.get('/api/periods').get_json()
    assert {x['period'] for x in listed} == {'2026-09', '2026-10', '2026-11'}
    blocked = client.post('/api/periods/2026-10/seal')
    assert blocked.status_code == 422
    assert client.post('/api/periods/2026-09/seal').status_code == 200
    assert client.post('/api/periods/2026-11/seal').status_code == 200
    sup = client.post('/api/periods/supplement', json={})
    assert sup.status_code == 200 and len(sup.get_json()['new_adjustments']) == 2
    export = client.get('/api/periods/2026-12/export.csv')
    assert export.status_code == 200 and '封存期调整' in export.data.decode('utf-8-sig')
    page = client.get('/periods?period=2026-12').data.decode()
    assert '240.00' in page


def test_http_export_aligned_with_page(client):
    client.post('/api/periods/import', json={})
    body = client.get('/api/periods/2026-10').get_json()
    page = client.get('/periods?period=2026-10').data.decode()
    export = client.get('/api/periods/2026-10/export.csv').data.decode('utf-8-sig')
    assert body['body_hash'] in page and body['body_hash'] in export
