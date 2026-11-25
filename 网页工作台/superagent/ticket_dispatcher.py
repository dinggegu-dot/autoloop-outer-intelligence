"""Router 只挑选有待办的工单。每个团队只消费自己工单的下一条事件。"""
import threading
import time
from collections import deque


class TeamDispatcher:
    def __init__(self, intake, capacity=2):
        self.intake = intake
        self.capacity = capacity
        self.active = {}
        self.queue = deque()

    def dispatch_ready(self):
        self.active = {ticket: thread for ticket, thread in self.active.items() if thread.is_alive()}
        ready = dict.fromkeys(self.intake.pending_team_tickets())
        self.queue = deque(ticket for ticket in self.queue if ticket in ready and ticket not in self.active)
        known = set(self.queue) | self.active.keys()
        self.queue.extend(ticket for ticket in ready if ticket not in known)
        while self.queue and len(self.active) < self.capacity:
            ticket = self.queue.popleft()
            thread = threading.Thread(target=self.consume, args=(ticket,), daemon=True)
            thread.start()
            self.active[ticket] = thread

    def consume(self, ticket):
        try:
            self.intake.drain_ticket(ticket, limit=1)
        except Exception as error:
            # 租约及未结算的事件留在账本，下一轮按原单恢复。
            print('本单团队待恢复', ticket, type(error).__name__, flush=True)

    def serve(self):
        while True:
            try:
                self.dispatch_ready()
            except Exception as error:
                print('工单调度待恢复', type(error).__name__, flush=True)
            time.sleep(0.5)
