"""原单续改。每轮固定一个已经交付的文件版本，下一轮独立批准和验收。"""
import json
import re
import unicodedata
from workbench import Problem


# 原话中必须能直接定位被改掉的旧值。只有新值时，仅在数量和单位能对应时识别。
_QUANTITY = r'0-9零一二两三四五六七八九十百千万'
_REPLACE = r'(?:改为|改成|换成|换为|替换为|调整为|缩短至|缩短为|延长至|延长为)'


def changes_old_requirement(clause, old):
    def normalize(text):
        return unicodedata.normalize('NFKC', text).casefold().strip().rstrip('。.')

    clause, old = normalize(clause), normalize(old)
    # 整条复合旧要求不能只因其中一个片段被改变就一起撤销。
    if re.search(r'[,;。!?、]|并|同时|以及|而且|且|和|与', old):
        return False
    clause = re.sub(r'^(?:请(?:帮我)?|帮我)', '', clause).strip()
    if (re.search(r'[,;。!?、]|不要改|别改|保留|不变|照旧|改不改', clause) or
            re.match(r'(?:再|另外|同时)?(?:新增|增加|追加|添加|补充|附上|另加)|(?:add|append)\b', clause)):
        return False
    removal = re.fullmatch(r'(?:取消|删去|删除|去掉|移除|撤回|不再需要|remove\s+|drop\s+)(.+)', clause)
    if removal:
        target = removal[1].strip(' “”‘’「」『』"')
        return len(target) >= 2 and target in old
    explicit = (re.fullmatch(r'(?:把|将|从|由)?(.+?)' + _REPLACE + r'(.+)', clause)
                or re.fullmatch(r'(?:change|replace)\s+(.+?)\s+(?:to|with)\s+(.+)', clause))
    if explicit:
        target, value = (part.strip(' “”‘’「」『』"') for part in explicit.groups())
        if len(target) < 2 or not value or target == value:
            return False
        # “7天”不能指向“17天”。
        boundary = f'(?<![{_QUANTITY}])' if re.match(f'[{_QUANTITY}]', target) else ''
        return bool(re.search(boundary + re.escape(target), old))
    quantity = re.fullmatch(_REPLACE + f'([{_QUANTITY}]+)([^0-9]+)', clause)
    if quantity:
        prior = re.search(f'([{_QUANTITY}]+)' + re.escape(quantity[2]), old)
        return bool(prior and prior[1] != quantity[1])
    return False


def delivered_base(task):
    delivery = task.get('goal_delivery')
    if task.get('status') != 'completed' or not delivery or not delivery.get('files'):
        return None
    return {key: delivery[key] for key in ('assignment', 'artifact_sha256', 'files')}


def round_view(ticket, task, *, brief=False):
    record = ticket.get('work_round') or {}
    execution_round = task.get('work_round_number', 1)
    eligible = (ticket.get('status') == 'AUTHORIZED' and bool(ticket.get('ticket_team'))
                and ticket.get('allow_model_input') is True)
    settled = delivered_base(task) if eligible else None
    view = dict(round=record.get('number', 1), execution_round=execution_round, can_start=bool(settled))
    if brief:
        if settled:
            view['base'] = {key:settled[key] for key in ('assignment','artifact_sha256')}
        return view
    return dict(view, base=settled or record.get('base'), source_delivery=record.get('base'),
                source_round=record.get('source_round'), request=record.get('request'))


def start_round(db, ticket, event, reference):
    if not ticket.get('task_id'):
        raise Problem('当前工单还没有可继续修改的交付版本', 409)
    task = json.loads(db.execute('SELECT body FROM tasks WHERE id=?', (ticket['id'],)).fetchone()[0])
    view = round_view(ticket, task)
    if not view['can_start'] or (reference is not None and reference != {
            key: view['base'][key] for key in ('assignment', 'artifact_sha256')}):
        raise Problem('交付版本已变化，请从原单最新交付继续修改', 409)
    if db.execute("SELECT 1 FROM outer_events WHERE ticket=? AND id!=? AND role IS NOT NULL "
                  "AND state IN ('PENDING','RUNNING') LIMIT 1", (ticket['id'], event['id'])).fetchone():
        raise Problem('这张单已有待处理的修改，请刷新查看', 409)
    ticket.update(work_round=dict(number=view['round'] + 1, source_round=view['execution_round'],
                                  event=event['id'], request=json.loads(event['body'])['text'], base=view['base']),
                  status='ASSESSING', questions_asked=0, error='', revision=ticket['revision'] + 1)


def verified_input(db, store, ticket):
    """仅返回上轮已交付清单内、逐字节摘要仍一致的文件。"""
    from .outer_verification import capture
    record = ticket['work_round']
    base = record['base']
    # 同轮中途重新授权也从本轮指定的已交付版本取件。
    row = db.execute("""SELECT json_extract(body,'$.goal_delivery') FROM outer_revisions
        WHERE ticket=? AND kind='execution_archive' AND json_extract(body,'$.status')='completed'
        AND coalesce(json_extract(body,'$.work_round_number'),1)=?
        AND json_extract(body,'$.goal_delivery.assignment')=?""",
        (ticket['id'], record['source_round'], base['assignment'])).fetchone()
    delivery = json.loads(row[0]) if row else {}
    if any(delivery.get(key) != base[key] for key in ('assignment','artifact_sha256','files')):
        raise Problem('续改依据与本单已交付版本不一致', 409)
    directory = store.root / 'goal-attempts' / ticket['id'] / base['assignment'] / 'verified'
    try:
        checkpoint, files, occupied = capture(directory)
    except (ValueError, OSError):
        raise Problem('上一轮已交付文件无法核对，请检查归档后重试', 409) from None
    if (checkpoint['files'] != base['files'] or checkpoint['digest'] != base['artifact_sha256'] or
            any(not is_dir and name not in files for name, is_dir in occupied.items())):
        raise Problem('上一轮归档与交付记录不一致，未开始新版', 409)
    return files
