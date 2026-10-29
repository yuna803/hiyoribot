# HiyoriBot · flash

保留用户原始TXT人设的纯QQ聊天Agent：DeepSeek API、NoneBot／NapCat、Python内置SQLite。没有网页聊天和TTS运行入口，不安装PostgreSQL、向量索引或embedding模型。

## 对话流程

```text
验证身份并去重 → 私聊/@/真实引用/正文名字立即响应，普通群消息进入旁听
→ 读取本会话累计摘要和允许使用的画像
→ 原始人设＋QQ规则＋摘要＋未压缩完整原文＋当前消息
→ 模型选择工具 → 工具结果交回模型 → 正式文字回复
→ 保存完整问答／发送回执 → 后台按批次更新摘要与画像
```

原始人设完整保存在 `rolecards/izumi-hiyori.json`。请求时只屏蔽原稿固定输出结构、状态栏、内心OS、记忆锚点和强制剧情推进章节；追加QQ规则，不预设地点、不编造当前动作，不输出旁白或OS。只有配置绑定的本人QQ是哥哥，其他成员保持独立身份。

每群50条成员消息整理一次，机器人回复不计数，图片/语音仅为类型占位。可沉默，有必要才运行角色Agent主动接话；同群至少5分钟、滚动一小时最多3次，失败或不确定发送尝试也占冷却。新消息触发一次复核，复核期间再次来消息或收到直接互动就弃稿；重启不补发旧稿。本人可在群发 `/旁听 开启`、`/旁听 关闭`。

群正文默认正则匹配“妃爱”直接回复，配置项为`qq_name_trigger_patterns: ['妃爱']`；不查昵称、引用正文或转发内容，非法/匹配空串的正则启动报错。每次角色聊天或主动插话全部发送成功并有消息编号后，开启本群后续5条成员消息的跟聊窗口；图片/语音也消耗一条，仅文字做相关性判断。所有群友均可接话，每次成功回复重新计5条，按用户选择不设时间上限。跟聊没有固定冷却，回复后立即接话也可以进入判断；不相关或没有文字时仍保持沉默。

窗口内使用一次简短DeepSeek请求判断是否在接她的话，通过后复用角色Agent及工具循环；只聊到相似主题、明显在和别人说话或判断失败就沉默。相关性判断会额外产生API费用。新直接互动优先，一次复核后仍有消息变化则弃稿。窗口/直接互动进行时50条摘要照常更新，但不额外插话、不延后补发。暂停旁听同时关闭自动跟聊，名字/@/引用仍可用；恢复不追旧话题。重启、断线重连清空窗口，撤回锚定回复或依据会使相关草稿失效。

判断同时提供她上一条回复对应的用户原话与发言者，原对话者的简短反问不会仅因没叫名字或中间有插话/图片而被认作在问别人。调侃是否相关与角色愿不愿意接受分开判断；明确@或引用其他成员时不自动跟聊，文字中明确点名其他人的对话也应保持沉默。

草稿获一次性发送许可后才保存完整问答并计入画像；尚未判定的发言暂缓旁听画像游标，沉默后恢复普通旁听资格，避免重复或漏记。已有即时回复接口兼容，新`POST /integrations/qq/group/followup`使用认证及已采集消息编号/窗口版本，`prepare → claim → receipt`分离判断、发送许可和回执；重复请求不重发。数据库仅增加窗口/任务附表，旧聊天及摘要不重置，模型请求期间不持写事务。

私聊每50条成功保存的用户＋助手消息（25轮完整问答）更新摘要。群摘要与旁听共用50条批次。每批摘要最多600字、累计摘要最多1200字；上下文放累计摘要及摘要截止点之后全部尚未压缩的原文，首次未满50条也完整带入，不限最近24条。仅已被摘要覆盖的旧原文按预算补充；未压缩材料超预算明确失败，不静默删掉中间记录。长摘要输入完整分块处理。

画像与摘要独立：直接互动首次20条、以后每50条；旁听首次10条有效发言、以后25条，在群观察批次完成后检查。好感度初始50、范围-100～100，直接互动单批增减-5～5；旁听不改变身份、相处感受和分值。群回复共享本群多人上下文，只读取本群公开证据支持的观察。

只读工具：真实时间、联网搜索、当前私聊／群历史关键词查询、批次摘要关键词查询、用户文字事实／偏好查询。保留最多4轮工具、总共8次。执行记录随回复存入数据库，不把内部思考或工具日志发到QQ。

## 本机／服务器准备

