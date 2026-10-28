import os

import nonebot
from nonebot.adapters.onebot.v11 import Adapter


# 配置文件和去重数据库放在私有目录，路径可由环境变量覆盖。
env_file = os.environ.get("HIYORI_QQ_ENV_FILE", r"E:\unser\q\hiyoribot_runtime\qq\.env")
nonebot.init(_env_file=env_file, host="127.0.0.1", port=8086, log_level="WARNING")
if not str(getattr(nonebot.get_driver().config, "hiyori_backend_token", "")).strip():
    raise RuntimeError("QQ网关私有配置缺少HIYORI_BACKEND_TOKEN")
nonebot.get_driver().register_adapter(Adapter)
if nonebot.load_plugin("hiyori_chat") is None:
    raise RuntimeError("Hiyori聊天插件加载失败，请检查私有运行日志")

@nonebot.get_app().get("/health")
def health():
    return {"app": "HiyoriBotQQ", "pid": os.getpid(), "bot_ids": list(nonebot.get_bots())}

if __name__ == "__main__":
    nonebot.run()
