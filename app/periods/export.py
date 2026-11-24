"""工资期间封存导出：与页面读取同一份存储快照（同版本、同校验值）。"""
import csv
import io
import json

from app import period_store as ps


def period_csv(ledger):
    body = ledger['body'] or {}
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    rid = '%s-v%s' % (ledger['period'], ledger['version'])
    writer.writerow(['记录类型', '期间', '版本', '来源行/班次', '业务ID', '原因码', '内容'])
    header = {'period': ledger['period'], 'status': ledger['status'],
              'version': ledger['version'], 'body_hash': ledger['body_hash'],
              'cutoff_day': ledger.get('cutoff_day'), 'sealed_at': ledger.get('sealed_at')}
    writer.writerow(['期间快照', ledger['period'], rid, '', '', '',
                     json.dumps(header, ensure_ascii=False, sort_keys=True)])
    writer.writerow(['期间对账', ledger['period'], rid, '', '', '',
                     json.dumps(body.get('reconcile', {}), ensure_ascii=False, sort_keys=True)])
    writer.writerow(['期间汇总', ledger['period'], rid, '', '', '',
                     json.dumps(body.get('business', {}), ensure_ascii=False, sort_keys=True)])
    # 原始打卡 → 片段 → 倍率 → 应付 的逐项链路
    for seg in body.get('segments', []):
        writer.writerow(['工作片段', ledger['period'], rid, seg['line'], seg['id'], '',
                         json.dumps(seg, ensure_ascii=False, sort_keys=True)])
    for f in body.get('fragments', []):
        writer.writerow(['计薪片段', ledger['period'], rid, f['line'], f['id'], f['code'],
                         json.dumps(f, ensure_ascii=False, sort_keys=True)])
    for view in body.get('shifts', []):
        writer.writerow(['班次串联', ledger['period'], rid, '', view['shift_id'], '',
                         json.dumps(view, ensure_ascii=False, sort_keys=True)])
    for adj in body.get('adjustments', []):
        writer.writerow(['封存期调整', ledger['period'], rid, adj['batch_id'],
                         adj['shift_id'], adj['reason'],
                         json.dumps(adj, ensure_ascii=False, sort_keys=True)])
    for item in body.get('incoming', []):
        writer.writerow(['补卡隔离证据', ledger['period'], rid, item['line'], item['id'],
                         item['code'], json.dumps(item, ensure_ascii=False, sort_keys=True)])
    for q in body.get('quarantine', []):
        writer.writerow(['隔离', ledger['period'], rid, q['line'], q.get('id', ''), q['code'],
                         json.dumps(q.get('raw', {}), ensure_ascii=False, sort_keys=True)])
    for blocker in body.get('blockers', []):
        writer.writerow(['发布阻断', ledger['period'], rid, '', blocker.get('shift_id', ''),
                         blocker['code'], json.dumps(blocker, ensure_ascii=False, sort_keys=True)])
    # 页面对齐校验：导出内联同一份 body_hash
    writer.writerow(['封存校验', ledger['period'], rid, '', '', '', ledger['body_hash']])
    return '﻿' + out.getvalue()


def period_checksum(ledger):
    return ps.hash_body(ledger['body']) if ledger.get('body') else None
