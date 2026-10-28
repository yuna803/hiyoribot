# 和泉妃爱角色卡

`izumi-hiyori.json` 的字段与后端 `PUT /character` 一致。当前System Prompt完整保存用户提供的 `E:\unser\Desktop\prompt.txt`，属于本项目采用的人设模板，不把其中补写的经历当作官方考据。

默认日常聊天模式 `knowledge_mode: reference` 会保留人设、互动风格和背景，屏蔽原稿固定输出结构、状态栏/OS/记忆锚点规范与剧情推进指令，使用自然聊天运行约定。旁白和角色OS按需，状态留在面板；程序画像中的好感度是唯一权威分数。全文留在角色卡和数据库中，便于查看、恢复和继续修改。

## 使用

数据库连接配置好并启动后端后，可以在网页「角色设定」中复制各字段；也可以从项目根目录运行：

```powershell
Invoke-RestMethod -Method Put -Uri 'http://127.0.0.1:8000/character' -ContentType 'application/json; charset=utf-8' -InFile '.\rolecards\izumi-hiyori.json'
```

角色卡固定对话者为和泉智宏（哥哥），可以直接聊工作、生活或心情，无需先选择原作章节。也可以可选地交代场景，例如：

> 放学后，我回到家，看到你正整理桌上的录音台本：“今天工作还顺利吗？”

长期记忆自动提取与用户画像是两个独立功能。画像按批次更新并区分现实、角色扮演和推测；当前长期记忆自动提取关闭，手动管理和已有记忆召回仍可用。角色卡通过网页/API保存到PostgreSQL；修改JSON文件后需要重新导入才会生效。
