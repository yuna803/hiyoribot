# 妃爱日语 TTS 试训

本目录只放可复现脚本。原声、日文训练标注、试听音频和模型权重留在本机 `E:\unser\q\hiyori_tts`，不进入公开仓库。本轮只使用共通线和妃爱主线，不接聊天网页。

## 数据准备

使用本机 Python 3.11 环境，安装 `numpy`、`soundfile`；7-Zip 用于从游戏 RAR 中只读流式获取语音包。`root.pfs` 是原版日文脚本。示例路径需替换成自己持有的游戏文件：

```powershell
python tools/tts_pilot_dataset.py --script-pfs 'E:\游戏目录\root.pfs' --game-rar 'E:\游戏压缩包.rar' --output 'E:\unser\q\hiyori_tts\pilot_v1'
```

脚本会核对 87 个脚本中的 2,081 条配音和日文台词，选 30 分钟训练、约 5 分钟不同脚本的测试语音；输出 `train.list`、JSONL 清单和 WAV。输出目录必须在本仓库之外且为空。

## 训练和试听

使用官方 GPT-SoVITS v2Pro 的独立代码与预训练权重。Windows 上原版 `pyopenjtalk`、`jieba_fast` 缺少本机可用的预编译包：本次使用 `pyopenjtalk-mod`，并只在训练进程的 `PYTHONPATH` 中用 `tts_compat` 将 `jieba_fast` 导入转到 `jieba`。日文文本不调用中文分词。还需要本机 FFmpeg 可执行文件。

```powershell
python tools/tts_pilot_train.py --gpt-root 'E:\unser\q\hiyori_tts\GPT-SoVITS' --dataset 'E:\unser\q\hiyori_tts\pilot_v1' --python 'E:\unser\q\hiyori_tts\env\python.exe'
python tools/tts_pilot_samples.py --gpt-root 'E:\unser\q\hiyori_tts\GPT-SoVITS' --dataset 'E:\unser\q\hiyori_tts\pilot_v1' --variant baseline
python tools/tts_pilot_samples.py --gpt-root 'E:\unser\q\hiyori_tts\GPT-SoVITS' --dataset 'E:\unser\q\hiyori_tts\pilot_v1' --variant early
python tools/tts_pilot_samples.py --gpt-root 'E:\unser\q\hiyori_tts\GPT-SoVITS' --dataset 'E:\unser\q\hiyori_tts\pilot_v1' --variant final
```

训练可加 `--stage preprocess|sovits|gpt` 单独重跑阶段。三个试听版本使用相同参考语音、同一条未训练过的原版日文台词和同一条新写的日文台词，便于比较是否过拟合。

生成完三个版本后，本机 `samples/试听对照.html` 可并排播放。若要做可选的发音代理检查，先把官方 `Systran/faster-whisper-small` 模型下载到仓库外，再运行：

```powershell
python tools/tts_pilot_evaluate.py --dataset 'E:\unser\q\hiyori_tts\pilot_v1' --model-path 'E:\unser\q\hiyori_tts\whisper_small'
```

识别错误率只用于发现明显漏字、重复或发音变化；音色与自然度仍要靠试听判断。

用户已选择最终权重作为当前使用版本。本机 `pilot_v1/active_model.json` 指向 SoVITS 第 4 轮和 GPT 第 5 轮权重，后续接入时以它为准；训练权重文件不提交公开仓库。
