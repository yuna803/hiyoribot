"""配音任务只排队一次；页面轮询阶段，服务重启后可以安全重试并复用缓存。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time
from uuid import uuid4

from fastapi import HTTPException

import tts_service

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hiyori-tts")
_lock = Lock()
_jobs: dict[str, dict] = {}


def create(text: str, config) -> dict:
    identifier = uuid4().hex
    with _lock:
        for key, value in list(_jobs.items()):
            if value["stage"] in ("ready","failed") and time.monotonic() - value["created"] > 3600:
                del _jobs[key]
        if sum(value["stage"] not in ("ready","failed") for value in _jobs.values()) >= 8:
            raise HTTPException(status_code=429, detail="配音队列已满，请等待当前任务完成")
        _jobs[identifier] = {"stage": "queued", "created": time.monotonic()}

    def update(stage: str):
        with _lock:
            _jobs[identifier]["stage"] = stage

    def worker():
        try:
            result = tts_service.create_speech(text,config,progress=update)
            with _lock:
                _jobs[identifier].update(stage="ready",result=result,finished=time.monotonic())
        except Exception as error:
            detail = error.detail if isinstance(error,HTTPException) else "配音任务失败，请查看本机日志后重试"
            with _lock:
                _jobs[identifier].update(stage="failed",error=detail,finished=time.monotonic())
    _executor.submit(worker)
    return {"id": identifier, "stage": "queued"}


def get(identifier: str) -> dict:
    with _lock:
        row = _jobs.get(identifier)
        if row is None:
            raise HTTPException(status_code=404, detail="配音任务已失效，请重新生成")
        return {"id":identifier,"stage":row["stage"],"elapsed_seconds":round(row.get("finished",time.monotonic())-row["created"],1),
                "result":row.get("result"),"error":row.get("error")}
