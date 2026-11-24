"""跨期工资期间分摊服务。

裁决：跨月夜班不整班挂起始月，也不为赶截止日只结前半段，而是按法定日历日在午夜
切分为期间片段；片段以同一 shift_id 串联，各自进入所属工资期间独立封存。
封存期间收到补卡只能生成差额调整，落到下一个未封存期间；同一补卡批次重跑幂等。
"""
import contextvars
import json
from copy import deepcopy
from decimal import Decimal

from app import period_store as ps
from app.errors import DomainError
from app.modules.m1_ingest.reader import read
from app.modules.m2_mapping.rules import normalize
from app.modules.m3_versions.rules import apply as pair
from app.modules.m4_segments.rules import apply as segment
from app.modules.m5_calculate.rules import apply as price
from app.modules.m6_reconcile.compare import balance

DEFAULT_CUTOFF = 5


def next_period(period):
    year, month = map(int, period.split('-'))
    month += 1
    if month == 13:
        year, month = year + 1, 1
    return '%04d-%02d' % (year, month)


def _period_of(shift_day):
    return shift_day[:7] if isinstance(shift_day, str) and len(shift_day) >= 7 else 'UNKNOWN'


# ---------------- 入库（首次导入 / 补卡共用） ----------------

def ingest(content, parameters, kind='import'):
    """解析一批打卡并写入期间账；返回 (batch, punches)。不触碰快照。"""
    parameters = parameters or {}
    cutoff_day = int(parameters.get('cutoff_day', DEFAULT_CUTOFF))
    parsed = read(content)
    parsed['parameters'] = dict(parameters, cutoff_day=cutoff_day)
    normalized = normalize(parsed)

    batch_id = ps.batch_fingerprint(content, parameters, kind)
    punches = []
    cross_duplicates = 0
    cross_conflicts = 0

    def signature(row):
        return str(sorted((k, str(v)) for k, v in row.items() if not k.startswith('_')))

    # m1/m2 已隔离的记录原样留痕
    for q in normalized['quarantine']:
        src = q.get('raw', {})
        punches.append({
            'id': str(q.get('id', '')), 'version': _extract_version(src),
            'status': 'isolated', 'employee': _extract(src, 'employee'),
            'shift_day': _extract(src, 'shift_day'),
            'period': _period_of(_extract(src, 'shift_day')),
            'line': q['line'], 'line_token': '%s#%s' % (batch_id, q['line']),
            'payload': {'quarantine': {'id': q.get('id', ''), 'code': q['code'], 'raw': src}},
            'quarantine_code': q['code']})

    for row in normalized['rows']:
        line = row['_line']
        token = '%s#%s' % (batch_id, line)
        payload = {k: v for k, v in row.items() if not k.startswith('_')}
        payload['_raw'] = row.get('_raw', {})
        common = {'id': str(row['id']), 'version': str(row['version']),
                  'employee': row.get('employee'), 'shift_day': row.get('shift_day'),
                  'period': _period_of(row.get('shift_day')),
                  'line': line, 'line_token': token}
        # 跨批次同键：完全相同记重复，内容冲突隔离（不取最后一行）
        existing = _accepted_signature(row['id'], row['version'])
        if existing is None:
            punches.append(dict(common, status='accepted', payload=payload))
        elif existing == signature(row):
            cross_duplicates += 1
            punches.append(dict(common, status='duplicate', payload=payload))
        else:
            cross_conflicts += 1
            punches.append(dict(common, status='isolated', payload=payload,
                                quarantine_code='CONFLICT'))

    accepted = sum(1 for p in punches if p['status'] == 'accepted')
    isolated = sum(1 for p in punches if p['status'] == 'isolated')
    batch = {
        'batch_id': batch_id, 'kind': kind, 'content': content,
        'encoding': parsed['encoding'], 'parameters': normalized['parameters'],
        'input_count': parsed['input_count'], 'blank_count': parsed['blank_count'],
        'header_count': parsed['header_count'],
        'duplicate_count': parsed['duplicate_count'] + cross_duplicates,
        'accepted_count': accepted, 'isolated_count': isolated + cross_conflicts}
    return batch, punches


