import asyncio
import os
import sqlite3
import time
import weakref
from contextlib import closing
from pathlib import Path

import httpx
from nonebot import get_bots, get_driver, on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, GroupRecallNoticeEvent, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.log import logger

config = get_driver().config
backend_url = str(getattr(config, "hiyori_backend_url", "http://127.0.0.1:8000")).rstrip("/")
backend_token = str(getattr(config, "hiyori_backend_token", ""))
env_file = Path(os.environ.get("HIYORI_QQ_ENV_FILE", str(Path.home()/".local/share/hiyoribot/flash/qq.env")))
state_path = Path(os.environ.get("HIYORI_QQ_STATE_DB", str(env_file.parent / "hiyori-qq.sqlite3")))
locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
matcher = on_message(priority=10, block=False)
notice_matcher = on_notice(priority=10, block=False)
started_bots = set()
poll_tasks = {}
active_groups = set()
active_candidates = set()
background_tasks = set()
group_versions = {}


def bump_group(bot_id, group_id):
    key = (bot_id, group_id)
    group_versions[key] = group_versions.get(key, 0) + 1


def state_db():
    state_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state_path)
    db.execute("CREATE TABLE IF NOT EXISTS seen (bot_id TEXT, message_id TEXT, PRIMARY KEY (bot_id, message_id))")
    db.execute("CREATE TABLE IF NOT EXISTS sent (bot_id TEXT, group_id TEXT, message_id TEXT, PRIMARY KEY (bot_id, group_id, message_id))")
    return db


def claim_message(bot_id: str, message_id: str) -> bool:
    with closing(state_db()) as db, db:
        return db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (bot_id, message_id)).rowcount == 1


def sent_message(bot_id: str, group_id: str, message_id: str, *, record=False) -> bool:
    with closing(state_db()) as db, db:
        if record:
            db.execute("INSERT OR IGNORE INTO sent VALUES (?, ?, ?)", (bot_id, group_id, message_id))
            return True
        return db.execute("SELECT 1 FROM sent WHERE bot_id=? AND group_id=? AND message_id=?", (bot_id, group_id, message_id)).fetchone() is not None


def quote_id(event):
    for segment in event.original_message:
        if segment.type == "reply" and segment.data.get("id") is not None:
            return str(segment.data["id"])
    return None


def is_direct(event, bot_id):
    return any(s.type == "at" and str(s.data.get("qq")) == bot_id for s in event.original_message) or (
        (quoted := quote_id(event)) is not None and sent_message(bot_id, str(event.group_id), quoted)
    )


def make_payload(event: MessageEvent, bot_id: str):
    if str(event.user_id) == bot_id or not isinstance(event, (PrivateMessageEvent, GroupMessageEvent)):
        return None
    if isinstance(event, GroupMessageEvent) and not is_direct(event, bot_id):
        return None
    message = "".join(str(s.data.get("text", "")) for s in event.original_message if s.type == "text").strip()
    if not message and isinstance(event, GroupMessageEvent):
        message = "".join(f"[{s.type}]" for s in event.original_message if s.type not in ("text", "at", "reply"))
    if not message:
        return None
    return {"bot_id": bot_id, "user_id": str(event.user_id),
            "group_id": str(event.group_id) if isinstance(event, GroupMessageEvent) else None,
            "message_id": str(event.message_id), "nickname": (event.sender.card or event.sender.nickname or "")[:80],
            "message": message}


async def api(method, path, *, payload=None, params=None, timeout=30.0):
    if not backend_token:
        raise RuntimeError("backend token missing")
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), trust_env=False) as client:
        headers = {"Authorization": f"Bearer {backend_token}"}
        if method == "POST":
            response = await client.post(backend_url + "/integrations/qq/" + path, headers=headers, json=payload)
        else:
            response = await client.get(backend_url + "/integrations/qq/" + path, headers=headers, params=params)
        response.raise_for_status()
        return response.json()


def message_id_from_ack(ack):
    if isinstance(ack, dict) and ack.get("message_id") is not None:
        return str(ack["message_id"])
    return None


