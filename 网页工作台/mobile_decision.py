"""本人私聊只接受已呈现并绑定当前内容的方案决定。"""
import hashlib
from execution_policy import policy_label

from task_brief import task_brief
from workbench import Problem
import uuid


def selected(workbench, task_id):
    snapshot = workbench.snapshot(task_id)
    return next(task for task in snapshot['tasks'] if task['id'] == task_id)


def proposal(task):
    approval = task.get('approval') or {}
    if (task.get('paused') or task.get('revision_pending') or task_brief(task).status == '状态待核对'
            or task['status'] != 'awaiting_approval' or approval.get('kind') != 'plan'):
        return {'message':'当前没有可以在手机确认的方案，或任务状态待核对。'}
    cards = [card for card in task['cards'] if card['state']=='open' and
             card['binding']=={'task':task['id'],'version':task['version'],'approval':approval.get('id')}]
    if len(cards)!=1 or cards[0]['options']!=['按这个方案执行','暂不执行']:
        return {'message':'未找到唯一、可核对的方案卡，请在看板核对。'}
    card = cards[0]
    plan = task['plan']
    lines = [task['name'],f"工单 {task['id']} · 方案 v{task['version']}",'目标：'+task['goal'],
             '完成标准：'+(task['acceptance'] or '以步骤验收条件为准'),plan['summary'],'范围：'+plan['scope'],
             *('假设：'+value for value in plan['assumptions'])]
    for index,step in enumerate(plan['steps'],1):
        lines.append(f"{index}. {step['title']}：{step['objective']}；验收：{step['verification']}")
        for package in step.get('packages',[]):
            deps='、'.join(package['depends_on']) or '可独立开始'
            lines.append(f"{package['id']} · {package['capability']} · {package['objective']}\n"
                         f"依赖 {deps} · 产物 {'、'.join(package['outputs'])} · 核验 {'、'.join(package['checks'])}")
    lines.extend(['当前执行政策：'+policy_label(task),
                  '角色：'+ ' / '.join(task.get('role_models',{}).get(role,'未配置') for role in ('plan','execute','review')),
                  '仅直接回复本卡「确认执行」或「暂不执行」。'])
    body='\n'.join(lines)
    if len(body)>4000:
        return {'message':'完整方案超过一张私聊卡可展示的长度；未生成批准入口，请到看板核对全文。'}
    binding={'task':task['id'],'task_version':task['version'],'card':card['id'],'version':card['version'],
             'approval':approval['id'],'digest':hashlib.sha256(body.encode()).hexdigest()}
    return {'body':body,'binding':binding}


def answer(workbench,task_id,binding,choice):
    if type(choice) is not int or choice not in (0,1) or not isinstance(binding,dict) or binding.get('task')!=task_id:
        return '审批绑定无效，未执行。'
    task=selected(workbench,task_id)
    if task_brief(task).status=='状态待核对':
        return '状态待核对，未提交决定。'
    current=proposal(task)
    if current.get('binding')!=binding:
        already=next((card for card in task['cards'] if card['id']==binding.get('card')
                      and card['binding']=={'task':task_id,'version':binding.get('task_version'),'approval':binding.get('approval')}
                      and card['state'] in ('decided','done') and card['choice'] in (0,1)),None)
        if already:
            return '该卡已处理：'+('按方案执行' if already['choice']==0 else '暂不执行')+'。本次未重复提交。'
        return '此方案已过期、已处理或不可操作；未提交，请重新查看方案。'
    try:
        data={'card':binding['card'],'version':binding['version'],'choice':choice}
        result=workbench.answer(task_id,data,source='feishu') if task.get('workflow')=='goal' else workbench.answer(task_id,data)
    except Problem as error:
        if error.status in (400,404,409):return '此决定未生效，请重新请求当前方案。'
        raise
    return '相同决定已记录，未重复派发。' if result.get('duplicate') else '决定已送达中台，等待续办；不代表执行已完成。'


def control_card(task):
    if task.get('workflow')=='goal' and task['status']=='stopping':
        return {'message':'已收到暂停请求，正在等待原轮次退出。可点「看进度」查看结果。'}
    brief=task_brief(task)
    if brief.status=='状态待核对' or task.get('revision_pending'):
        return {'message':'状态待核对或正在改版，未生成控制卡。'}
    goal = task.get('workflow')=='goal'
    actions = task.get('goal_actions',{})
    resumable = actions.get('resume') if goal else (
        task['status'] in ('created','paused','blocked') and not task.get('pending_resume'))
    pausable = actions.get('pause') if goal else (
        not task.get('paused') and task['status'] in ('queued','planning','executing','reviewing','awaiting_approval'))
    if resumable:
        operation='resume'
        instruction='确认继续'
        effect='按现有时间预算和模型开始或续办；不会代你批准方案，也不会追加时间。'
    elif pausable:
        operation='pause'
        instruction='确认暂停'
        effect='仅暂停这张单；先请求取消，须待原轮次退出。'
    else:
        return {'message':'此状态没有可用的手机控制，请查看状态或回看板处理。'}
    body=brief.as_private_reply()+'\n'+task['name']+'\n'+policy_label(task)+'\n'+effect+'\n直接回复本卡「'+instruction+'」。'
    binding={'task':task['id'],'version':task['version'],'operation':operation,
             'digest':hashlib.sha256(body.encode()).hexdigest()}
    if goal:
        binding['stop_generation']=task['goal_execution'].get('stop_generation',0)
    return {'body':body,'binding':binding}


def control(workbench,task_id,binding,event):
    snapshot=selected(workbench,task_id)
    if control_card(snapshot).get('binding')!=binding:
        return '控制卡已过期，未执行；请重新查看控制。'
    goal = snapshot.get('workflow')=='goal'
    source = {'source':'feishu'} if goal else {}
    try:
        if binding['operation']=='pause':
            if goal:
                source['binding']=binding
            outcome=workbench.pause(task_id,**source)
            return '已请求暂停，等待实际停止。' if outcome['stopping'] else '已暂停，未发现活动轮次。'
        request=str(uuid.uuid5(uuid.NAMESPACE_URL,'odyssey:mobile-control:'+event))
        resume_args={'request_id':request}
        if goal:
            resume_args['stop_generation']=binding['stop_generation']
        result=workbench.resume(task_id,resume_args,**source)
        if result.get('awaiting_approval'):
            return '已恢复到待确认，请打开当前方案确认授权。'
        if goal and result.get('queued'):
            return '续办已登记，等待后台派工。'
        return '中台已接单，等待执行。' if result.get('queued') else '派发尚未确认，请核对看板。'
    except Problem:
        return '控制未确认。请在看板核对实际状态，再决定是否继续。'
