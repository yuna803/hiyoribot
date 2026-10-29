"""flash: 仅QQ后端，SQLite＋DeepSeek，原始TXT人设。"""
import asyncio
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
import yaml
from fastapi import FastAPI,Header,HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAIError
from pydantic import BaseModel,ConfigDict,Field,field_validator
import agent_service
import chat_service
import group_service
import followup_service
import profile_service
import profile_storage
import qq_service
import storage
import summary_service
import user_scope

HOME=Path.home()/'.local/share/hiyoribot/flash'
class AppConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider:Literal['deepseek']='deepseek'
    api_key:str=''
    model:str='deepseek-flash'
    base_url:str='https://api.deepseek.com'
    sqlite_path:str=str(HOME/'hiyoribot.sqlite3')
    input_budget_chars:int=Field(default=48000,ge=20000,le=96000)
    profile_enabled:bool=True
    profile_home:str=str(HOME/'profiles')
    qq_profile_home:str=str(HOME/'profiles')
    profile_initial_messages:int=Field(default=20,ge=2,le=200)
    profile_batch_messages:int=Field(default=50,ge=2,le=200)
    profile_subject_key:str=Field(default='web:owner',exclude=True)
    qq_enabled:bool=False
    qq_bot_id:str=''
    qq_owner_id:str=''
    qq_backend_token:str=''
    qq_name_trigger_patterns:list[str]=Field(default_factory=lambda:['妃爱'],max_length=8)
    qq_nickname:str=Field(default='',exclude=True)
    qq_group_id:str=Field(default='',exclude=True)
    qq_message_id:str=Field(default='',exclude=True)

    @field_validator('qq_name_trigger_patterns')
    @classmethod
    def valid_name_patterns(cls,patterns):
        for pattern in patterns:
            try:
                if len(pattern)>200 or re.compile(pattern).search('') is not None:
                    raise ValueError('名字正则不能过长或匹配空串')
            except re.error as error:
                raise ValueError('名字正则格式无效') from error
        return patterns

    @field_validator('sqlite_path','profile_home','qq_profile_home')
    @classmethod
    def private_path(cls,value):
        p=Path(value).expanduser()
        if not p.is_absolute() or p.resolve().is_relative_to(storage.ROOT):
            raise ValueError('运行数据必须使用仓库外的绝对路径')
        return str(p)

    @field_validator('profile_initial_messages','profile_batch_messages')
    @classmethod
    def complete_turns(cls,value):
        if value%2:
            raise ValueError('直接互动阈值必须为完整问答的偶数条')
        return value

def load_config():
    path=Path(os.getenv('HIYORI_CONFIG',str(HOME/'config.yaml')))
    data=yaml.safe_load(path.read_text(encoding='utf8')) if path.exists() else {}
    return AppConfig.model_validate(data or {})

def resolve_config():
    config=load_config()
    config=config.model_copy(update={'api_key':os.getenv('DEEPSEEK_API_KEY') or config.api_key})
    if not config.api_key:
        raise HTTPException(503,'未配置DeepSeek API密钥')
    return config

@asynccontextmanager
async def lifespan(app):
    config=load_config()
    storage.configure(config.sqlite_path)
    with storage.process_guard():
        storage.init_db()
        group_service.tick(resolve_config() if config.qq_enabled else config,restart=True)
        with storage.connect(write=True) as c:
            c.execute("UPDATE qq_request SET state='failed' WHERE state='running'")
        if config.profile_enabled:
            for subject in profile_storage.subjects():
                with user_scope.activate(subject):
                    profile_service.schedule_safely(user_scope.config_for(resolve_config()),sync=True)
        with storage.connect() as c:
            sessions=c.execute("SELECT c.id,c.subject_key FROM conversation c JOIN qq_session s ON c.id=s.conversation_id WHERE s.group_id='' ").fetchall()
        for row in sessions:
            with user_scope.activate(row['subject_key']):
                summary_service.schedule(row['id'],resolve_config())
        async def maintain():
            while True:
                await asyncio.sleep(60)
                try:
                    await asyncio.to_thread(group_service.tick,resolve_config())
                except Exception as error:
                    logging.getLogger(__name__).warning('群后台维护失败：%s',type(error).__name__)
        task=asyncio.create_task(maintain())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

