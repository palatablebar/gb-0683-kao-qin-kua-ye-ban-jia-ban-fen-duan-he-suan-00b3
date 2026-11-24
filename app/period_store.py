"""跨期工资期间封存账（SQLite 持久层）。

设计要点：
- period_batches：每次导入/补卡的原始字节与计数，原始记录永不丢失；
- period_punches：逐条逻辑打卡（含隔离、重复），按批次留痕；
- period_ledgers：每个工资期间一份快照，open 可重出版本，sealed 只校验不改写；
- period_adjustments：封存期间收到补卡产生的差额调整，带幂等指纹。
"""
import base64
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.errors import DomainError


def _now():
    return datetime.now(timezone.utc).isoformat()


def connection():
    path = Path(os.environ.get('DB_PATH', 'data/records.db'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    _init_schema(db)
    return db


def _init_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS period_batches(
        batch_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
        source_b64 TEXT NOT NULL, source_sha256 TEXT NOT NULL, encoding TEXT,
        parameters TEXT NOT NULL,
        input_count INTEGER NOT NULL DEFAULT 0, blank_count INTEGER NOT NULL DEFAULT 0,
        header_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
        accepted_count INTEGER NOT NULL DEFAULT 0, isolated_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS period_punches(
        id TEXT NOT NULL, version TEXT NOT NULL, batch_id TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL,
        employee TEXT, shift_day TEXT, period TEXT,
        line INTEGER, line_token TEXT NOT NULL,
        payload TEXT NOT NULL, quarantine_code TEXT,
        PRIMARY KEY(id, version, batch_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS period_ledgers(
        period TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'open',
        version INTEGER NOT NULL DEFAULT 0,
        body TEXT, body_hash TEXT,
        cutoff_day INTEGER, sealed_at TEXT, updated_at TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS period_adjustments(
        adjust_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
        source_period TEXT NOT NULL, target_period TEXT NOT NULL,
        shift_id TEXT NOT NULL, employee TEXT,
        digest TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    db.commit()


def canonical(body):
    def default(obj):
        if isinstance(obj, (Decimal,)):
            return str(obj)
        raise TypeError('不可序列化类型: %s' % type(obj))
    return json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), default=default)


def hash_body(body):
    return hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()


def batch_fingerprint(content, parameters, kind):
    raw = content + canonical(parameters).encode() + kind.encode()
    return hashlib.sha256(raw).hexdigest()[:20]


# ---------- 批次与原始打卡 ----------

def get_batch(db, batch_id):
    row = db.execute('SELECT * FROM period_batches WHERE batch_id=?', (batch_id,)).fetchone()
    return dict(row) if row else None


def has_accepted_key(db, punch_id, version):
    return db.execute(
        'SELECT 1 FROM period_punches WHERE id=? AND version=? AND status=? LIMIT 1',
        (punch_id, version, 'accepted')).fetchone() is not None


def save_batch(db, batch, punches):
    """整批写入；调用方负责外层事务，失败即回滚。"""
    db.execute('''INSERT INTO period_batches(batch_id,kind,source_b64,source_sha256,encoding,
        parameters,input_count,blank_count,header_count,duplicate_count,accepted_count,
        isolated_count,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (batch['batch_id'], batch['kind'],
         base64.b64encode(batch['content']).decode('ascii'),
         hashlib.sha256(batch['content']).hexdigest(), batch.get('encoding'),
         canonical(batch['parameters']), batch['input_count'], batch['blank_count'],
         batch['header_count'], batch['duplicate_count'], batch['accepted_count'],
         batch['isolated_count'], _now()))
    for p in punches:
        db.execute('''INSERT INTO period_punches(id,version,batch_id,kind,status,employee,
            shift_day,period,line,line_token,payload,quarantine_code)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (p['id'], p['version'], batch['batch_id'], batch['kind'], p['status'],
             p.get('employee'), p.get('shift_day'), p.get('period'), p.get('line'),
             p['line_token'], canonical(p['payload']), p.get('quarantine_code')))


def load_punches(db):
    return [dict(r) for r in db.execute('SELECT * FROM period_punches ORDER BY rowid').fetchall()]


def load_batches(db):
    return [dict(r) for r in db.execute('SELECT * FROM period_batches ORDER BY rowid').fetchall()]


def latest_parameters(db):
    row = db.execute('SELECT parameters FROM period_batches ORDER BY rowid DESC LIMIT 1').fetchone()
    return json.loads(row['parameters']) if row else {}


# ---------- 期间快照 ----------

def get_ledger(db, period):
    row = db.execute('SELECT * FROM period_ledgers WHERE period=?', (period,)).fetchone()
    if not row:
        raise DomainError('NOT_FOUND', '工资期间不存在: %s' % period, 404)
    out = dict(row)
    out['body'] = json.loads(out['body']) if out['body'] else None
    return out


def list_ledgers(db):
    rows = db.execute('SELECT * FROM period_ledgers ORDER BY period').fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item['body'] = json.loads(item['body']) if item['body'] else None
        out.append(item)
    return out


def sealed_periods(db):
    return {r['period'] for r in db.execute(
        "SELECT period FROM period_ledgers WHERE status='sealed'").fetchall()}


def upsert_snapshot(db, period, body, cutoff_day):
    """open 期间：内容变化才出新版本；sealed 期间禁止写入（由 service 层先行拦截）。"""
    body_hash = hash_body(body)
    existing = db.execute('SELECT * FROM period_ledgers WHERE period=?', (period,)).fetchone()
    if existing and existing['status'] == 'sealed':
        raise DomainError('SEALED', '期间已封存，禁止改写: %s' % period, 409)
    if existing and existing['body_hash'] == body_hash:
        db.execute('UPDATE period_ledgers SET updated_at=? WHERE period=?', (_now(), period))
        return existing['version'], False
    if existing:
        db.execute('''UPDATE period_ledgers SET version=version+1, body=?, body_hash=?,
            cutoff_day=?, updated_at=? WHERE period=?''',
            (canonical(body), body_hash, cutoff_day, _now(), period))
        return existing['version'] + 1, True
    db.execute('''INSERT INTO period_ledgers(period,status,version,body,body_hash,cutoff_day,updated_at)
        VALUES(?,?,1,?,?,?,?)''', (period, 'open', canonical(body), body_hash, cutoff_day, _now()))
    return 1, True


def seal_ledger(db, period, body):
    body_hash = hash_body(body)
    existing = db.execute('SELECT * FROM period_ledgers WHERE period=?', (period,)).fetchone()
    if not existing:
        raise DomainError('NOT_FOUND', '工资期间不存在: %s' % period, 404)
    if existing['status'] == 'sealed':
        raise DomainError('SEALED', '期间已封存: %s' % period, 409)
    db.execute('''UPDATE period_ledgers SET status='sealed', body=?, body_hash=?, sealed_at=?,
        updated_at=? WHERE period=?''',
        (canonical(body), body_hash, _now(), _now(), period))


# ---------- 调整（封存期间补卡差额） ----------

def adjustment_exists(db, batch_id, source_period, shift_id):
    return db.execute('''SELECT 1 FROM period_adjustments
        WHERE batch_id=? AND source_period=? AND shift_id=? LIMIT 1''',
        (batch_id, source_period, shift_id)).fetchone() is not None


def save_adjustment(db, adj):
    db.execute('''INSERT INTO period_adjustments(adjust_id,batch_id,source_period,target_period,
        shift_id,employee,digest,body,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
        (adj['adjust_id'], adj['batch_id'], adj['source_period'], adj['target_period'],
         adj['shift_id'], adj.get('employee'), adj['digest'], canonical(adj), _now()))


def list_adjustments(db, period=None):
    if period:
        rows = db.execute(
            'SELECT * FROM period_adjustments WHERE target_period=? OR source_period=? ORDER BY rowid',
            (period, period)).fetchall()
    else:
        rows = db.execute('SELECT * FROM period_adjustments ORDER BY rowid').fetchall()
    return [json.loads(r['body']) for r in rows]
