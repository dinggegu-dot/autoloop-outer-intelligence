"""P0：把已鉴权、已选定的看板任务快照转换为只读摘要，不负责查找任务或投递。"""
from dataclasses import dataclass
import hashlib

from workbench import STATES, TASK_RE


@dataclass(frozen=True)
class TaskBrief:
    task_id: str
    version: int
    status: str
    progress: str
    next_action: str

    def as_private_reply(self):
        return (f'{self.task_id} · 方案 v{self.version}\n{self.status}\n'
                f'{self.progress}\n{self.next_action}')


def task_brief(task):
    task_id = task['id']
    version = task['version']
    if not TASK_RE.fullmatch(task_id) or type(version) is not int or version < 1:
        raise ValueError('缺少有效的任务编号或版本')
    status = task.get('status')
    runs = task.get('runs', [])
    if (task.get('observation_stale') is not False or status not in STATES
            or (runs and runs[0].get('state') == 'unknown')):
        return TaskBrief(task_id, version, '状态待核对', '当前进度未知', '请在连接恢复后重查')

    progress = task['progress']
    steps = f"已完成步骤 {progress['done']}/{progress['total']}" if progress['total'] else '暂无已登记步骤'
    action = '无需操作，可稍后查询'
    label = STATES[status]
    if task.get('revision_pending'):
        label = task['status_label']
        action = '请查看任务改版记录'
    elif status == 'completed':
        action = '可在看板核对成果和验收记录'
    elif task.get('workflow') == 'goal' and status == 'created':
        action = '后台将自动开始规划'
    elif task.get('workflow') == 'goal' and status == 'budget_exhausted':
        action = '工单授权的执行时间已用尽，可在看板核对已有证据'
    elif status in ('paused', 'stopping', 'blocked', 'budget_exhausted', 'rejected'):
        action = {'stopping': '等待停止确认，不要重复发起',
                  'paused': '如需继续，请核对任务后恢复',
                  'blocked': '请查看阻塞原因',
                  'budget_exhausted': '需本人决定是否追加预算',
                  'rejected': '本版不再执行'}[status]
    elif any(card.get('state') == 'open' for card in task.get('cards', [])):
        action = '需本人处理本版待办'
    elif status == 'awaiting_approval':
        action = '尚未查到可操作的本版卡片，请重查'
    return TaskBrief(task_id, version, label, steps, action)


def milestone(task):
    brief=task_brief(task)
    if brief.status=='状态待核对':return {'known':False}
    state=task['status']
    if state not in ('awaiting_approval','blocked','budget_exhausted','completed') or task.get('revision_pending'):
        return {'known':True,'key':None}
    extra=''
    if state=='awaiting_approval':
        cards=sorted((card['id'],card['version']) for card in task.get('cards',[]) if card['state']=='open')
        if not cards:return {'known':False}
        extra=str(cards)
    elif state=='blocked':
        extra=hashlib.sha256((task.get('error') or '').encode()).hexdigest()
    return {'known':True,'key':f"{brief.version}:{state}:{extra}",'body':brief.as_private_reply()}