def _extract(raw, key):
    return raw.get(key) if isinstance(raw, dict) else None


def _extract_version(raw):
    value = _extract(raw, 'version')
    return str(value) if value not in (None, '') else '1'


def _accepted_signature(punch_id, version):
    """进程内签名缓存由 submit 注入；缺省回落到数据库查询。"""
    cache = _ACCEPT_CACHE.get()
    if cache is not None:
        return cache.get((str(punch_id), str(version)))
    with ps.connection() as db:
        rows = db.execute(
            "SELECT payload FROM period_punches WHERE id=? AND version=? AND status='accepted'",
            (str(punch_id), str(version))).fetchall()
    if not rows:
        return None
    payload = json.loads(rows[0]['payload'])
    return str(sorted((k, str(v)) for k, v in payload.items() if not k.startswith('_')))


class _Cache:
    _var = contextvars.ContextVar('accept_cache', default=None)

    def get(self):
        return self._var.get()

    def set(self, value):
        return self._var.set(value)

    def reset(self, token):
        self._var.reset(token)


_ACCEPT_CACHE = _Cache()


# ---------------- 从持久账重建流水线状态 ----------------

def build_state(db):
    punches = ps.load_punches(db)
    batches = ps.load_batches(db)
    state = {'rows': [], 'quarantine': [], 'parameters': ps.latest_parameters(db),
             'input_count': 0, 'blank_count': 0, 'header_count': 0,
             'duplicate_count': 0, 'encoding': 'ledger'}
    for b in batches:
        state['input_count'] += b['input_count']
        state['blank_count'] += b['blank_count']
        state['header_count'] += b['header_count']
        state['duplicate_count'] += b['duplicate_count']
    for p in punches:
        payload = json.loads(p['payload'])
        if p['status'] == 'accepted':
            row = {k: v for k, v in payload.items() if k != '_raw'}
            row['_line'] = p['line_token']
            row['_raw'] = payload.get('_raw', {})
            state['rows'].append(row)
        elif p['status'] == 'isolated':
            if 'quarantine' in payload:
                q = payload['quarantine']
                state['quarantine'].append(
                    {'line': p['line_token'], 'id': q.get('id', ''),
                     'code': p['quarantine_code'] or q['code'], 'raw': q.get('raw', {})})
            else:
                state['quarantine'].append(
                    {'line': p['line_token'], 'id': p['id'],
                     'code': p['quarantine_code'] or 'CONFLICT',
                     'raw': payload.get('_raw', payload)})
    return state


def run_pipeline(state):
    state = deepcopy(state)
    state = pair(state)
    state = segment(state)
    state = price(state)
    state['reconcile'] = balance(state)
    return state


# ---------------- 期间快照 ----------------

def _line_period_index(db):
    return {p['line_token']: p['period'] for p in ps.load_punches(db)}


