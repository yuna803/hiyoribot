"""单用户生成取消；停止关闭上游流，完整回复进入保存阶段后不再撤销。"""

from dataclasses import dataclass, field
from threading import Event, Lock
import time
from uuid import UUID


class GenerationCancelled(RuntimeError):
    pass


@dataclass
class Generation:
    started: bool = False
    state: str = "running"
    created: float = field(default_factory=time.monotonic)
    cancelled: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    callback: object = None

    def check(self):
        if self.cancelled.is_set():
            raise GenerationCancelled("已停止生成，本轮未保存")

    def bind(self, callback):
        with self.lock:
            self.callback = callback
            cancelled = self.cancelled.is_set()
        if cancelled:
            callback()
            self.check()

    def cancel(self) -> dict:
        with self.lock:
            if self.state in ("saving","completed","failed"):
                return {"accepted": False, "state": self.state}
            self.cancelled.set()
            self.state = "cancelled"
            callback = self.callback
        if callback:
            try:
                callback()
            except Exception:
                pass  # 流可能已经关闭；取消状态仍生效。
        return {"accepted": True, "state": "cancelled"}

    def begin_save(self):
        with self.lock:
            self.check()
            self.state = "saving"

    def finish(self, state: str):
        with self.lock:
            self.state = "cancelled" if self.cancelled.is_set() else state
            self.callback = None


_lock = Lock()
_jobs: dict[UUID, Generation] = {}


def get(identifier: UUID) -> Generation:
    with _lock:
        for key, value in list(_jobs.items()):
            if time.monotonic() - value.created > 600 and value.state != "running":
                del _jobs[key]
        if identifier not in _jobs:
            if len(_jobs) >= 100:
                finished = next((key for key,value in _jobs.items() if value.state not in ("running","saving")), None)
                if finished is None:
                    raise ValueError("同时生成的请求过多")
                del _jobs[finished]
            _jobs[identifier] = Generation()
        return _jobs[identifier]


def register(identifier: UUID) -> Generation:
    job = get(identifier)
    with job.lock:
        if job.started:
            raise ValueError("生成标识已经使用，请重新发送")
        job.started = True
    return job
