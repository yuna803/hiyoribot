# HiyoriBot（V0.9）

这是一个本地、单用户的 Python 聊天项目。后端在 `hiyoribot-backend/`，网页在 `hiyoribot-frontend/`，每次修改的简短记录见 [WORKLOG.md](WORKLOG.md)。

当前包含角色聊天、长短期记忆、模型主动工具查询和按需日语配音。

## 本机小模型与角色微调

本机模式通过 Ollama 运行 Qwen3-4B-Instruct-2507 的 Q4_K_M 量化模型。它提供正式回复和工具调用，不输出思考链。网页仍使用原来的角色卡、短期上下文、长期记忆和原作检索；微调只学习妃爱的接话和说话方式，剧情事实仍由检索库提供。

本机 Ollama、模型、训练环境和结果放在仓库外的 `E:\unser\q\hiyori_llm`。启动模型服务：

```powershell
.\tools\start_local_llm.ps1
```

在被 Git 忽略的 `hiyoribot-backend/config.yaml` 设置以下字段，然后按后面的启动步骤运行网页：

```yaml
provider: local
local_model: hiyori-base
local_auxiliary_model: hiyori-base
local_base_url: http://127.0.0.1:11434/v1
local_tokenizer_path: 'E:/unser/q/hiyori_llm/models/tokenizer.json'
local_context_tokens: 8192
```

`local_model` 用于聊天，`local_auxiliary_model` 用于记忆提取与忠实日语翻译，后者保留未微调的基础模型。`provider: local` 不读取 `DEEPSEEK_*` 环境变量，也不会在本地失败时回退收费 API。切回 `provider: deepseek` 才会使用原有云端配置。`GET /model-status` 只返回提供方、模型名称和是否支持思考，不暴露密钥或连接串。

本地上下文用模型的 `tokenizer.json` 计算预算，预留输出和模板开销；超预算先删除完整旧问答，再删参考资料，不能容纳角色设定与当前问答时明确报错。每轮工具调用重新检查预算。本地聊天、记忆提取和配音串行使用显卡；配音翻译完成后必须成功卸载本项目的语言模型，才启动 GPT-SoVITS。缓存命中不重新合成。原始台词、训练数据和权重不提交 Git。

### 原作对话数据与训练

只读导出已入库的共通线和妃爱线中文台词，不使用用户私聊或机器人生成回复：

```powershell
.\hiyoribot-backend\venv\Scripts\python.exe tools\llm_pilot_dataset.py --output 'E:\unser\q\hiyori_llm\pilot_v1'
```

数据按原作说话人构造 user（智宏）/assistant（妃爱），最多保留三轮完整上下文；旁白和其他角色插话切断片段，相邻同说话人合并。按完整脚本分约 80/10/10 三组，再去掉重复目标回复；最终样本比例会因各脚本长度不同而变化。报告记录剔除原因和各组来源，公开代码不包含游戏全文。

训练使用独立 `Hiyori-Train` Ubuntu WSL2，虚拟环境 `/opt/hiyori-env`，关键依赖见 [训练依赖](tools/llm-training-requirements.txt)。基础训练权重来自 `unsloth/Qwen3-4B-Instruct-2507-unsloth-bnb-4bit`，推理 GGUF 来自 `unsloth/Qwen3-4B-Instruct-2507-GGUF`；下载时固定 revision，并保留本机清单与完整依赖锁定文件。模型加载使用本机文件。训练前暂时停止网页服务，卸载 Ollama 模型，避免与训练竞争显存。

```powershell
wsl -d Hiyori-Train -u root -- /opt/hiyori-env/bin/python /mnt/e/unser/Desktop/hiyori_bot/tools/llm_pilot_train.py --model /mnt/e/unser/q/hiyori_llm/training_base --dataset /mnt/e/unser/q/hiyori_llm/pilot_v1 --stage auto
```

`auto` 先运行 1,024 tokens 的 20 步显存检查；CUDA OOM 才重试 512 tokens，两次 OOM 就停止。检查通过后重新加载模型，按 rank 8、批量 1、梯度累积 8、学习率 `1e-4` 训练两轮，只计算 assistant 回复的损失。长样本跳过并记录，不截断目标台词。保存每轮 adapter 和训练配置；训练中其他错误或 OOM 会停止，不自动加轮数。

### 对照与使用门槛

用 `tools/llm_pilot_evaluate.py --model ... --dataset ... --variant baseline|early|final` 在同一量化与采样条件下比较三组权重，包含新日常话题、连续聊天、未训练过的脚本对话、记忆提取和翻译。结果与 HTML 对照页只保存在本机。工具协议测试使用固定日期结果，再单独通过网页接口检查真实工具调用；对照输出限制为 256 tokens，并标记触及上限的回复。

比较接话自然度、角色身份、复读、原作事实与工具能力；低训练损失不等于角色效果更好。本机默认先使用 `hiyori-base`，经用户对照体验后才切换候选角色权重。机器翻译和原作事实仍需核对，尤其注意漏词、称呼和人称。

本轮两组 adapter 在新话题上出现工具标签复读，未通过使用门槛，保留在本机供检查，不导入 Ollama 作为聊天模型。当前 Unsloth 的合并流程要求原始 16 位基础权重；本轮直接合并 NF4 基础权重的尝试被拒绝，没有写出候选 GGUF。先排查回复标签、语料与训练配置，通过对照后再安排下一次训练与导出；本轮不增加训练轮数。

