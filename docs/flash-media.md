# flash.4 识图与表情

独立实现图片含义缓存、人工审核图库和Agent自主选图，借鉴麦麦的流程，没有复制其源码。原始人设、SQLite、50条摘要和无冷却5条跟聊不变。

## 使用

普通群图后台识别；私聊、@、名字触发和跟聊需要图片时，获取与等待合计最多8秒。没有结果就明确未知，不补发。QQ可提供图片URL的image/mface段可处理；仅有face编号的系统表情保留占位。引用图片通过OneBot原消息定位。普通图也能理解，但只有视觉判断为表情包的群图进入候选，候选绝不自动批准。

本人在机器人**私聊**管理：

| 命令 | 作用 |
| --- | --- |
| `/表情 待审核 [页码]` | 每页5张原图、编号与描述 |
| `/表情 通过 编号` | 批准后所有群共享 |
| `/表情 拒绝 编号` | 不允许发送 |
| `/表情 标签 编号 内容` | 修改检索标签 |
| `/表情 删除 编号` | 从可发送图库移除，原文件随缓存到期清理 |
| `/表情 状态` | 图库数量、当日额度、暂停原因 |
| `/识图 重试` | 恢复识图并重试7天内失败/达限材料 |
| 图片附文字 `/表情 收藏` | 本人主动提交到待审核区；随后仍需批准 |

其他私聊的图不进公共图库。管理确认不计问答、不续跟聊。编号可用至少8位无歧义前缀。初始图库为空，不自动下载表情资源包。

Agent使用`search_memes(query, emotion)`最多查5张，`send_meme(meme_id)`仅登记本轮1张；可文字、图文或纯表情。只允许已批准的本地编号，不能指定路径/URL/收件人；同一群或私聊避开最近5张成功表情。网关用base64发送原图/原动图，实际回执才增加使用次数。所有文字分段和图片成功后只续一次跟聊，部分失败或未知不自动重发。

## 配置与限额

在仓库外现有YAML添加：

```yaml
media_enabled: true
media_home: /var/lib/hiyoribot-flash/media
vision_model: deepseek-flash
vision_daily_limit: 100
```

复用`api_key`和`base_url`，环境变量`DEEPSEEK_API_KEY`仍优先。视觉单并发，按北京时间计实际请求尝试，失败也计数；缓存命中不计。达到上限的材料不会跨日自动补跑，需本人手动重试；次日新图正常按新日额度处理。401/402/403暂停整个视觉队列，不影响图片缓存或文字请求。视觉模型没有实际能力或输出不完整时保持未知，不切换其他供应商。

图片最大5MiB、800万像素；动画最多120帧且累计解码像素不超过6000万。GIF/动态WebP抽首中末最多3帧，一次请求；原图保留。网关串行获取，后端串行解码，限制小内存服务器的瞬时占用。只允许结构化图片段提供的QQ图片域名，重定向同样检查；不下载聊天正文URL、不读取QQ容器本地文件路径，不把QQ签名链接交给模型。

描述与原消息分别保存；描述只包含图自身内容。图片文字是待分析资料，不是系统指令，也不直接进入个人画像证据。群消息撤回同时移除自身及引用它的图片关联并沿用原摘要清理规则。群与私聊的来源范围独立；相同图片可复用内容缓存，但不能泄露其他会话原文或身份。

## 接口、备份与验证

现有Bearer认证下新增`media/ingest`、`media/status`、`media/manage`、`media/file/{asset_id}`、`media/receipt`，统一前缀`/integrations/qq/`。图片读取只允许本人审核或本会话合法待发送记录。聊天结果兼容原`reply`，选图后额外返回`attachments`和`delivery_id`。媒体附表自动创建，不修改旧聊天。

原图缓存与未批准候选7天清理，批准图库长期保存。数据库在线备份现在附带同名`.media`目录及SHA256清单；自定义图库位置需带`--media-home`：

```sh
python tools/database_backup.py backup --database /var/lib/hiyoribot-flash/hiyoribot.sqlite3 --folder /opt/backups/hiyoribot --media-home /var/lib/hiyoribot-flash/media
python tools/database_backup.py check --file /opt/backups/hiyoribot/备份文件.sqlite3
```

恢复先停所有写入方，使用既有`restore --confirm-restore 目标数据库文件名`；恢复前自动备份，校验全部图片后先恢复文件再替换数据库，不删除目标多出的安全副本。若当前图库已有文件丢失，安全快照明确记录缺失项，仍允许从完整备份修复；缺少批准图片的快照不能当作完整恢复源。清单缺失或校验失败不恢复。

合成图片和模拟OneBot测试不联网。真实视觉预检会产生API费用，明确执行以下命令，报告只放仓库外：

```sh
python tools/verify_media.py --live-model --config /etc/hiyoribot-flash/config.yaml --report /var/lib/hiyoribot-flash/vision-preflight.json
```

它用临时库检查静态图和三帧GIF，不发送QQ消息、不加入正式图库。部署前将预检当天的实际调用量计入正式`media_daily`，防止额外突破100次。最终需要本人用真实图片检查识别，并审核首批表情；模拟回执不等同于真实QQ发图验收。

上线备份配置、数据库和图库，先更新并验证后端，再重启现有NoneBot加载新网关；QQ容器不重启。回滚需恢复旧配置（旧代码不接受媒体字段），SQLite新增表可保留。

依据：[DeepSeek视觉接口](https://api-docs.deepseek.com/zh-cn/guides/vision/)、[MaiBot源码](https://github.com/MaiM-with-u/MaiBot)。
