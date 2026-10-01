# HiyoriBot（V0.8）

这是一个本地、单用户的 Python 聊天项目。后端在 `hiyoribot-backend/`，网页在 `hiyoribot-frontend/`，每次修改的简短记录见 [WORKLOG.md](WORKLOG.md)。

当前范围到 V0.8；工具调用和模型自主决策留给 V0.9～V1.0。

## 一条消息怎样运行

```text
用户消息
  ├─ 角色的 System Prompt（PostgreSQL）
  ├─ 相关原作角色资料（独立 role_knowledge 表，pgvector 召回）
  ├─ 同一会话最近的消息（短期上下文）
  └─ pgvector 找到的相关用户长期记忆
       ↓
     DeepSeek 流式回复 → 保存完整问答
       ↓
     后台再调用一次模型，提取稳定的用户事实 → 本地 embedding → pgvector
```

完整聊天记录和给模型使用的上下文分开保存。短期上下文最多取最近 24 条消息，再按 12,000 字符预算裁剪；长期记忆按向量相似度先取 12 条候选，过滤后结合重要度选最多 5 条。借鉴了 SillyTavern 将角色设定、聊天历史和按需注入的信息分开处理的思路，未复制其代码。参考：[SillyTavern Prompt 文档](https://github.com/SillyTavern/SillyTavern-Docs/blob/main/Usage/Prompts/index.md)、[World Info 文档](https://github.com/SillyTavern/SillyTavern-Docs/blob/main/Usage/worldinfo.md)。

原作角色资料与用户长期记忆使用不同的表。前者存角色经历、风格和剧情线索，按当前角色名和消息语义检索；后者只存用户事实。角色设定窗口会显示当前角色的资料条数。改变角色名称后，旧角色资料不会注入新角色的聊天。

本地已整理的和泉妃爱资料在 `roleplay_data/hiyori_knowledge.jsonl`，是从用户提供的游戏脚本归纳的中文短句，每条保留脚本与行号，不保存原台词。当前共 32 条。修改资料文件后，从 `hiyoribot-backend` 目录运行以下命令重新导入；按来源键更新，重复执行不会增加重复条目：

```powershell
.\venv\Scripts\python.exe import_role_knowledge.py
```

## 启动

1. 准备本机或远程 PostgreSQL，并确认该实例已安装 pgvector 扩展。连接用户需要能执行 `CREATE EXTENSION vector` 和建表语句；表结构见 [schema.sql](hiyoribot-backend/schema.sql)。

2. 在本地 `hiyoribot-backend/config.yaml` 填入 `database_url`，例如 `postgresql://用户名:密码@主机:5432/数据库名`。也可以在启动后端的终端设置 `DATABASE_URL` 环境变量；环境变量优先。请不要把真实连接串提交到代码库。

   ```yaml
   database_url: "postgresql://用户名:密码@主机:5432/数据库名"
   ```

   配置后可先在 `hiyoribot-backend` 目录运行 `python -c "import storage; storage.init_db(); print('database ready')"` 检查连接、pgvector 扩展与建表权限。

3. 安装后端依赖并启动：

   ```powershell
   cd E:\unser\Desktop\hiyori_bot\hiyoribot-backend
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python -m uvicorn main:app --reload
   ```

4. 在 [聊天页面](http://127.0.0.1:8000/) 使用。接口文档在 [API Docs](http://127.0.0.1:8000/docs)。数据库表在第一次需要时自动创建。

项目不再附带 Docker 配置。数据库数据由你选择的 PostgreSQL 实例管理；后端只通过连接串访问。

### 当前电脑的本地数据库

PostgreSQL 16.15 和 pgvector 0.8.6 已安装到 `E:\unser\q\postgresql`，数据目录是 `E:\unser\q\postgresql-data`。连接串保存在被 Git 忽略的 `hiyoribot-backend/config.yaml`，请勿公开该文件。

数据库当前按需手动启动，重启电脑后运行：

```powershell
& 'E:\unser\q\postgresql\Library\bin\pg_ctl.exe' -D 'E:\unser\q\postgresql-data' -l 'E:\unser\q\postgresql-data\server.log' -w start
```

停止时运行：

```powershell
& 'E:\unser\q\postgresql\Library\bin\pg_ctl.exe' -D 'E:\unser\q\postgresql-data' -m fast -w stop
```

数据库只供本机项目使用。网页后端仍需按上面的 Uvicorn 命令启动。

### 模型配置

在 `hiyoribot-backend/config.yaml` 写 DeepSeek `api_key`，或在**启动后端的同一个终端**设置环境变量：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API Key"
```

环境变量优先于 YAML。`config.yaml` 已被 Git 忽略，[配置示例](hiyoribot-backend/config.example.yaml)没有真实 Key。配置里的 `auto_extract_memory: false` 可以关闭每轮的自动提取；默认开启时，每轮成功聊天会增加一次 DeepSeek 请求。

### 本地向量模型

向量使用 FastEmbed 的 `BAAI/bge-small-zh-v1.5`，维度为 512。第一次提取或检索长期记忆时会下载约 90 MB 模型，之后使用本地缓存；这一步不调用 DeepSeek 的 embedding API。模型与维度见 [FastEmbed 官方列表](https://qdrant.github.io/fastembed/examples/Supported_Models/)。

### 日语 TTS 配音

已用共通线和妃爱线的原版日文台词与配音做本机试训，训练数据和最终权重都留在仓库外，详见 [试训说明](tools/README.md)。在被 Git 忽略的 `hiyoribot-backend/config.yaml` 配置本机目录：

```yaml
tts_home: 'E:/unser/q/hiyori_tts'
```

也可设置 `HIYORI_TTS_HOME` 环境变量。目录内需保留 `pilot_v1/active_model.json`、训练参考音频、`GPT-SoVITS` 和独立的 `env`。聊天页面每条助手回复下方有「妃爱配音」按钮；点击后 DeepSeek 提取并翻译一两句台词，再用选定的最终权重在本机生成日语音频。页面显示译文并提供播放控件；同一回复再次请求时读取本机 `pilot_v1/web_audio` 缓存。它不会配音模型思考内容，也不会自动播放每条消息。翻译会增加一次 DeepSeek 请求。

### 导入本机汉化对话

游戏汉化补丁内已有中文剧本，无需调用翻译 API。当前导入范围是共通线与妃爱线的 87 个脚本；其他角色路线和额外成人场景不在范围内。每条正文保留脚本名、序号、说话人和汉化原话；相邻对话另做向量索引。两张原作对话表与用户长期记忆表分开，聊天时只召回少量相关片段。

先从你自己的游戏包提取汉化补丁中的 PF6 文件 `hamidashi.pfs.099`，然后在后端目录运行：

```powershell
.\.venv\Scripts\python.exe import_game_dialogue.py 'E:\你的路径\hamidashi.pfs.099' --dry-run
.\.venv\Scripts\python.exe import_game_dialogue.py 'E:\你的路径\hamidashi.pfs.099'
```

`--dry-run` 只检查；正式导入可重复运行，不会累积重复记录。原始游戏包和全文未加入项目文件。该导入程序只适配当前验证过的汉化包布局，遇到其他版本会因脚本数或正文数不符而停止。

## 页面和接口

- 角色设定：查看与修改名称、描述、性格、背景、说话方式及 System Prompt；每轮请求从数据库读取当前版本并组合成 System 消息。
- 会话：按会话保存完整的用户和助手消息；网页可切换旧会话或开始新会话。
- 模型思考：发送框下方默认勾选「显示模型思考」。DeepSeek 的思考文本与正式回复分开流式显示和保存，历史会话中可展开查看；取消勾选会关闭下一轮的思考模式。思考模式可能增加等待时间和 token 用量。
- 长期记忆：自动提取、手动新增、编辑、合并、调整 1～5 的重要度，以及删除。
- `POST /chat/stream`：请求体可传 `thinking: true/false`（默认 `true`）；SSE 事件为 `meta`（会话 ID 与召回记忆）、`reasoning_delta`（思考片段）、`delta`（正式回复片段）、`done` 或 `error`。`POST /chat` 返回的 JSON 也包含可选的 `reasoning` 字段。
- `GET/PUT /character`、`GET /character/knowledge`（角色资料及原作对话条数）、`GET /conversations`、`GET /conversations/{id}/messages`、`GET/POST /memories`、`PUT/DELETE /memories/{id}`、`POST /memories/merge`。

“遗忘”会从长期记忆表删除那条内容，**不会删除原聊天记录**；最近聊天消息仍可能把同一事实带给模型。当前没有多用户隔离与认证，服务请只绑定在本机。

## 检查

在 `hiyoribot-backend` 目录执行：

```powershell
python -m unittest discover -s tests -v
node --check ..\hiyoribot-frontend\app.js
```

这些检查使用模拟的模型和数据库调用。真正的 PostgreSQL、pgvector、DeepSeek 联通性需要启动相应服务后再验证。

配置好 PostgreSQL＋pgvector 后，可以执行一次真正的数据库验收。测试会创建带唯一前缀的会话和记忆，并在结束时清理：

```powershell
$env:RUN_DB_TEST = "1"
python -m unittest discover -s tests -p test_db_integration.py -v
Remove-Item Env:RUN_DB_TEST
```
