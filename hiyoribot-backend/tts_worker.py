"""独立的 GPT-SoVITS Python 环境入口；从标准输入读取日语台词。"""

import argparse
import json
import os
import sys
from pathlib import Path

MAX_TEXT_CHARS = 8000
MAX_AUDIO_SECONDS = 1800


def split_japanese_text(text: str, limit: int = 160) -> list[str]:
    """尽量在句末分段，单句过长也按长度拆开，避免模型提前结束。"""
    chunks, start = [], 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = max(text.rfind(mark, start, end) for mark in "。！？!?\n")
            if boundary >= start:
                end = boundary + 1
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start = end
    return chunks

def generate(home: Path, output: Path, text: str) -> None:
    if not text or len(text) > MAX_TEXT_CHARS:
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
    rate, frames, arrays = None, 0, []
    for chunk in split_japanese_text(text):
        pieces = list(tts.run({
            "text": chunk, "text_lang": "all_ja", "ref_audio_path": reference["wav"],
            "prompt_text": reference["text"], "prompt_lang": "all_ja", "seed": 42,
            "parallel_infer": False, "batch_size": 1, "text_split_method": "cut5",
        }))
        if not pieces:
            raise RuntimeError("某段台词没有生成语音")
        for sample_rate, piece in pieces:
            rate = sample_rate if rate is None else rate
            if sample_rate != rate or not np.any(piece):
                raise RuntimeError("某段配音格式不一致或全静音")
            frames += len(piece)
            if frames / rate > MAX_AUDIO_SECONDS:
                raise RuntimeError("生成语音时长超出限制")
            arrays.append(piece)
    if not arrays:
        raise RuntimeError("没有生成语音")
    audio = np.concatenate(arrays)
    if not 0.3 <= len(audio) / rate <= MAX_AUDIO_SECONDS:
        raise RuntimeError("生成语音为空或时长异常")
    sf.write(output, audio, rate, subtype="PCM_16")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    generate(arguments.home.resolve(), arguments.output.resolve(), sys.stdin.read().strip())