async def register_sent(bot_id, group_id, ack, message):
    message_id = message_id_from_ack(ack)
    if message_id is None:
        return
    sent_message(bot_id, group_id, message_id, record=True)
    try:
        await api("POST", "group/bot-message", payload={"bot_id": bot_id, "group_id": group_id,
                  "message_id": message_id, "message": message})
    except Exception:
        logger.warning("QQ群消息记录同步失败")


async def send_reply(bot, event, text):
    ack = await bot.send(event, MessageSegment.text(text))
    if isinstance(event, GroupMessageEvent):
        await register_sent(str(bot.self_id), str(event.group_id), ack, text)


def group_payload(event, bot_id, direct):
    segments = event.original_message
    text = "".join(str(s.data.get("text", "")) for s in segments if s.type == "text")
    message = "".join(str(s.data.get("text", "")) if s.type == "text" else f"[{s.type}]" for s in segments)
    return {"bot_id": bot_id, "user_id": str(event.user_id), "group_id": str(event.group_id),
            "message_id": str(event.message_id), "nickname": (event.sender.card or event.sender.nickname or "")[:80],
            "message": message, "text": text, "quoted_message_id": quote_id(event), "direct": direct,
            "sent_at": int(event.time)}


async def process_message(bot: Bot, event: MessageEvent):
    bot_id = str(bot.self_id)
    if str(event.user_id) == bot_id:
        return
    if isinstance(event, GroupMessageEvent):
        bump_group(bot_id, str(event.group_id))
        text = "".join(str(s.data.get("text", "")) for s in event.original_message if s.type == "text")
        if text in ("/旁听 开启", "/旁听 关闭") and all(s.type == "text" for s in event.original_message):
            try:
                await api("POST", "group/toggle", payload={"bot_id": bot_id, "group_id": str(event.group_id),
                          "user_id": str(event.user_id), "enabled": text.endswith("开启")})
                await send_reply(bot, event, "旁听已开启。" if text.endswith("开启") else "旁听已关闭。")
            except Exception:
                logger.warning("QQ群旁听设置失败")
            return
        direct = is_direct(event, bot_id)
        try:
            await api("POST", "group/ingest", payload=group_payload(event, bot_id, direct))
        except Exception:
            logger.warning("QQ群消息采集失败")
    payload = make_payload(event, bot_id)
    if payload is None:
        return
    key = (bot_id, payload["user_id"])
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    async with lock:
        try:
            if not backend_token:
                logger.warning("QQ 网关未配置后端令牌")
                return
            if not claim_message(bot_id, payload["message_id"]):
                return
            if len(payload["message"]) > 4000:
                await send_reply(bot, event, "消息太长了，请控制在 4000 字以内再发送。")
                return
            async with httpx.AsyncClient(timeout=httpx.Timeout(420.0, connect=10.0), trust_env=False) as client:
                response = await client.post(backend_url + "/integrations/qq/chat", headers={"Authorization": f"Bearer {backend_token}"}, json=payload)
                if response.status_code == 429:
                    await send_reply(bot, event, "现在有点忙，请稍后重新发送消息。")
                    return
                response.raise_for_status()
                reply = response.json().get("reply")
                if not isinstance(reply, str) or not reply.strip():
                    raise ValueError("invalid reply")
            reply = reply[:12000]
            for start in range(0, len(reply), 2000):
                await send_reply(bot, event, reply[start:start + 2000])
        except Exception:
            logger.warning("QQ 消息处理失败，请检查网关及后端状态")
            try:
                await send_reply(bot, event, "处理消息时出了点问题，请稍后重新发送。")
            except Exception:
                logger.warning("QQ 消息发送失败")


