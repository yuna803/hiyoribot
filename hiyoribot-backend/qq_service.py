"""已认证的QQ入口：身份、会话和请求去重由程序控制。"""

import hashlib
import json
import os
import re
import secrets
from typing import Literal
from threading import BoundedSemaphore

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

import storage
import user_scope
import profile_service
import summary_service
import group_storage
import media_service

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
    trigger_reason:Literal['direct','name','followup']='direct'
    media_wait_seconds:float=Field(default=8,ge=0,le=8)
    has_images:bool=False

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


def _conversation(body):
    with storage.connect() as c:
        row=c.execute('SELECT conversation_id FROM qq_session WHERE bot_id=? AND user_id=? AND group_id=?',
            (body.bot_id,body.user_id,body.group_id or '')).fetchone()
    if row:
        result=storage.get_conversation(row['conversation_id'])
        if result is None:
            raise HTTPException(409,'会话身份不匹配')
        return result
    result=storage.create_conversation(body.message)
    with storage.connect(write=True) as c:
        c.execute('INSERT INTO qq_session(bot_id,user_id,group_id,conversation_id) VALUES(?,?,?,?)',
            (body.bot_id,body.user_id,body.group_id or '',result['id']))
    return result


def save_reply(c,body,conversation_id,result):
    """即时回复和已获发送许可的跟聊共用同一问答提交逻辑。"""
    portrait=result['recalled_context'].get('profile',{})
    response=dict(reply=result['reply'],conversation_id=conversation_id,
        profile_revision=portrait.get('revision',0),affinity=portrait.get('affinity',50))
    attachments=result.get('attachments',[])
    delivery_id=f'qq:{body.bot_id}:{body.message_id}'
    if attachments:
        response.update(attachments=attachments,delivery_id=delivery_id)
        media_service.register_delivery(c,delivery_id,body.bot_id,body.user_id,body.group_id,attachments)
    recalled={**result['recalled_context'],'trigger_reason':body.trigger_reason}
    uid=c.execute("INSERT INTO message(conversation_id,role,content) VALUES(?,'user',?)",(conversation_id,body.message)).lastrowid
    aid=c.execute("INSERT INTO message(conversation_id,role,content,agent_messages,recalled_context) VALUES(?,'assistant',?,?,?)",
        (conversation_id,media_service.record_text(result['reply'],attachments),storage.json_dump(result['agent_messages']),storage.json_dump(recalled))).lastrowid
    c.execute('INSERT INTO media_turn_link(message_row_id,bot_id,user_id,group_id,external_message_id) VALUES(?,?,?,?,?)',
              (uid,body.bot_id,body.user_id,body.group_id or '',body.message_id))
    c.execute('INSERT INTO completed_turn(conversation_id,user_message_id,assistant_message_id,subject_key) VALUES(?,?,?,?)',
        (conversation_id,uid,aid,user_scope.subject()))
    c.execute('UPDATE conversation SET updated_at=unixepoch() WHERE id=?',(conversation_id,))
    c.execute("UPDATE qq_request SET state='completed',response=? WHERE bot_id=? AND message_id=?",
        (storage.json_dump(response),body.bot_id,body.message_id))
    return response


def chat(body,config,execute_chat,*,defer=None):
    if body.bot_id!=config.qq_bot_id or body.user_id==body.bot_id:
        raise HTTPException(403,'机器人或用户身份不匹配')
    if not _capacity.acquire(blocking=False):
        raise HTTPException(429,'当前请求较多')
    entered=False
    try:
        group_storage.direct(body.bot_id,body.group_id,True,preempt=defer is None)
        entered=True
        lock_key='qq-chat:'+body.user_id if defer is None else f'followup-chat:{body.bot_id}:{body.group_id}'
        with user_scope.activate('qq:'+body.user_id),storage.subject_lock(lock_key) as acquired:
            if not acquired:
                raise HTTPException(429,'该用户上一条消息仍在处理')
            scoped=user_scope.config_for(config).model_copy(update={'qq_group_id':body.group_id or '',
                'qq_nickname':body.nickname,'qq_message_id':body.message_id,'media_wait_seconds':body.media_wait_seconds,'qq_has_images':body.has_images})
            payload=body.model_dump(exclude={'message_id','nickname','trigger_reason','media_wait_seconds','has_images'})
            fingerprint=hashlib.sha256(storage.json_dump(payload).encode()).hexdigest()
            with storage.connect(write=True) as c:
                old=c.execute('SELECT * FROM qq_request WHERE bot_id=? AND message_id=?',(body.bot_id,body.message_id)).fetchone()
                if old:
                    if old['subject_key']!=user_scope.subject() or old['fingerprint']!=fingerprint:
                        raise HTTPException(409,'消息编号与原请求不一致')
                    if old['state']=='completed':
                        return old['response']
                    raise HTTPException(409,'该请求已经处理或中断，请发一条新消息')
                c.execute("INSERT INTO qq_request(bot_id,message_id,subject_key,fingerprint,state) VALUES(?,?,?,?,'running')",
                    (body.bot_id,body.message_id,user_scope.subject(),fingerprint))
            try:
                profile_service.snapshot(scoped)
                conversation=_conversation(body)
                result=execute_chat({'message':body.message,'conversation_id':conversation['id']},scoped)
                with storage.connect(write=True) as c:
                    if defer is not None:
                        # 跟聊草稿尚未获发送许可，不计完整问答或更新画像。
                        defer(c,body,conversation['id'],result)
                        c.execute("UPDATE qq_request SET state='prepared',response=NULL WHERE bot_id=? AND message_id=?",(body.bot_id,body.message_id))
                        response={'reply':result['reply'],'attachments':result.get('attachments',[])}
                    else:
                        response=save_reply(c,body,conversation['id'],result)
            except Exception:
                with storage.connect(write=True) as c:
                    c.execute("UPDATE qq_request SET state='failed' WHERE bot_id=? AND message_id=?",(body.bot_id,body.message_id))
                raise
            if defer is None:
                profile_service.schedule_safely(scoped)
            if not body.group_id:
                summary_service.schedule(conversation['id'],scoped)
            return response
    finally:
        if entered:
            group_storage.direct(body.bot_id,body.group_id,False)
        _capacity.release()
