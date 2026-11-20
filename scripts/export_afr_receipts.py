"""从原生 Autoloop 账本导出指定 V2 工单的 AFR 本地检索回执。"""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from afr_command_agent import EVENT, HEX, LIMIT, REQUEST, SOURCE

RULE = 'afr-local-lookup'
STATES = frozenset(('ok', 'no_match', 'revision_changed', 'not_found', 'invalid_request',
                    'invalid_vault', 'io_error', 'afr_timeout', 'afr_response_limit',
                    'invalid_afr_response', 'read_verification_failed', 'invalid_local_input',
                    'invalid_configuration'))


def export(database, ticket):
    if not re.fullmatch(r'wb-[a-f0-9]{32}', ticket):
        raise ValueError('无效工单号')
    prefix = ticket + '-'
    with sqlite3.connect(database.resolve(strict=True).as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute("""SELECT e.id, e.subject, r.id, r.state, r.summary
            FROM events e JOIN run_events x ON x.event_id=e.id JOIN runs r ON r.id=x.run_id
            WHERE e.source=? AND e.type=? AND substr(e.subject,1,length(?))=?
              AND e.key=e.subject AND r.rule=? AND r.agent=? ORDER BY r.id LIMIT 101""",
            (SOURCE, EVENT, prefix, prefix, RULE, RULE)).fetchall()
    if not 1 <= len(rows) <= 100:
        raise ValueError('没有匹配回执或已超过单张工单的导出上限')
    runs = []
    for event, subject, run, state, raw in rows:
        if state != 'done' or not REQUEST.fullmatch(subject) or not raw or len(raw) > 6000:
            raise ValueError('工单仍有未结束或无有效回执的原生派工')
        item = json.loads(raw)
        refs = item.get('references') if isinstance(item, dict) else None
        if (not isinstance(refs, list) or len(refs) > LIMIT or item.get('request_id') != subject
                or type(item.get('version')) is not int or item['version'] != 1
                or item.get('status') not in STATES or type(item.get('verified_count')) is not int
                or len(refs) != item['verified_count'] or bool(refs) != (item['status'] == 'ok')):
            raise ValueError('回执版本、内容或请求绑定不一致')
        references = []
        for ref in refs:
            if not isinstance(ref, dict) or any(not isinstance(ref.get(k), str) or not HEX.fullmatch(ref[k])
                                                for k in ('ref', 'revision')):
                raise ValueError('回执引用无效')
            references.append({k: ref[k] for k in ('ref', 'revision')})
        row = dict(event=event, run=run, status=item['status'], verified_count=len(refs), references=references)
        # 合成试点声明的场景名可以分辨两次成功检索。其他请求编号只作绑定，不导出原文。
        case = subject[len(prefix):]
        if case in ('lookup', 'no-match', 'old-revision', 'lookup-after-edit'):
            row['case'] = case
        runs.append(row)
    return dict(ticket=ticket, afr_version=1, source=SOURCE, rule=RULE, runs=runs)


def write_once(path, data):
    # 完整写入后才让文件可见，重复导出不得改写已有输入。
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(staged, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                    raise ValueError('同名文件内容不同，必须保留原版并另行导出') from None
        finally:
            staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hub-db', required=True, type=Path)
    parser.add_argument('--ticket', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    payload = export(args.hub_db, args.ticket)
    write_once(args.output, (json.dumps(payload, ensure_ascii=False, indent=2)+'\n').encode())
    print(f"{len(payload['runs'])} 轮本地 AFR 回执已导出")


if __name__ == '__main__':
    main()
