# flash运行工具

- start_qq.py：启动SQLite后端和NoneBot网关；--stop只停止网关。
- start_hiyoribot.py：启动/停止SQLite后端，不启动或修改PostgreSQL、Ollama或TTS。
- database_backup.py：SQLite init/status/backup/check/restore，使用在线backup及SHA256检查，恢复前备份并检查后端进程锁。
- verify_group.py：临时SQLite后端测试；--live-model调用当前模型验证自然聊天、工具与摘要，不发送QQ。

私有环境、数据库、备份和模型输出应放仓库外。具体配置和命令见根README。legacy_tests是main版本历史测试，不属于flash测试入口；其他训练和语音脚本留作离线工具，运行时不加载。
