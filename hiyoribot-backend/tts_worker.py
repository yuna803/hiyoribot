"""独立的 GPT-SoVITS Python 环境入口；从标准输入读取日语台词。"""

import argparse
import json
import os
import sys
from pathlib import Path


def generate(home: Path, output: Path, text: str) -> None:
    if not text or len(text) > 180:
        raise ValueError("日语台词为空或过长")
    root = home / "GPT-SoVITS"
    dataset = home / "pilot_v1"
    active = json.loads((dataset / "active_model.json").read_text(encoding="utf-8"))
    reference_id = active["reference_voice_id"]
    with (dataset / "train.jsonl").open(encoding="utf-8") as lines:
        reference = next(row for line in lines
                         if (row := json.loads(line))["voice_id"] == reference_id)

    # 官方推理代码按其仓库目录解析模型和语种模块。
    os.chdir(root)
    sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "tools" / "tts_compat"),
                    str(root / "GPT_SoVITS"), str(root / "tools"), str(root)]
    import numpy as np
    import soundfile as sf
    from TTS_infer_pack.TTS import TTS, TTS_Config

    models = root / "GPT_SoVITS" / "pretrained_models"
    config = TTS_Config({"custom": {
        "device": "cuda", "is_half": True, "version": active["model_version"],
        "t2s_weights_path": str(dataset / active["gpt_weight"]),
        "vits_weights_path": str(dataset / active["sovits_weight"]),
        "bert_base_path": str(models / "chinese-roberta-wwm-ext-large"),
        "cnhuhbert_base_path": str(models / "chinese-hubert-base"),
    }})
    tts = TTS(config)
    pieces = list(tts.run({
        "text": text, "text_lang": "all_ja", "ref_audio_path": reference["wav"],
        "prompt_text": reference["text"], "prompt_lang": "all_ja", "seed": 42,
        "parallel_infer": False, "batch_size": 1, "text_split_method": "cut5",
    }))
    if not pieces:
        raise RuntimeError("没有生成语音")
    rate = pieces[0][0]
    audio = np.concatenate([piece for _, piece in pieces])
    if not 0.3 <= len(audio) / rate <= 90 or not np.any(audio):
        raise RuntimeError("生成语音为空或时长异常")
    sf.write(output, audio, rate, subtype="PCM_16")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    generate(arguments.home.resolve(), arguments.output.resolve(), sys.stdin.read().strip())
