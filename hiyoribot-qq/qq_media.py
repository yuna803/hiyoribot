"""OneBot图片获取与管理入口。只从结构化图片段取地址，不下载用户正文URL。"""
import asyncio
import base64
import time
from urllib.parse import urlparse,urljoin

import httpx
from nonebot.adapters.onebot.v11 import GroupMessageEvent,MessageSegment

MAX_BYTES=5*1024*1024
_tasks={}
_downloads=asyncio.Semaphore(1)


def picture_segments(message):
    return [s for s in message if s.type in ('image','mface')]


def has_images(event):
    reply=getattr(event,'reply',None)
    return bool(picture_segments(event.original_message) or
                (reply and picture_segments(getattr(reply,'message',[]))) or
                any(s.type=='reply' for s in event.original_message))


def key(bot,event):
    return (str(bot.self_id),str(getattr(event,'group_id','')),str(event.user_id),str(event.message_id))


def scope(bot,event):
    return {'bot_id':str(bot.self_id),'user_id':str(event.user_id),'group_id':str(getattr(event,'group_id',''))}


def permitted_url(url):
    p=urlparse(url)
    host=(p.hostname or '').casefold()
    return bool(p.scheme in ('http','https') and p.port in (None,80,443) and not p.username and not p.password and
        any(host==d or host.endswith('.'+d) for d in ('qq.com','qq.com.cn','qpic.cn')))


async def download(url):
    async with httpx.AsyncClient(timeout=httpx.Timeout(8,connect=4),trust_env=False) as c:
        for _ in range(4):
            if not permitted_url(url):
                raise ValueError('图片源地址不受支持')
            async with c.stream('GET',url) as r:
                if r.is_redirect:
                    url=urljoin(url,r.headers.get('location',''))
                    continue
                r.raise_for_status()
                if int(r.headers.get('content-length','0'))>MAX_BYTES:
                    raise ValueError('图片太大')
                data=bytearray()
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data)>MAX_BYTES:
                        raise ValueError('图片太大')
                return bytes(data)
    raise ValueError('图片重定向过多')


async def image_bytes(bot,segment):
    data=segment.data
    url=data.get('url') or data.get('file_url')
    if url:
        try:
            return await download(str(url))
        except (ValueError,httpx.HTTPError):
            pass
    # URL可能过期：使用OneBot原始文件标识刷新一次；不读取协议端的本地路径。
    file=data.get('file')
    if file:
        result=await bot.get_image(file=str(file))
        if result.get('url'):
            return await download(str(result['url']))
    raise ValueError('无法获取图片')


async def collect(bot,event,api):
    async with _downloads:
        await _collect(bot,event,api)


async def _collect(bot,event,api):
    images=[(s,False) for s in picture_segments(event.original_message)]
    reply=getattr(event,'reply',None)
    quoted_id=str(reply.message_id) if reply and getattr(reply,'message_id',None) is not None else None
    quoted=picture_segments(getattr(reply,'message',[])) if reply else []
    if not quoted:
        quoted_id=quoted_id or next((s.data.get('id') for s in event.original_message if s.type=='reply'),None)
        if quoted_id is not None:
            try:
                result=await bot.get_msg(message_id=int(quoted_id))
                from nonebot.adapters.onebot.v11 import Message
                raw=result.get('message',[])
                # get_msg在不同适配器版本中可能返回CQ字符串、Message或字典段列表。
                if isinstance(raw,list):
                    raw=Message([MessageSegment(s['type'],s.get('data',{})) if isinstance(s,dict) else s for s in raw])
                quoted=picture_segments(Message(raw))
            except Exception:
                quoted=[]
    images.extend((s,True) for s in quoted)
    text=''.join(str(s.data.get('text','')) for s in event.original_message if s.type=='text').strip()
    for slot,(segment,referenced) in enumerate(images):
        try:
            raw=await image_bytes(bot,segment)
            await api('POST','media/ingest',payload={**scope(bot,event),'message_id':str(event.message_id),
                'slot':slot,'data':base64.b64encode(raw).decode(),'referenced':referenced,
                'reference_message_id':str(quoted_id) if referenced and quoted_id is not None else None,
                'manual':not isinstance(event,GroupMessageEvent) and text=='/表情 收藏'},timeout=30)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 图不可获取不影响文字回复，也不把临时URL或凭据写到日志/群聊。
            continue


def start(bot,event,api):
    if not has_images(event):
        return
    k=key(bot,event)
    if k not in _tasks:
        task=asyncio.create_task(collect(bot,event,api))
        _tasks[k]=task
        task.add_done_callback(lambda done:_tasks.pop(k,None) if _tasks.get(k) is done else None)


async def wait(bot,event,seconds=8):
    started=time.monotonic()
    task=_tasks.get(key(bot,event))
    if task:
        try:
            await asyncio.wait_for(asyncio.shield(task),timeout=min(4,seconds))
        except Exception:
            pass
    return max(0,seconds-(time.monotonic()-started))


async def get_file(api_url,token,asset_id,params):
    if not isinstance(asset_id,str) or len(asset_id)!=64 or any(c not in '0123456789abcdef' for c in asset_id):
        raise ValueError('无效图片编号')
    async with httpx.AsyncClient(timeout=15,trust_env=False) as c:
        async with c.stream('GET',api_url+'/integrations/qq/media/file/'+asset_id,
                            headers={'Authorization':'Bearer '+token},params=params) as r:
            r.raise_for_status()
            data=bytearray()
            async for chunk in r.aiter_bytes():
                data.extend(chunk)
                if len(data)>MAX_BYTES:
                    raise ValueError('图片太大')
            return bytes(data)


async def management(bot,event,api,backend_url,token):
    text=''.join(str(s.data.get('text','')) for s in event.original_message if s.type=='text').strip()
    if isinstance(event,GroupMessageEvent) or not (text.startswith('/表情') or text.startswith('/识图')):
        return False
    start(bot,event,api)
    await wait(bot,event)
    try:
        result=await api('POST','media/manage',payload={**scope(bot,event),'command':text})
        await bot.send(event,MessageSegment.text(result['reply']))
        for item in result.get('items',[]):
            await bot.send(event,MessageSegment.text('编号：'+item['asset_id']+'\n'+item['description'][:600]))
            raw=await get_file(backend_url,token,item['asset_id'],{**scope(bot,event),'review':True})
            await bot.send(event,MessageSegment.image('base64://'+base64.b64encode(raw).decode()))
    except Exception:
        await bot.send(event,MessageSegment.text('表情管理暂不可用；请确认使用本人账号私聊，图片可能已过期。'))
    return True


async def disconnect(bot_id):
    tasks=[t for k,t in list(_tasks.items()) if k[0]==bot_id]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks,return_exceptions=True)
