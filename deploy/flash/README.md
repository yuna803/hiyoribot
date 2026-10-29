# 复用服务器现有NoneBot连接

后端使用独立venv及本目录systemd模板，发布代码放/opt/hiyoribot-flash/releases/<commit>，current指向已验证版本。数据在/var/lib/hiyoribot-flash，配置在/etc/hiyoribot-flash/config.yaml，均不进入仓库。模板假定已有bot系统用户及Python3.11+；SQLite须3.38+。

当服务器已有在线的NoneBot/OneBot时，可将hiyori_flash_bridge.py安装到既有NoneBot工作目录并加载。新增HIYORI_BACKEND_URL=http://127.0.0.1:8000及HIYORI_BACKEND_TOKEN（匹配后端qq_backend_token），用HIYORI_QQ_STATE_DB指定新网关SQLite位置。必要时给现有NoneBot单元增加ReadWritePaths=/var/lib/hiyoribot-flash。只启用一个聊天Agent，关闭旧聊天插件的功能开关，保留其他插件及其数据。

启用前备份现有启动文件、私有环境与systemd配置；后端健康后再加载bridge并重启NoneBot。不要重启QQ容器，也不改变QQ认证或OneBot连接配置。桥接只读健康检查为原NoneBot监听地址下的/hiyori-flash/health，包含连接QQ及在线状态。

回滚时停止NoneBot，恢复本次备份的启动文件、环境与unit drop-in，重启NoneBot，再停止hiyoribot-flash。保留新SQLite和旧数据。发布与回滚前都应确认当前实例和PID，不将服务启动当作真实QQ回复已验收。

flash.2在原SQLite增加跟聊附表，不重置聊天、摘要及画像。更新前用database_backup.py在线备份，停止NoneBot后停止后端，再做最终备份及保存私有配置/current指向。切到新release，启动后端健康后再启动NoneBot；QQ客户端不重启。跟聊窗口会重置，不补发旧稿。回退flash.1时还需恢复旧config.yaml（旧版本不接受新增正则配置字段）；通常保留当前SQLite即可，不必倒退已保存的聊天。若必须恢复数据库，停止所有写入方并先备份当前数据。