app=FastAPI(title='HiyoriBot flash QQ',version='flash.1',lifespan=lifespan)

@app.exception_handler(sqlite3.Error)
async def database_error(request,error):
    return JSONResponse(status_code=503,content={'detail':'SQLite暂不可用，请查看本机后台状态'})

@app.get('/health')
def health():
    with storage.connect() as c:
        c.execute('SELECT 1').fetchone()
    return {'app':'HiyoriBot','version':'flash.2','pid':os.getpid(),'database':True,'storage':'sqlite','provider':'deepseek'}

@app.post('/integrations/qq/chat')
def qq_chat(body:qq_service.QQChatRequest,authorization:str|None=Header(default=None)):
    config=load_config()
    qq_service.authenticate(config,authorization)
    storage.configure(config.sqlite_path)
    try:
        return qq_service.chat(body,resolve_config(),chat_service.direct_reply)
    except (OpenAIError,agent_service.AgentError,ValueError) as error:
        logging.getLogger(__name__).warning('QQ模型处理失败：%s',type(error).__name__)
        raise HTTPException(502,'模型回复未完成，请重新发送消息') from error

def qq_group_config(authorization,bot_id):
    config=load_config()
    qq_service.authenticate(config,authorization)
    group_service.check_bot(bot_id,config)
    storage.configure(config.sqlite_path)
    return config


@app.post('/integrations/qq/group/ingest')
def qq_group_ingest(body:group_service.Ingest,authorization:str|None=Header(default=None)):
    qq_group_config(authorization,body.bot_id)
    return group_service.ingest(body,resolve_config())


@app.post('/integrations/qq/group/toggle')
def qq_group_toggle(body:group_service.Toggle,authorization:str|None=Header(default=None)):
    config=qq_group_config(authorization,body.bot_id)
    return group_service.store.toggle(body,config.qq_owner_id)


@app.post('/integrations/qq/group/recall')
def qq_group_recall(body:group_service.Recall,authorization:str|None=Header(default=None)):
    config=qq_group_config(authorization,body.bot_id)
    return group_service.recall(body,config)


@app.post('/integrations/qq/group/bot-message')
def qq_group_bot_message(body:group_service.BotMessage,authorization:str|None=Header(default=None)):
    qq_group_config(authorization,body.bot_id)
    return group_service.store.bot_message(body)


@app.post('/integrations/qq/group/followup')
def qq_group_followup(body:followup_service.Request,authorization:str|None=Header(default=None)):
    config=qq_group_config(authorization,body.bot_id)
    return followup_service.handle(body,resolve_config())


@app.get('/integrations/qq/group/candidates')
def qq_group_candidates(bot_id:str,authorization:str|None=Header(default=None)):
    qq_group_config(authorization,bot_id)
    return group_service.store.candidates(bot_id)


@app.post('/integrations/qq/group/claim')
def qq_group_claim(body:group_service.Claim,authorization:str|None=Header(default=None)):
    qq_group_config(authorization,body.bot_id)
    return group_service.claim(body,resolve_config())


@app.post('/integrations/qq/group/receipt')
def qq_group_receipt(body:group_service.Receipt,authorization:str|None=Header(default=None)):
    qq_group_config(authorization,body.bot_id)
    return group_service.store.receipt(body)


@app.post('/integrations/qq/group/startup')
def qq_group_startup(body:group_service.BotKey,authorization:str|None=Header(default=None)):
    qq_group_config(authorization,body.bot_id)
    return group_service.store.startup(body.bot_id)
