"""本机模型的显卡串行使用、上下文裁剪与请求选项。"""

import json
from contextlib import contextmanager
from functools import lru_cache
from threading import Lock
from urllib.parse import urlsplit, urlunsplit

import httpx
from tokenizers import Tokenizer

# SSE 生成器可在不同线程继续执行，使用允许跨线程释放的普通锁。
_gpu_lock = Lock()  # ponytail: 单用户、单显卡串行；多 GPU 时再按设备拆锁。


def is_local(config) -> bool:
    return getattr(config, "provider", "deepseek") == "local"


@contextmanager
def gpu_session(config, job=None):
    if not is_local(config):
        if job:
            job.check()
        yield
        return
    while not _gpu_lock.acquire(timeout=0.2):
        if job:
            job.check()
    try:
        if job:
            job.check()
        yield
    finally:
        _gpu_lock.release()


def auxiliary_model(config) -> str:
    return getattr(config, "local_auxiliary_model", "") if is_local(config) else config.model


def transport_options(config) -> dict:
    # 本机请求不经过系统代理，避免把聊天内容交给代理服务。
    return {"http_client": httpx.Client(trust_env=False)} if is_local(config) else {}


def local_options(config, *, auxiliary: bool = False) -> dict:
    if not is_local(config):
        return {}
    return {"temperature": 0 if auxiliary else 0.8,
            "top_p": 0.8, "max_tokens": 2048 if auxiliary else 1024}


@lru_cache(maxsize=2)
def _tokenizer(path: str) -> Tokenizer:
    try:
        return Tokenizer.from_file(path)
    except Exception as exc:
        raise ValueError("本地分词器文件无法加载") from exc


def fit_messages(messages: list[dict], config, tools: list | None = None,
                 *, output_tokens: int = 1024) -> list[dict]:
    if not is_local(config):
        return messages
    # DeepSeek 历史中的思考字段不传给本地非思考模型，工具协议仍保留。
    fitted = [{key: value for key, value in item.items() if key != "reasoning_content"}
              for item in messages]
    tokenizer = _tokenizer(config.local_tokenizer_path)
    budget = config.local_context_tokens - output_tokens - 256

    def tokens() -> int:
        # JSON 与额外余量保守覆盖聊天模板和工具 schema 的开销。
        text = json.dumps({"messages": fitted, "tools": tools or []}, ensure_ascii=False)
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    while tokens() > budget:
        users = [index for index, item in enumerate(fitted) if item["role"] == "user"]
        if len(users) > 1:
            del fitted[users[0]:users[1]]  # 删除整个旧问答，不留下孤立工具结果。
            continue
        references = [index for index, item in enumerate(fitted)
                      if index > 0 and item["role"] == "system"]
        if references:
            del fitted[references[-1]]
            continue
        raise ValueError("本地模型上下文不足，请缩短当前消息或角色设定后重试")
    return fitted


def unload_local_models(config) -> None:
    """只卸载本项目使用的模型；成功返回后才让 TTS 占用显卡。"""
    if not is_local(config):
        return
    url = urlsplit(config.base_url)
    endpoint = urlunsplit((url.scheme, url.netloc, "/api/generate", "", ""))
    with httpx.Client(timeout=60, trust_env=False) as client:
        for model in dict.fromkeys((config.model, auxiliary_model(config))):
            response = client.post(endpoint, json={"model": model, "keep_alive": 0})
            response.raise_for_status()