需要Python 3.11+和已经登录、提供OneBot 11反向WebSocket的NapCat。本分支只提供应用部署步骤，NapCat/QQ本身独立安装及登录。

```powershell
python -m venv E:\unser\q\hiyoribot_flash\env
E:\unser\q\hiyoribot_flash\env\Scripts\python.exe -m pip install -r hiyoribot-backend/requirements.txt -r hiyoribot-qq/requirements.txt
$env:HIYORI_FLASH_HOME='E:\unser\q\hiyoribot_flash'
$env:HIYORI_CONFIG="$env:HIYORI_FLASH_HOME\config.yaml"
$env:HIYORI_QQ_ENV_FILE="$env:HIYORI_FLASH_HOME\qq.env"
```

把后端config.example.yaml复制到上面的私有config.yaml，填写DeepSeek密钥、bot/owner QQ、独立后端令牌，启用qq_enabled；SQLite和profiles必须指向仓库外绝对路径。环境变量DEEPSEEK_API_KEY覆盖YAML中的api_key。把hiyoribot-qq/.env.example复制到qq.env，填写ONEBOT_ACCESS_TOKEN及匹配的HIYORI_BACKEND_TOKEN。

NapCat反向WS地址：`ws://127.0.0.1:8086/onebot/v11/ws`，令牌匹配ONEBOT_ACCESS_TOKEN。后端默认127.0.0.1:8000、网关127.0.0.1:8086，不提供公开网页管理面板。

```powershell
E:\unser\q\hiyoribot_flash\env\Scripts\python.exe tools/start_qq.py
# 只停止网关；后端可单独停止
E:\unser\q\hiyoribot_flash\env\Scripts\python.exe tools/start_qq.py --stop
E:\unser\q\hiyoribot_flash\env\Scripts\python.exe tools/start_hiyoribot.py --stop
```

Linux使用仓库外虚拟环境，安装同样两份requirements；设置HIYORI_FLASH_HOME、HIYORI_CONFIG、HIYORI_QQ_ENV_FILE为服务器绝对路径，并把YAML中的sqlite_path/profile_home/qq_profile_home改为Linux路径，再用该虚拟环境的Python运行tools/start_qq.py。无需Docker、GPU或PostgreSQL。每个SQLite文件只允许一个后端进程，不使用多worker；不同群和用户通过线程内锁隔离，模型请求期间不持有数据库写事务。

## 数据与备份

flash从新SQLite开始，原PostgreSQL及其备份不迁移、不删除。运行数据默认位于用户目录 `.local/share/hiyoribot/flash`，可通过上面的环境变量改位置。NoneBot另有网关去重SQLite。角色卡随代码发布，密钥、画像、日志和数据库只留本机。

SQLite启用外键、WAL和写入等待；摘要、批次游标、好感变动使用短事务及唯一约束。群原文保留7天，必要画像证据和摘要长期保留；私聊完整保存。撤回会清除原文及相关旁听证据，并清空无法可靠排除影响的累计摘要和本群摘要存档。

```powershell
python tools/database_backup.py init --database 'E:\unser\q\hiyoribot_flash\hiyoribot.sqlite3'
python tools/database_backup.py status --database 'E:\unser\q\hiyoribot_flash\hiyoribot.sqlite3'
python tools/database_backup.py backup --database 'E:\unser\q\hiyoribot_flash\hiyoribot.sqlite3'
python tools/database_backup.py check --file 'E:\实际备份路径.sqlite3'
# 先停后端，恢复前再自动备份；确认名称必须等于目标数据库文件名
python tools/database_backup.py restore --database 'E:\unser\q\hiyoribot_flash\hiyoribot.sqlite3' --file 'E:\实际备份路径.sqlite3' --confirm-restore hiyoribot.sqlite3
```

备份使用SQLite在线backup接口及SHA256/完整性校验，包含WAL中已提交记录，不直接拷贝正在写入的数据库。恢复受单进程锁保护。

## 验证

```powershell
python tools/verify_group.py
# 使用当前配置模型，会产生API费用；只用临时SQLite和合成资料，不向QQ发消息
python tools/verify_group.py --live-model
# 跟聊真实模型对照：临时库，无QQ发送
python tools/verify_followup.py --live-model
# 在hiyoribot-qq目录验证网关
python -m unittest -v test_gateway
```

现有PG／网页／TTS历史测试保留在tools/legacy_tests，原实现保存在main分支。旧的游戏导入、训练及语音脚本不是flash运行链路，不会被启动或自动导入；需要原环境时使用main分支。
