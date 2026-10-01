# 和泉妃爱角色卡

`izumi-hiyori.json` 的字段与后端 `PUT /character` 一致。资料来自[游戏官方角色页](https://imel.co.jp/hamidashi/character)和[动画官方角色介绍](https://hcanime.com/)；性格和对话规则经过整理，属于本项目的扮演设定，不是官方台词。

## 使用

数据库连接配置好并启动后端后，可以在网页「角色设定」中复制各字段；也可以从项目根目录运行：

```powershell
Invoke-RestMethod -Method Put -Uri 'http://127.0.0.1:8000/character' -ContentType 'application/json; charset=utf-8' -InFile '.\rolecards\izumi-hiyori.json'
```

角色卡现在固定对话者为和泉智宏。第一条消息只需交代场景，例如：

> 放学后，我回到家，看到你正整理桌上的录音台本：“今天工作还顺利吗？”

当前长期记忆提取针对用户真实信息，角色扮演时建议在本地 `hiyoribot-backend/config.yaml` 中设置 `auto_extract_memory: false`，避免把剧情台词存成用户事实。角色卡仍需要 PostgreSQL 连接才能通过当前网页/API 保存。