async def send_candidate(bot, candidate_id, group_id):
    bot_id = str(bot.self_id)
    state, message_id = "failed", None
    version = group_versions.get((bot_id, group_id), 0)
    try:
        result = await api("POST", "group/claim", payload={"bot_id": bot_id, "candidate_id": candidate_id}, timeout=420.0)
        if not result.get("send"):
            return
        target = str(result.get("group_id", ""))
        reply = result.get("reply")
        if target != group_id or not isinstance(reply, str) or not reply.strip():
            raise ValueError("invalid candidate")
        if get_bots().get(bot_id) is not bot:
            raise RuntimeError("bot offline")
        member = await bot.get_group_member_info(group_id=int(group_id), user_id=int(bot_id), no_cache=True)
        if member.get("shut_up_timestamp") is None:
            # 个别实现的单成员接口缺字段时，使用成员列表中的明确禁言时间。
            members = await bot.get_group_member_list(group_id=int(group_id), no_cache=True)
            member = next((m for m in members if str(m.get("user_id")) == bot_id), {})
            if member.get("shut_up_timestamp") is None:
                raise RuntimeError("mute status unavailable")
        if int(member.get("shut_up_timestamp") or 0) > int(time.time()):
            raise RuntimeError("bot muted")
        if group_versions.get((bot_id, group_id), 0) != version:
            raise RuntimeError("group changed after claim")
        message = MessageSegment.text(reply)
        if result.get("quote_message_id") is not None:
            message = MessageSegment.reply(int(result["quote_message_id"])) + message
        try:
            ack = await asyncio.wait_for(bot.send_group_msg(group_id=int(group_id), message=message), timeout=15)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            state = "uncertain"
            return
        state = "sent"
        message_id = message_id_from_ack(ack)
        await register_sent(bot_id, group_id, ack, reply)
    except Exception:
        logger.warning("QQ群候选消息发送失败")
    finally:
        # claim 明确不发送时不产生回执；其他情况只上报一次，不重试发送。
        if 'result' not in locals() or result.get("send"):
            try:
                receipt = {"bot_id": bot_id, "candidate_id": candidate_id, "state": state}
                if message_id is not None:
                    receipt["message_id"] = message_id
                await api("POST", "group/receipt", payload=receipt)
            except Exception:
                logger.warning("QQ群候选消息回执失败")
        active_groups.discard((bot_id, group_id))
        active_candidates.discard((bot_id, candidate_id))


async def poll_candidates(bot):
    bot_id = str(bot.self_id)
    try:
        while get_bots().get(bot_id) is bot:
            try:
                if bot_id not in started_bots:
                    await api("POST", "group/startup", payload={"bot_id": bot_id})
                    started_bots.add(bot_id)
                result = await api("GET", "group/candidates", params={"bot_id": bot_id})
                for item in result.get("items", []):
                    candidate_id, group_id = str(item["id"]), str(item["group_id"])
                    if len(background_tasks) >= 2:
                        break
                    if (bot_id, group_id) in active_groups or (bot_id, candidate_id) in active_candidates:
                        continue
                    active_groups.add((bot_id, group_id))
                    active_candidates.add((bot_id, candidate_id))
                    task = asyncio.create_task(send_candidate(bot, candidate_id, group_id))
                    background_tasks.add(task)
                    task.add_done_callback(background_tasks.discard)
            except Exception:
                logger.warning("QQ群候选消息轮询失败")
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("QQ群候选消息启动失败")


@get_driver().on_bot_connect
async def connected(bot: Bot):
    bot_id = str(bot.self_id)
    old = poll_tasks.get(bot_id)
    if old is None or old.done():
        poll_tasks[bot_id] = asyncio.create_task(poll_candidates(bot))


@get_driver().on_bot_disconnect
async def disconnected(bot: Bot):
    task = poll_tasks.pop(str(bot.self_id), None)
    if task is not None:
        task.cancel()


@notice_matcher.handle()
async def handle_notice(bot: Bot, event: GroupRecallNoticeEvent):
    bump_group(str(bot.self_id), str(event.group_id))
    try:
        await api("POST", "group/recall", payload={"bot_id": str(bot.self_id), "group_id": str(event.group_id),
                  "message_id": str(event.message_id)})
    except Exception:
        logger.warning("QQ群撤回同步失败")


@matcher.handle()
async def handle_message(bot: Bot, event: MessageEvent):
    await process_message(bot, event)
