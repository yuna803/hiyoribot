# 复用服务器现有NoneBot连接

后端使用独立venv及本目录systemd模板，发布代码放/opt/hiyoribot-flash/releases/<commit>，current指向已验证版本。数据在/var/lib/hiyoribot-flash，配置在/etc/hiyoribot-flash/config.yaml，均不进入仓库。模板假定已有bot系统用户及Python3.11+；SQLite须3.38+。

当服务器已有在线的NoneBot/OneBot时，可将hiyori_flash_bridge.py安装到既有NoneBot工作目录并加载。新增HIYORI_BACKEND_URL=http://127.0.0.1:8000及HIYORI_BACKEND_TOKEN（匹配后端qq_backend_token），用HIYORI_QQ_STATE_DB指定新网关SQLite位置。必要时给现有NoneBot单元增加ReadWritePaths=/var/lib/hiyoribot-flash。只启用一个聊天Agent，关闭旧聊天插件的功能开关，保留其他插件及其数据。

启用前备份现有启动文件、私有环境与systemd配置；后端健康后再加载bridge并重启NoneBot。不要重启QQ容器，也不改变QQ认证或OneBot连接配置。桥接只读健康检查为原NoneBot监听地址下的/hiyori-flash/health，包含连接QQ及在线状态。

回滚时停止NoneBot，恢复本次备份的启动文件、环境与unit drop-in，重启NoneBot，再停止hiyoribot-flash。保留新SQLite和旧数据。发布与回滚前都应确认当前实例和PID，不将服务启动当作真实QQ回复已验收。
