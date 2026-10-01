"""用日语 ASR 对试听音频做发音代理检查；分数不能代替人工听感。"""

import argparse
import json
import re
from pathlib import Path

import pyopenjtalk
from faster_whisper import WhisperModel


def distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for column, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (char != other)))
        previous = current
    return previous[-1]


def sounds(text: str) -> str:
    kana = pyopenjtalk.g2p(text, kana=True)
    return re.sub(r"[^\u30a0-\u30ff]", "", kana)


def evaluate(dataset: Path, model_path: Path) -> list[dict]:
    folder = dataset / "samples"
    model = WhisperModel(str(model_path), device="cuda", compute_type="float16")
    records = []
    for variant in ("baseline", "early", "final"):
        for row in json.loads((folder / f"{variant}.json").read_text(encoding="utf-8")):
            segments, _info = model.transcribe(row["wav"], language="ja", beam_size=5)
            recognized = "".join(segment.text for segment in segments).strip()
            expected_phones = sounds(row["text"])
            heard_phones = sounds(recognized)
            records.append({**row, "asr_text": recognized,
                            "kana_error_rate": round(distance(expected_phones, heard_phones)
                                                     / max(1, len(expected_phones)), 3)})
    folder.joinpath("asr_evaluation.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return [{"variant": row["variant"], "kind": row["kind"],
             "kana_error_rate": row["kana_error_rate"]} for row in records]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.dataset.resolve(), args.model_path.resolve()), ensure_ascii=False))