def build_snapshots(db, state, adjustments, cutoff_day):
    periods = {}
    line_period = _line_period_index(db)
    all_punches = ps.load_punches(db)
    global_reconcile = state['reconcile']

    def bucket(period):
        return periods.setdefault(period, {
            'period': period, 'cutoff_day': cutoff_day,
            'fragments': [], 'segments': [], 'shift_ids': set(),
            'quarantine': [], 'blockers': [], 'warnings': []})

    # 片段与计薪证据按期归集
    for calc in state['calculations']:
        p = bucket(calc['calendar_day'][:7])
        p['fragments'].append(calc)
        p['shift_ids'].add(calc['id'])
    for seg in state['segments']:
        bucket(seg['calendar_day'][:7])['segments'].append(seg)
        periods[seg['calendar_day'][:7]]['shift_ids'].add(seg['id'])

    # 班次视图（跨期串联）与缺卡阻断
    shift_views = {}
    surviving = {s['id']: s for s in state['shifts']}
    for sid, shift in surviving.items():
        home = _period_of(shift['day'])
        view = {'shift_id': sid, 'employee': shift['employee'], 'shift_day': shift['day'],
                'home_period': home,
                'periods': sorted({c['calendar_day'][:7] for c in state['calculations']
                                   if c['id'] == sid}),
                'complete': bool(shift.get('in') and shift.get('out'))}
        shift_views[sid] = view
        if not view['complete']:
            bucket(home)['blockers'].append(
                {'code': 'MISSING_SEGMENT', 'shift_id': sid,
                 'message': '班次缺少进出打卡，片段不完整'})
        expected = _expected_span_months(shift)
        actual = set(view['periods'])
        missing = expected - actual
        for month in missing:
            bucket(month)['blockers'].append(
                {'code': 'MISSING_SEGMENT', 'shift_id': sid,
                 'message': '跨期班次在 %s 缺少工作片段' % month})
        for month in expected | actual | {home}:
            bucket(month)['shift_ids'].add(sid)

    # 隔离证据按来源打卡归属期间
    for q in state['quarantine']:
        month = line_period.get(q['line'], 'UNKNOWN')
        bucket(month)['quarantine'].append(q)
        periods[month]['warnings'].append(
            {'code': q['code'], 'line': q['line'], 'message': '存在隔离打卡，需人工复核'})

    # 实发按打卡归属期间（与 m5 全局 paid 口径一致：仅存活行）
    paid_by_period = {}
    for row in state['rows']:
        month = _period_of(row['shift_day'])
        paid_by_period[month] = paid_by_period.get(month, Decimal(0)) + Decimal(str(row['paid']))

    # 调整落到未封存目标期间
    adj_by_target = {}
    for adj in adjustments:
        adj_by_target.setdefault(adj['target_period'], []).append(adj)
        bucket(adj['target_period'])
    incoming_supplements = _incoming_evidence(db)

    bodies = {}
    all_fragment_total = sum((Decimal(c['amount']) for c in state['calculations']), Decimal(0))
    period_total_sum = Decimal(0)

    for month, p in periods.items():
        fragments = sorted(p['fragments'], key=lambda r: (r['start'], r['employee'], r['code']))
        total = sum((Decimal(f['amount']) for f in fragments), Decimal(0))
        minutes = sum(f['minutes'] for f in fragments)
        paid = paid_by_period.get(month, Decimal(0))
        period_adjs = adj_by_target.get(month, [])
        adjustment_total = sum((Decimal(a['delta_amount']) for a in period_adjs), Decimal(0))
        period_total_sum += total

        # 片段分钟守恒：计薪片段分钟之和须等于切段分钟之和
        seg_minutes = sum(s['minutes'] for s in p['segments'])
        if minutes != seg_minutes:
            p['blockers'].append({'code': 'AMOUNT_MISMATCH', 'period': month,
                                  'message': '计薪分钟 %d 与切段分钟 %d 不平' % (minutes, seg_minutes)})

        # 期间内打卡守恒（统一使用调用方连接，保证事务内可见性一致）
        month_punches = [x for x in all_punches if x['period'] == month]
        accepted_n = sum(1 for x in month_punches if x['status'] == 'accepted')
        isolated_n = sum(1 for x in month_punches if x['status'] == 'isolated')
        duplicate_n = sum(1 for x in month_punches if x['status'] == 'duplicate')
        punch_total = len(month_punches)
        reconcile = {
            'accepted': accepted_n, 'isolated': isolated_n, 'duplicates': duplicate_n,
            'punch_total': punch_total,
            'balanced_punches': punch_total == accepted_n + isolated_n + duplicate_n,
            'global_balanced': global_reconcile['balanced'],
            'global': global_reconcile}
        if not global_reconcile['balanced']:
            p['blockers'].append({'code': 'GLOBAL_UNBALANCED', 'period': month,
                                  'message': '全局行数对账不平，禁止发布'})

        bodies[month] = {
            'period': month, 'cutoff_day': p['cutoff_day'],
            'fragments': fragments,
            'segments': sorted(p['segments'], key=lambda r: (r['start'], r['employee'])),
            'shifts': sorted((shift_views[s] for s in p['shift_ids'] if s in shift_views),
                             key=lambda v: v['shift_id']),
            'quarantine': p['quarantine'], 'warnings': p['warnings'],
            'blockers': p['blockers'], 'release_blocked': bool(p['blockers']),
            'reconcile': reconcile,
            'adjustments': period_adjs,
            'incoming': incoming_supplements.get(month, []),
            'business': {
                'total': str(total), 'paid': str(paid),
                'difference': str(total - paid), 'minutes': minutes,
                'adjustment_total': str(adjustment_total),
                'payable': str(total + adjustment_total), 'unit': 'CNY'},
            'global_business': state['business']}

    # 跨期间金额守恒：各期间片段合计必须等于全局应发
    if period_total_sum != all_fragment_total:
        for body in bodies.values():
            body['blockers'].append(
                {'code': 'AMOUNT_MISMATCH', 'period': body['period'],
                 'message': '期间合计 %s 与全局应发 %s 不平'
                            % (period_total_sum, all_fragment_total)})
            body['release_blocked'] = True
    return bodies


