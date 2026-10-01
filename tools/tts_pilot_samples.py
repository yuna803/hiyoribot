"""用同一参考音频与测试句生成 v2Pro 基线、早期和最终权重的试听样本。"""

import argparse
import gc
import json
import os
import sys
from html import escape
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


NOVEL_SENTENCE = "今日は少し疲れたけど、お兄ちゃんの顔を見たら元気になったよ。"


def update_comparison_page(folder: Path) -> None:
    versions = {}
    for variant in ("baseline", "early", "final"):
        path = folder / f"{variant}.json"
        if path.exists():
            versions[variant] = {row["kind"]: row for row in json.loads(path.read_text(encoding="utf-8"))}
    rows = []
    for kind in ("heldout", "novel"):
        sample = next((group[kind] for group in versions.values() if kind in group), None)
        if sample is None:
            continue
        cells = "".join(
            f'<td><audio controls preload="none" src="{variant}_{kind}.wav"></audio></td>'
            if variant in versions else "<td>待生成</td>"
            for variant in ("baseline", "early", "final")
        )
        rows.append(f"<tr><th>{'未训练台词' if kind == 'heldout' else '全新句子'}<small>"
                    f"{escape(sample['text'])}</small></th>{cells}</tr>")
    folder.joinpath("试听对照.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<title>妃爱日语 TTS 试训对照</title>'
        '<style>body{font:16px sans-serif;max-width:1100px;margin:40px auto;padding:0 20px}'
        'table{border-collapse:collapse;width:100%}th,td{padding:16px;border:1px solid #ddd}'
        'th{text-align:left}small{display:block;margin-top:8px;font-weight:normal}'
        'audio{max-width:210px}</style><h1>妃爱日语 TTS 试训对照</h1>'
        '<p>同一参考音频、同一测试句；未训练台词来自独立测试脚本。</p>'
        '<table><tr><th>测试句</th><th>未微调</th><th>早期权重</th><th>最终权重</th></tr>'
        + "".join(rows) + '</table></html>', encoding="utf-8"
    )


def checkpoint(folder: Path, pattern: str) -> Path:
    paths = sorted(folder.glob(pattern))
    if len(paths) != 1:
        raise FileNotFoundError(f"预期找到一个 {folder / pattern}，实际 {len(paths)} 个")
    return paths[0]


def make_samples(root: Path, dataset: Path, variant: str) -> list[dict]:
    sys.path[:0] = [str(Path(__file__).with_name("tts_compat")),
                    str(root / "GPT_SoVITS"), str(root / "tools"), str(root)]
    os.environ["PATH"] = str(Path(sys.executable).parent / "Library" / "bin") + os.pathsep + os.environ["PATH"]
    from TTS_infer_pack.TTS import TTS, TTS_Config

    models = root / "GPT_SoVITS" / "pretrained_models"
    if variant == "baseline":
        gpt, sovits = models / "s1v3.ckpt", models / "v2Pro" / "s2Gv2Pro.pth"
    else:
        s2_epoch, gpt_epoch = (2, 2) if variant == "early" else (4, 5)
        sovits = checkpoint(dataset / "weights" / "sovits", f"*_e{s2_epoch}_*.pth")
        gpt = checkpoint(dataset / "weights" / "gpt", f"*e{gpt_epoch}*.ckpt")

    train = [json.loads(line) for line in (dataset / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    test = [json.loads(line) for line in (dataset / "test.jsonl").read_text(encoding="utf-8").splitlines()]
    references = [row for row in train if 6 <= row["duration"] <= 9 and row["quiet_fraction"] < 0.25]
    reference = min(references, key=lambda row: (abs(row["rms"] - 0.10), row["voice_id"]))
    heldout = next(row for row in sorted(test, key=lambda row: row["voice_id"])
                   if 5 <= row["duration"] <= 9 and 15 <= len(row["text"]) <= 60)
    if NOVEL_SENTENCE in {row["text"] for row in train}:
        raise ValueError("新句子意外出现在训练集")

    config = TTS_Config({"custom": {
        "device": "cuda", "is_half": True, "version": "v2Pro",
        "t2s_weights_path": str(gpt), "vits_weights_path": str(sovits),
        "bert_base_path": str(models / "chinese-roberta-wwm-ext-large"),
        "cnhuhbert_base_path": str(models / "chinese-hubert-base"),
    }})
    tts = TTS(config)
    target = dataset / "samples"
    target.mkdir(exist_ok=True)
    records = []
    for kind, text in (("heldout", heldout["text"]), ("novel", NOVEL_SENTENCE)):
        pieces = list(tts.run({
            "text": text, "text_lang": "all_ja", "ref_audio_path": reference["wav"],
            "prompt_text": reference["text"], "prompt_lang": "all_ja", "seed": 42,
            "parallel_infer": False, "batch_size": 1, "text_split_method": "cut5",
        }))
        if not pieces:
            raise RuntimeError(f"{variant}/{kind} 没有生成音频")
        rate = pieces[0][0]
        audio = np.concatenate([piece for _, piece in pieces])
        if len(audio) / rate < 0.5 or not np.any(audio):
            raise RuntimeError(f"{variant}/{kind} 音频过短或全静音")
        path = target / f"{variant}_{kind}.wav"
        sf.write(path, audio, rate, subtype="PCM_16")
        records.append({"variant": variant, "kind": kind, "wav": str(path),
                        "text": text, "duration": round(len(audio) / rate, 2),
                        "reference_voice_id": reference["voice_id"],
                        "heldout_voice_id": heldout["voice_id"]})
    del tts
    gc.collect()
    torch.cuda.empty_cache()
    (target / f"{variant}.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    update_comparison_page(target)
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpt-root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--variant", choices=("baseline", "early", "final"), required=True)
    args = parser.parse_args()
    print(json.dumps(make_samples(args.gpt_root.resolve(), args.dataset.resolve(), args.variant),
                     ensure_ascii=False, indent=2))
