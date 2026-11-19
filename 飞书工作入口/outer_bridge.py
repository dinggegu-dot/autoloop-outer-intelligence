"""V2 本人私聊接入。复用 bridge 的消息鉴权、持久发送队列与按钮白名单。"""
import json
import re

def call(bridge, command, **payload):
    return bridge.board.call('outer', command=command, payload=payload)

def submit(bridge, event, text, **binding):
    if not 1 <= len(text.strip()) <= 6000:
        return '需求需为 1 至 6000 字。'
    payload = dict(request_id=event['message_id'], text=text.strip(), **binding)
    call(bridge,'submit',**payload)
    return '已接收。整理完成后，结果会发到本对话。'

def mirror_notifications(bridge):
    """先持久入桥，飞书确认投递与原卡归属之后再确认引擎通知。"""
    for delivery in call(bridge,'notifications'):
        key='outer-'+delivery['id']
        row=bridge.store.db.execute('SELECT state,ref FROM outbox WHERE id=?',(key,)).fetchone()
        if row:
            if row['state']=='sent':
                saved=bridge.store.db.execute('SELECT context FROM click_prompts WHERE outbox_id=?',(key,)).fetchone()
                if delivery.get('ticket_id') and saved:
                    binding=json.loads(saved[0])['binding']
                    try:
                        call(bridge,'bind',ticket_id=binding['ticket_id'],thread=row['ref'],revision=binding['revision'])
                    except ValueError:
                        # 旧卡仍然已投递；点操作时必须重新核对当前代次。
                        pass
                call(bridge,'delivered',id=delivery['id'])
            continue
        if delivery.get('ticket_id'):
            card=call(bridge,'card',ticket_id=delivery['ticket_id'])
        elif delivery.get('choices'):
            choices=delivery['choices']
            card={'body':'请选择这条需求对应的工单。','binding':{'event_id':delivery['event_id']},
                  'actions':[['outer_pick_'+str(i),str(v.get('summary') or v['id'])[:60]] for i,v in enumerate(choices)]}
            card['binding']['choices']=[v['id'] for v in choices]
            card['actions'].append(['outer_new','新建目标工单'])
        else:
            card={'body':delivery.get('error') or delivery.get('reason') or '已处理该消息，可在工作台查看。',
                  'binding':{},'actions':[]}
        bridge.queue_prompt(key,'outer',json.dumps(card,ensure_ascii=False),card['body'])

def directory(bridge,event):
    drafts=call(bridge,'directory')
    if not drafts:return '当前没有目标工单。可以直接发送需求。'
    actions=[['outer_open_'+str(i),row['id'][-8:]+' · '+row['status']] for i,row in enumerate(drafts)]
    card={'body':'选择要查看的目标工单。','binding':{'directory':[row['id'] for row in drafts]},'actions':actions}
    bridge.queue_prompt('reply-'+event['message_id'],'outer',json.dumps(card,ensure_ascii=False),card['body'])
    return card['body']

def parent_card(bridge, event):
    parent=event.get('reply_to')
    if not parent:
        return None
    row=bridge.store.db.execute("""SELECT context FROM click_prompts JOIN outbox ON id=outbox_id
        WHERE kind='outer' AND state='sent' AND ref=?""",(parent,)).fetchone()
    return json.loads(row[0]) if row else None

def respond(bridge,event,card):
    binding=card['binding']
    action=event.get('action') if event.get('message_type')=='wb_click' else None
    thread=event['reply_to']
    if 'directory' in binding:
        if not action or not re.fullmatch(r'outer_open_[0-7]',action):return '请使用原卡选择工单。'
        idx=int(action.rsplit('_',1)[1])
        if idx>=len(binding['directory']):return '工单选择已失效。'
        updated=call(bridge,'card',ticket_id=binding['directory'][idx])
        bridge.queue_prompt('reply-'+event['message_id'],'outer',json.dumps(updated,ensure_ascii=False),updated['body'])
        return updated['body']
    if 'event_id' in binding:
        if action=='outer_new':
            selected=None
        elif action and re.fullmatch(r'outer_pick_[0-9]+',action):
            i=int(action.rsplit('_',1)[1])
            if i>=len(binding['choices']): return '选择已失效。'
            selected=binding['choices'][i]
        else:
            return '请点选原卡上的工单。'
        call(bridge,'select',event_id=binding['event_id'],ticket_id=selected,request_id=event['message_id'])
        return '已记录工单选择。'
    ticket=binding.get('ticket_id')
    if not ticket:
        return '请从当前工单卡继续。'
    # 回执提交与用户点击可能交错；只对已投递给本人且已核对的原卡补写绑定。
    if action=='outer_status':
        updated=call(bridge,'card',ticket_id=ticket)
        bridge.queue_prompt('reply-'+event['message_id'],'outer',json.dumps(updated,ensure_ascii=False),updated['body'])
        return '当前工单卡已更新。'
    call(bridge,'bind',ticket_id=ticket,thread=thread,revision=binding['revision'])
    if action in ('outer_approve','outer_pause','outer_resume','outer_stop'):
        if action not in [name for name,_ in card['actions']]:return '当前卡不支持此操作。'
        call(bridge,action.removeprefix('outer_'),ticket_id=ticket,revision=binding['revision'],
             goal_revision=binding['goal_revision'],acceptance_revision=binding['acceptance_revision'],
             request_id=event['message_id'],thread=thread)
        return '决定已记录。'
    if action is not None:return '操作已失效。'
    return submit(bridge,event,event['content'],ticket_id=ticket,revision=binding['revision'],
                  question_id=binding.get('question_id'),thread=thread)