def _expected_span_months(shift):
    if not shift.get('in') or not shift.get('out'):
        return set()
    from datetime import datetime
    from app.modules.m3_versions.rules import ZONE
    start = datetime.fromisoformat(shift['in']['at']).astimezone(ZONE)
    end = datetime.fromisoformat(shift['out']['at']).astimezone(ZONE)
    months = {start.date().strftime('%Y-%m')}
    if start.month != end.month or start.year != end.year:
        months.add(end.date().strftime('%Y-%m'))
    return months


def _incoming_evidence(db):
    """封存期间补卡产生的隔离证据，展示在下一未封存期间。"""
    out = {}
    sealed = ps.sealed_periods(db)
    for p in ps.load_punches(db):
        if p['status'] == 'isolated' and p['period'] in sealed:
            target = next_period(p['period'])
            out.setdefault(target, []).append({
                'batch_id': p['batch_id'], 'source_period': p['period'],
                'id': p['id'], 'line': p['line_token'],
                'code': p['quarantine_code'] or 'ISOLATED'})
    return out


# ---------------- 重算与调整 ----------------

def recompute(db, batch_id=None, create_adjustments=True):
    state = run_pipeline(build_state(db))
    sealed = ps.sealed_periods(db)
    cutoff_day = int((ps.latest_parameters(db) or {}).get('cutoff_day', DEFAULT_CUTOFF))

    # 先按“无调整”口径生成候选快照，用于封存期间差异比对
    candidate = build_snapshots(db, state, [], cutoff_day)
    adjustments = ps.list_adjustments(db)
    new_adjustments = []

    if not create_adjustments:
        # 封存前重算只刷新未封存期间快照，绝不新增调整（无驱动批次）
        final = build_snapshots(db, state, adjustments, cutoff_day)
        written = []
        for month, body in sorted(final.items()):
            if month in sealed:
                if not ps.get_ledger(db, month).get('body_hash'):
                    raise DomainError('SEALED', '封存快照缺少校验值，已终止重算', 409)
                continue
            version, changed = ps.upsert_snapshot(db, month, body, cutoff_day)
            written.append({'period': month, 'version': version, 'changed': changed})
        return {'state': state, 'written': written,
                'new_adjustments': [], 'sealed_periods': sorted(sealed)}

    for month in sorted(sealed):
        old = ps.get_ledger(db, month)
        new = candidate.get(month)
        if not new or not old.get('body'):
            continue
        # 封存快照不得改写：upsert_snapshot 对 sealed 直接拒绝；这里仅以差异驱动调整
        old_by_shift = _totals_by_shift(old['body'])
        new_by_shift = _totals_by_shift(new)
        for sid in sorted(set(old_by_shift) | set(new_by_shift)):
            old_amount, old_minutes = old_by_shift.get(sid, ('0', 0))
            new_amount, new_minutes = new_by_shift.get(sid, ('0', 0))
            delta = Decimal(new_amount) - Decimal(old_amount)
            d_minutes = new_minutes - old_minutes
            if delta == 0 and d_minutes == 0:
                continue
            # 调整必须由具体批次驱动；同一批次对同一封存期间/班次只生成一次
            if not batch_id:
                continue
            if ps.adjustment_exists(db, batch_id, month, sid):
                continue  # 同一补卡重跑：不重复生成调整
            target = next_period(month)
            digest = ps.hash_body(
                {'batch': batch_id, 'source': month, 'shift': sid,
                 'old': old_amount, 'new': new_amount, 'minutes': d_minutes})
            adj = {
                'adjust_id': digest[:20], 'batch_id': batch_id,
                'source_period': month, 'target_period': target, 'shift_id': sid,
                'employee': _employee_of(state, sid),
                'old_amount': old_amount, 'new_amount': new_amount,
                'delta_amount': str(delta), 'old_minutes': old_minutes,
                'new_minutes': new_minutes, 'delta_minutes': d_minutes,
                'reason': 'SEALED_PERIOD_SUPPLEMENT', 'digest': digest}
            ps.save_adjustment(db, adj)
            new_adjustments.append(adj)

    # 含调整的最终快照只写未封存期间；封存期间原样保留
    final = build_snapshots(db, state, ps.list_adjustments(db), cutoff_day)
    written = []
    for month, body in sorted(final.items()):
        if month in sealed:
            # 封存期间：绝不写入，保留封存版本与校验值
            stored = ps.get_ledger(db, month)
            if not stored.get('body_hash'):
                raise DomainError('SEALED', '封存快照缺少校验值，已终止重算', 409)
            continue
        version, changed = ps.upsert_snapshot(db, month, body, cutoff_day)
        written.append({'period': month, 'version': version, 'changed': changed})
    return {'state': state, 'written': written,
            'new_adjustments': new_adjustments, 'sealed_periods': sorted(sealed)}