## 一条消息怎样运行

```text
用户消息
  ├─ 角色的 System Prompt（PostgreSQL）
  ├─ 相关原作角色资料（独立 role_knowledge 表，pgvector 召回）
  ├─ 同一会话最近的消息（短期上下文）
  └─ pgvector 找到的相关用户长期记忆
       ↓
     本机模型或 DeepSeek → 按需调用查询工具 → 读取结果继续判断 → 流式回复 → 保存完整问答
       ↓
     后台再调用一次模型，提取稳定的用户事实 → 本地 embedding → pgvector
```

完整聊天记录和给模型使用的上下文分开保存。短期上下文最多取最近 24 条消息，按完整问答及其工具消息做约 12,000 字符裁剪；最新一轮整体保留，可能略超预算。长期记忆按向量相似度先取 12 条候选，过滤后结合重要度选最多 5 条。借鉴了 SillyTavern 将角色设定、聊天历史和按需注入的信息分开处理的思路，未复制其代码。参考：[SillyTavern Prompt 文档](https://github.com/SillyTavern/SillyTavern-Docs/blob/main/Usage/Prompts/index.md)、[World Info 文档](https://github.com/SillyTavern/SillyTavern-Docs/blob/main/Usage/worldinfo.md)。

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

DeepSeek 模式的环境变量优先于 YAML。`config.yaml` 已被 Git 忽略，[配置示例](hiyoribot-backend/config.example.yaml)没有真实 Key。配置里的 `auto_extract_memory: false` 可以关闭每轮的自动提取；默认开启时，每轮成功聊天会增加一次模型请求。本地模式请求基础辅助模型，DeepSeek 模式请求云端。

### 本地向量模型

向量使用 FastEmbed 的 `BAAI/bge-small-zh-v1.5`，维度为 512。第一次提取或检索长期记忆时会下载约 90 MB 模型，之后使用本地缓存；这一步不调用 DeepSeek 的 embedding API。模型与维度见 [FastEmbed 官方列表](https://qdrant.github.io/fastembed/examples/Supported_Models/)。

### 模型主动工具查询

发送框下方默认勾选「允许查询工具」。模型可按需要调用五个只读工具：`get_current_time` 查询北京时间，`search_user_memory` 检索用户长期记忆，`search_character_knowledge` 检索当前角色的原作资料，`search_chat_history` 用关键词查当前会话的旧消息，`search_web` 联网搜索公开信息。已有上下文足够时可以直接回复；查询结果不足时可以换关键词继续查。

每条消息最多 4 轮工具调用、8 次执行，然后再请求一次模型直接回复。相同查询在本轮内复用结果；参数错误和工具失败会交回模型处理。工具结果仅作参考资料，当前会话和角色范围由后端限定。查询循环见 `agent_service.py`，工具定义与执行见 `agent_tools.py`。

页面用折叠区域展示每次调用的参数、结果与轮次，成功聊天后保存在 `message.agent_messages`；刷新旧会话也能查看。下一轮聊天会按完整协议回传这些工具消息与模型返回的 `reasoning_content`，符合 [DeepSeek 思考模式的工具调用要求](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。思考开关和工具开关可以独立使用；多次调用会增加模型请求次数和等待时间。

联网搜索使用 [DDGS](https://github.com/deedy5/ddgs) 的 Bing 后端，无需新增 API Key。每次最多取 5 条标题、摘要与链接，支持 `timelimit` 最近一天 `d`、一周 `w`、一月 `m`、一年 `y`。网页调用记录中的来源可点击；返回的是搜索摘要，不是网页全文，检索时间也不等于发布时间。查询只发送模型选出的公开关键词，搜索失败会作为工具错误交回模型处理。需要代理时可在启动后端的终端设置 `DDGS_PROXY` 环境变量，地址填自己的本机代理。

### 日语 TTS 配音

已用共通线和妃爱线的原版日文台词与配音做本机试训，训练数据和最终权重都留在仓库外，详见 [试训说明](tools/README.md)。在被 Git 忽略的 `hiyoribot-backend/config.yaml` 配置本机目录：

```yaml
tts_home: 'E:/unser/q/hiyori_tts'
```

也可设置 `HIYORI_TTS_HOME` 环境变量。目录内需保留 `pilot_v1/active_model.json`、训练参考音频、`GPT-SoVITS` 和独立的 `env`。聊天页面每条助手回复下方有「妃爱配音」按钮；点击后按原顺序保留全部台词，要求配置的模型完整、忠实地译成日语，再用选定的最终权重在本机生成音频。按当前角色回复约定，括号内容作为动作说明跳过；代码块不朗读，Markdown 链接保留可见文字。长译文分段合成后拼接，不挑一两句或做摘要。

页面显示完整日语译文，并可展开「查看中文配音原文」核对。翻译仍可能有错误，实际结果以中日对照和试听为准。它不会配音模型思考内容，也不会自动播放每条消息。翻译会增加一次模型请求，本地模式使用基础辅助模型，DeepSeek 模式使用收费 API；同一回复再次请求时读取本机 `pilot_v1/web_audio` 缓存。翻译规则、提供方与辅助模型配置参与缓存键，旧版摘要配音不再自动复用；已有页面需刷新并重新点击配音。

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
