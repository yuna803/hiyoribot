# HiyoriBot QQ 网关

独立的 NoneBot2 / OneBot 11 网关，接收 NapCat 反向 WebSocket 消息并转发给 HiyoriBot 后端。私聊直接触发；群内真实 @或引用本机器人在本群发出的消息即时回复，普通群消息进入旁听。记忆和身份绑定由后端负责。

## 配置与启动

1. 在本目录执行 `python -m pip install -r requirements.txt`。
2. 将 `.env.example` 的内容复制到私有配置文件 `E:\unser\q\hiyoribot_runtime\qq\.env`，填写两个令牌。不要提交真实配置。
3. 启动后端，然后在本目录运行 `python bot.py`。NapCat 的 OneBot 11 反向 WebSocket 地址使用 `ws://127.0.0.1:8086/onebot/v11/ws`，访问令牌与 `ONEBOT_ACCESS_TOKEN` 一致。

可用环境变量 `HIYORI_QQ_ENV_FILE` 指定其他私有配置文件，`HIYORI_QQ_STATE_DB` 指定去重数据库路径；默认数据库位于配置文件同目录。哥哥身份由后端私有 YAML 的 `qq_owner_id` 绑定，网关不能根据聊天文本改绑。

网关将每条已接收消息先记入数据库，再请求一次后端；超时、进程中断或 QQ 发送失败均不会自动重试。输入超过 4000 字时会提示缩短；后端繁忙时会提示稍后重新发送。为避免消息日志泄露，启动脚本固定日志级别为 `WARNING`。回复最多发送 12000 字，按每段 2000 字拆分。去重记录持续保留；删除数据库会使旧消息可能再次处理。

## 群旁听

- 所有群默认启用，各群独立累计50条非自身消息；非文字只留下 `[image]`、`[record]` 等类型，不做识别。普通采集与直接回复分开去重。
- 后端先用简短模型请求更新摘要、决定是否沉默；有必要才运行完整角色 Agent。主动插话每群至少间隔5分钟，滚动一小时最多3次。失败和不确定的发送尝试也占用冷却，防止异常时连续发送。
- 生成期间有新消息则复核一次；复核或发送前检查期间再有消息、直接互动、撤回或开关变化，就弃稿。断线、禁言、超时不向群里发送错误，不自动重发。后台候选最多并行处理2个群。
- 本人可在目标群单独发 `/旁听 关闭` 或 `/旁听 开启`，权限由后端 `qq_owner_id` 检查。暂停停止普通采集，即时互动不受影响；恢复从零计数。不支持模型修改开关。
- 真实引用只认发送成功回执里的消息编号，且必须同机器人、同群。功能启用前没有发送回执的老消息不作为引用触发依据，仍可真实 @。
- 原始群文本7天后清除；保留去重编号、非可逆指纹、群摘要及画像实际引用的必要原话。撤回会删除待处理内容、清空可能受影响的群摘要、撤销相关旁听画像证据，并重新同步画像文件。
- 旁听画像只在50条观察批次完成后整理：每人首次10条有效发言，此后25条；跨群累计但证据注明来源，群回复只用本群资料。旁听不改身份、关系或好感度；原有直接互动20/50条门槛独立。

数据以 PostgreSQL 为准；SQLite 只存直接回复去重和本机器人已发消息编号。后台模型失败保留旧资料，下次新消息或服务重启再检查；重启不会补发旧候选。

## 验证

网关在本目录执行 `python -m unittest -v test_gateway`。后端在项目根目录执行：

```powershell
.\hiyoribot-backend\venv\Scripts\python.exe tools/verify_group.py
.\hiyoribot-backend\venv\Scripts\python.exe tools/verify_group.py --all
# 下面会产生已配置模型的API费用；只用合成资料和临时库，不发QQ。
.\hiyoribot-backend\venv\Scripts\python.exe tools/verify_group.py --live-model
```

验证脚本只创建并删除它本次新建的 `profile_test_` 数据库。不要直接把 `PROFILE_TEST_DATABASE=1` 指向正式库。