def _totals_by_shift(body):
    out = {}
    for f in body.get('fragments', []):
        amount, minutes = out.get(f['id'], ('0', 0))
        out[f['id']] = (str(Decimal(amount) + Decimal(f['amount'])),
                        minutes + f['minutes'])
    return out


def _employee_of(state, sid):
    for s in state['shifts']:
        if s['id'] == sid:
            return s['employee']
    return None


# ---------------- 对外事务入口 ----------------

def submit(content, parameters, kind):
    with ps.connection() as db:
        batch_id = ps.batch_fingerprint(content, parameters or {}, kind)
        if ps.get_batch(db, batch_id):
            return _idempotent_result(db, batch_id)
        # 构建跨批次签名缓存
        cache = {}
        for r in db.execute(
                "SELECT id,version,payload FROM period_punches WHERE status='accepted'"):
            payload = json.loads(r['payload'])
            cache[(r['id'], r['version'])] = str(
                sorted((k, str(v)) for k, v in payload.items() if not k.startswith('_')))
        token = _ACCEPT_CACHE.set(cache)
        try:
            batch, punches = ingest(content, parameters, kind)
        finally:
            _ACCEPT_CACHE.reset(token)
        ps.save_batch(db, batch, punches)
        result = recompute(db, batch_id)
    return {'batch_id': batch_id, 'kind': kind, **{k: v for k, v in result.items()
                                                   if k != 'state'}}


def _idempotent_result(db, batch_id):
    result = recompute(db, batch_id)
    return {'batch_id': batch_id, 'idempotent': True,
            'written': result['written'], 'new_adjustments': [],
            'sealed_periods': result['sealed_periods']}


def seal(period):
    with ps.connection() as db:
        ledger = ps.get_ledger(db, period)
        if ledger['status'] == 'sealed':
            raise DomainError('SEALED', '期间已封存: %s' % period, 409)
        # 封存前强制重算（只刷新快照，不生成调整），确保封存的是最新核对结果
        recompute(db, create_adjustments=False)
        ledger = ps.get_ledger(db, period)
        body = ledger['body']
        if body.get('release_blocked'):
            raise DomainError(
                'BLOCKED', '期间存在发布阻断，无法封存: %s'
                           % '; '.join(b['code'] for b in body['blockers']), 422)
        ps.seal_ledger(db, period, body)
        sealed = ps.get_ledger(db, period)
    return {'period': period, 'body_hash': sealed['body_hash'],
            'version': sealed['version']}


def get_period(period):
    with ps.connection() as db:
        return ps.get_ledger(db, period)


def list_periods():
    with ps.connection() as db:
        ledgers = ps.list_ledgers(db)
        adjustments = ps.list_adjustments(db)
    for ledger in ledgers:
        ledger['adjustment_count'] = sum(
            1 for a in adjustments if a['target_period'] == ledger['period'])
    return ledgers
