"""任务状态常量与状态机。

状态流转:
    PENDING -> RUNNING -> COMPLETED
                       \\-> FAILED
                       \\-> TIMEOUT
          RUNNING/CANCELLING -> CANCELLED

重启恢复: PENDING/RUNNING/CANCELLING 一律记为 FAILED。
"""

PENDING = "pending"
RUNNING = "running"
CANCELLING = "cancelling"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
TIMEOUT = "timeout"

TERMINAL_STATES = frozenset({COMPLETED, CANCELLED, FAILED, TIMEOUT})
UNFINISHED_STATES = frozenset({PENDING, RUNNING, CANCELLING})
