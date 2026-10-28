# flash QQ网关

NoneBot2／OneBot 11适配器将NapCat事件转发到SQLite QQ后端。私聊、真实@及引用网关登记过的本群机器人消息即时回复；其他成员消息按群累计50条旁听。非文字只记录类型占位，不进行识别。

安装本目录requirements，把.env.example复制到仓库外qq.env，设置HIYORI_QQ_ENV_FILE；用tools/start_qq.py一起启动后端与网关。哥哥身份仅由后端qq_owner_id绑定，昵称和聊天文字不能修改身份。Linux/Windows具体步骤见根README。

网关只保存直接回复去重及已发送消息编号，业务数据、群摘要、画像、批次和发送许可在后端SQLite。采集与直接回复分别去重。本人命令/旁听 开启、/旁听 关闭由后端校验权限。主动发送前检查在线、禁言、群事件变化；超时或不确定不自动重发，不往群里发旁听错误。

反向WebSocket为ws://127.0.0.1:8086/onebot/v11/ws，NapCat令牌与ONEBOT_ACCESS_TOKEN匹配；网关调用后端使用另一个独立HIYORI_BACKEND_TOKEN。旧回执未登记的历史机器人消息不能单独引用触发，可真实@。

测试：在本目录执行python -m unittest -v test_gateway。全部使用合成事件，不向真实QQ发送消息。
