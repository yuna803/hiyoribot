"""复用已有NoneBot/OneBot连接加载flash网关，不启动第二个QQ客户端。"""
import os
import nonebot
from nonebot import get_bots,load_plugin

source=os.environ.get('HIYORI_FLASH_GATEWAY_SOURCE','/opt/hiyoribot-flash/current/hiyoribot-qq/hiyori_chat.py')
if load_plugin(source) is None:
    raise RuntimeError('flash网关未能加载')

@nonebot.get_app().get('/hiyori-flash/health')
async def flash_health():
    bots=get_bots()
    result={'app':'HiyoriBotFlashBridge','bot_ids':list(bots),'pid':os.getpid(),'qq_online':False}
    if bots:
        try:
            bot=next(iter(bots.values()))
            status=await bot.get_status()
            result['qq_online']=bool(status.get('online'))
        except Exception:
            result['qq_online']=False
    return result
