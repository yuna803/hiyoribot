"""已认证的QQ入口：身份、会话和请求去重由程序控制。"""

import hashlib
import json
import os
import re
import secrets
from threading import BoundedSemaphore

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

import storage
import user_scope

# ponytail: 本机最多同时处理4个QQ模型请求；需要扩容时再改有界任务队列。
_capacity = BoundedSemaphore(4)
QQ_ID = r"^[1-9][0-9]{4,11}$"


class QQChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    bot_id: str = Field(pattern=QQ_ID)
    user_id: str = Field(pattern=QQ_ID)
    group_id: str | None = Field(default=None, pattern=QQ_ID)
    message_id: str = Field(min_length=1, max_length=80)
    nickname: str = Field(default="", max_length=80)
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message_id")
    @classmethod
    def valid_message_id(cls, value):
        if not re.fullmatch(r'-?[0-9]{1,20}',value):
            raise ValueError("无效消息编号")
        return value


def authenticate(config, authorization: str | None) -> None:
    expected = os.getenv("HIYORI_QQ_BACKEND_TOKEN") or config.qq_backend_token
    if not config.qq_enabled or not expected or not config.qq_bot_id:
        raise HTTPException(503, "QQ入口尚未启用")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not authorization or not authorization.startswith("Bearer ") or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(403, "QQ网关认证失败")


def _conversation(body: QQChatRequest) -> dict:
    with storage.connect() as conn:
        row = conn.execute("SELECT conversation_id FROM qq_session WHERE bot_id=%s AND user_id=%s AND group_id=%s",
                           (body.bot_id, body.user_id, body.group_id or "")).fetchone()
    if row:
        conversation = storage.get_conversation(row["conversation_id"])
        if conversation is None:
            raise HTTPException(409, "QQ会话身份不匹配")
        return conversation
    conversation = storage.create_conversation(body.message)
    with storage.connect() as conn:
        conn.execute("INSERT INTO qq_session(bot_id,user_id,group_id,conversation_id) VALUES(%s,%s,%s,%s)",
                     (body.bot_id, body.user_id, body.group_id or "", conversation["id"]))
    return conversation


def chat(body: QQChatRequest, config, execute_chat) -> dict:
    if body.bot_id != config.qq_bot_id or body.user_id == body.bot_id:
        raise HTTPException(403, "机器人或用户身份不匹配")
    if not _capacity.acquire(blocking=False):
        raise HTTPException(429, "当前请求较多，请稍后重新发送")
    import group_storage
    entered=False
    try:
        group_storage.direct(body.bot_id,body.group_id,True)
        entered=True
        with user_scope.activate("qq:" + body.user_id):
            scoped = user_scope.config_for(config)
            scoped = scoped.model_copy(update={"qq_nickname": body.nickname, "qq_group_id": body.group_id or ""})
            payload = body.model_dump(exclude={"message_id", "nickname"})
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            # 会话级锁覆盖整个模型调用，使同一人的多平台/多群请求保持顺序。
            with storage.connect() as guard:
                lock_key = "qq-chat:" + user_scope.subject()
                acquired = guard.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,901012)) AS ok",
                                         (lock_key,)).fetchone()["ok"]
                guard.commit()
                if not acquired:
                    raise HTTPException(429, "这个用户上一条消息仍在处理")
                try:
                    old = guard.execute("SELECT * FROM qq_request WHERE bot_id=%s AND message_id=%s",
                                        (body.bot_id, body.message_id)).fetchone()
                    if old:
                        if old["subject_key"] != user_scope.subject() or old["fingerprint"] != fingerprint:
                            raise HTTPException(409, "消息编号与原始请求不一致")
                        if old["state"] == "completed":
                            return old["response"]
                        raise HTTPException(409, "该消息已经处理或中断，请重新发送一条消息")
                    guard.execute("""INSERT INTO qq_request(bot_id,message_id,subject_key,fingerprint,state)
                                     VALUES(%s,%s,%s,%s,'running')""",
                                  (body.bot_id, body.message_id, user_scope.subject(), fingerprint))
                    guard.commit()
                    try:
                        # 在第一条完整问答之前创建画像起算点，不能漏掉首轮。
                        import profile_service
                        profile_service.snapshot(scoped)
                        conversation = _conversation(body)
                        result = execute_chat({"message": body.message, "conversation_id": conversation["id"],
                                               "thinking": False, "tools_enabled": True}, scoped)
                        portrait = result.recalled_context.get("profile", {})
                        response = {"reply": result.reply, "conversation_id": str(result.conversation_id),
                                    "profile_revision": portrait.get("revision", 0),
                                    "affinity": portrait.get("affinity", 50)}
                        guard.execute("""UPDATE qq_request SET state='completed',response=%s
                                         WHERE bot_id=%s AND message_id=%s""",
                                      (Jsonb(response), body.bot_id, body.message_id))
                        guard.commit()
                        return response
                    except Exception:
                        guard.rollback()
                        guard.execute("UPDATE qq_request SET state='failed' WHERE bot_id=%s AND message_id=%s",
                                      (body.bot_id, body.message_id))
                        guard.commit()
                        raise
                finally:
                    guard.execute("SELECT pg_advisory_unlock(hashtextextended(%s,901012))", (lock_key,))
    finally:
        if entered:
            group_storage.direct(body.bot_id,body.group_id,False)
        _capacity.release()
