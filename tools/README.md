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

## 混合日语与英文的发音资源

GPT-SoVITS 的 `all_ja` 仍会把拉丁字母片段交给英语发音模块。需要 NLTK 的 `cmudict`、`averaged_perceptron_tagger` 和新版 `averaged_perceptron_tagger_eng`。只含日文的测试可能不会触发这些依赖；遇到英文词再自动下载容易因网络或代理检查失败。

从 [NLTK 官方数据索引](https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/index.xml) 下载这三个包对应的官方 ZIP，将索引中这三个 `<package>` 的属性作为 JSON 数组保存为同目录的 `packages.json`（须包含 `id` 与 `sha256_checksum`）。本机已核验的文件位于 `E:\unser\q\hiyori_tts\downloads\nltk`。用独立 TTS Python 离线安装：

```powershell
& 'E:\unser\q\hiyori_tts\env\python.exe' tools/setup_tts_resources.py --home 'E:\unser\q\hiyori_tts' --archives 'E:\unser\q\hiyori_tts\downloads\nltk'
```

脚本校验 SHA256 与解压路径，只写 `hiyori_tts/env/nltk_data`；保留 ZIP 以满足 `g2p_en` 的导入检查，并实际加载词典与英语词性标注器。配音 worker 在加载 GPU 模型前检查这些资源，缺少时明确报错，不在聊天过程中下载。其他合成失败的完整诊断保存在仓库外 `pilot_v1/web_audio/*.error.log`。
