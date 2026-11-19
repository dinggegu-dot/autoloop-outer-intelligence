"""执行资源保护：自动模式不按调用次数中断，旧任务保留原有约定。"""
import hashlib
import json

AUTO_SECONDS = 60 * 60
CALL_HARD_SECONDS = 15 * 60
CALL_IDLE_SECONDS = 10 * 60


def call_hard_seconds(task, role):
    """Allow an explicitly extended review without changing other calls."""
    limit = task.get('review_call_limit_seconds')
    if role == 'review' and type(limit) is int:
        return max(CALL_HARD_SECONDS, min(35 * 60, limit))
    return CALL_HARD_SECONDS


def automatic(task):
    return task.get('execution_mode', 'bounded') == 'auto'


def exhaustion(task):
    limit = task.get('max_calls')
    if not automatic(task) and type(limit) is int and task['calls'] >= limit:
        return '已达到原任务的调用次数预算。可切换自动完成后继续。'
    if task.get('elapsed_seconds', 0) >= task.get('time_budget_seconds', AUTO_SECONDS):
        return '已达到本任务累计执行时间预算，成果已保留；增加时间后可继续。'
    return None


def plan_limit(task):
    limit = task.get('max_calls')
    return min(5, limit - 2) if not automatic(task) and type(limit) is int else 5


def policy_label(task):
    mode = '自动完成，不按调用次数中断' if automatic(task) else '限定预算'
    limit = task.get('max_calls')
    if not automatic(task) and type(limit) is int:
        mode += f'，最多 {limit} 次调用'
    return mode + f'；累计执行时间预算 {task.get("time_budget_seconds", AUTO_SECONDS)//60} 分钟'


class ProgressWindow:
    """重复信号和心跳不延长等待；硬期限和总预算始终有效。"""
    def __init__(self, started, remaining, hard=CALL_HARD_SECONDS, idle=CALL_IDLE_SECONDS):
        self.started = self.last_progress = started
        self.deadline = started + min(hard, max(0, remaining))
        self.idle = idle
        self.seen = set()

    def note(self, key, at):
        if key and key not in self.seen:
            self.seen.add(key)
            self.last_progress = at

    def reason(self, at):
        if at >= self.deadline:
            return '本次执行达到时间保护上限，已有文件保留'
        if at - self.last_progress >= self.idle:
            return '长时间没有新的工具结果或文件进展，已有文件保留'
        return None


def command_progress(item):
    if item.get('exit_code') != 0:
        return None
    value = json.dumps([item.get('command'), item.get('aggregated_output')], ensure_ascii=False)
    return 'command:' + hashlib.sha256(value.encode()).hexdigest()
