"""长期记忆：用户事实提取、本地向量生成和相关记忆召回。"""

import logging
from functools import lru_cache

from fastembed import TextEmbedding
from openai import OpenAI
from pydantic import BaseModel, Field

import storage
import llm_runtime

logger = logging.getLogger(__name__)
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
MIN_SIMILARITY = 0.55

EXTRACTION_PROMPT = """你负责从用户原话提取适合长期记住的事实或偏好。
只依据用户当前这条消息，不推测，也不要把助手说的话当成用户事实。
只保留今后聊天可能有用的稳定信息；临时任务、闲聊、密码、密钥、证件号和详细地址不要保存。
最多提取 3 条，每条独立完整，不超过 300 字。重要度为 1 到 5。
仅返回 JSON 对象，格式为 {"facts":[{"content":"...","importance":3}]}。
没有值得保存的信息时返回 {"facts":[]}。"""


class ExtractedFact(BaseModel):
    content: str = Field(min_length=3, max_length=300)
    importance: int = Field(ge=1, le=5)


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact] = Field(max_length=3)


@lru_cache(maxsize=1)
def get_embedder() -> TextEmbedding:
    # 首次调用会下载约 90 MB 的中文模型，此后使用本地缓存。
    return TextEmbedding(model_name=EMBEDDING_MODEL)


def embed(text: str) -> list[float]:
    try:
        values = next(get_embedder().embed([text])).tolist()
    except Exception as exc:
        raise RuntimeError("本地 embedding 模型不可用") from exc
    if len(values) != 512:
        raise RuntimeError("embedding 维度与数据库定义不一致")
    return [float(value) for value in values]


def recall(query: str) -> list[dict]:
    if storage.memory_count() == 0:
        return []
    candidates = storage.recall_memories(embed(query), limit=12)
    relevant = [row for row in candidates if float(row["similarity"]) >= MIN_SIMILARITY]
    relevant.sort(
        key=lambda row: float(row["similarity"]) + 0.03 * (row["importance"] - 3),
        reverse=True,
    )
    return relevant[:5]


def extract_and_store(user_text: str, source_message_id: int, config) -> int:
    with llm_runtime.gpu_session(config), OpenAI(api_key=config.api_key, base_url=config.base_url,
                                               timeout=120, max_retries=0,
                                               **llm_runtime.transport_options(config)) as client:
        response = client.chat.completions.create(
            model=llm_runtime.auxiliary_model(config),
            messages=llm_runtime.fit_messages([
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": user_text},
            ], config, output_tokens=2048),
            response_format={"type": "json_object"},
            **llm_runtime.local_options(config, auxiliary=True),
        )
    content = response.choices[0].message.content if response.choices else None
    facts = ExtractedFacts.model_validate_json(content or '{}').facts
    for fact in facts:
        storage.add_memory(
            fact.content.strip(), fact.importance, embed(fact.content), source_message_id
        )
    return len(facts)


def extract_safely(user_text: str, source_message_id: int, config) -> None:
    try:
        extract_and_store(user_text, source_message_id, config)
    except Exception as exc:
        # 后台提取失败不影响已保存的聊天；日志不包含原文和密钥。
        logger.warning("长期记忆提取失败：%s", type(exc).__name__)
