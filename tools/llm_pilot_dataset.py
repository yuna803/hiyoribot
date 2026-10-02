"""导出按原作脚本隔离的妃爱角色微调数据。"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "hiyoribot-backend"
ROLECARD_PATH = ROOT / "rolecards" / "izumi-hiyori.json"
DEFAULT_OUTPUT = Path(r"E:\unser\q\hiyori_llm\pilot_v1")
SPLIT_NAMES = ("train", "validation", "test")
SPLIT_PRIORITY = {"train": 0, "validation": 1, "test": 2}


def selected_script(name: str) -> bool:
    """只接受已导入的共通线与妃爱线脚本。"""
    basename = name.removeprefix("script\\")
    return basename.endswith(".ast") and basename.startswith(("共通-", "妃愛-"))


def split_scripts(script_names: Iterable[str], seed: int = 42) -> dict[str, str]:
    """按完整脚本固定随机分组，比例约为 80/10/10。"""
    names = sorted(set(script_names))
    random.Random(seed).shuffle(names)
    count = len(names)
    if count == 0:
        return {}
    if count == 1:
        sizes = (1, 0, 0)
    elif count == 2:
        sizes = (1, 0, 1)
    else:
        train_count = min(count - 2, max(1, round(count * 0.8)))
        validation_count = min(
            count - train_count - 1, max(1, round(count * 0.1))
        )
        sizes = (train_count, validation_count, count - train_count - validation_count)

    result: dict[str, str] = {}
    start = 0
    for split, size in zip(SPLIT_NAMES, sizes):
        for name in names[start : start + size]:
            result[name] = split
        start += size
    return result


def _target_rejection_reason(text: str) -> str | None:
    visible = "".join(character for character in text if not character.isspace())
    if visible and not any(unicodedata.category(character)[0] in "LN" for character in visible):
        return "pure_punctuation"
    if len(visible) < 3:
        return "too_short"
    return None


def build_samples(
    rows: Iterable[Mapping[str, object]], system_prompt: str
) -> tuple[list[dict[str, object]], Counter[str], int]:
    """每个有效妃爱回复构造一个样本，最多保留三轮上下文。"""
    ordered = sorted(rows, key=lambda row: (str(row["script_name"]), int(row["entry_no"])))
    raw_hiyori_lines = sum(
        row.get("scope") in ("common", "hiyori") and row.get("speaker") == "妃愛"
        for row in ordered
    )
    excluded: Counter[str] = Counter()
    samples: list[dict[str, object]] = []
    turns: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    current_script: str | None = None
    segment_scope: str | None = None

    def finish_turn() -> None:
        nonlocal current
        if current is None:
            return

        speaker = current["speaker"]
        if speaker == "妃愛":
            if not turns or turns[-1]["speaker"] != "智宏":
                excluded["missing_tomohiro_context"] += 1
            else:
                reply = str(current["content"])
                reason = _target_rejection_reason(reply)
                if reason:
                    excluded[reason] += 1
                else:
                    window = (turns + [current])[-6:]
                    if window and window[0]["speaker"] == "妃愛":
                        window = window[1:]
                    if not window or window[0]["speaker"] != "智宏":
                        excluded["missing_tomohiro_context"] += 1
                    else:
                        messages = [{"role": "system", "content": system_prompt}]
                        messages.extend(
                            {
                                "role": "user" if turn["speaker"] == "智宏" else "assistant",
                                "content": turn["content"],
                            }
                            for turn in window
                        )
                        samples.append(
                            {
                                "messages": messages,
                                "script_name": current["script_name"],
                                "entry_no": current["entry_nos"][-1],
                                "scope": current["scope"],
                            }
                        )
        turns.append(current)
        current = None

    for row in ordered:
        script_name = str(row["script_name"])
        if current_script != script_name:
            finish_turn()
            current = None
            turns = []
            segment_scope = None
            current_script = script_name

        scope = row.get("scope")
        speaker = row.get("speaker")
        if (
            not selected_script(script_name)
            or scope not in ("common", "hiyori")
            or speaker not in ("智宏", "妃愛")
        ):
            finish_turn()
            turns = []
            segment_scope = None
            continue

        if segment_scope is not None and scope != segment_scope:
            finish_turn()
            turns = []
        segment_scope = str(scope)

        if current is not None and current["speaker"] == speaker and current["scope"] == scope:
            current["content"] = f'{current["content"]}\n{row["content"]}'
            current["entry_nos"].append(int(row["entry_no"]))
            continue

        finish_turn()
        current = {
            "script_name": script_name,
            "scope": str(scope),
            "speaker": str(speaker),
            "content": str(row["content"]),
            "entry_nos": [int(row["entry_no"])],
        }

    finish_turn()
    return samples, excluded, int(raw_hiyori_lines)


def deduplicate_targets(
    samples: list[dict[str, object]], script_splits: Mapping[str, str]
) -> tuple[list[dict[str, object]], int]:
    """相同回复优先留给测试、验证组，避免进入训练组。"""
    ranked = sorted(
        enumerate(samples),
        key=lambda item: (-SPLIT_PRIORITY[script_splits[str(item[1]["script_name"])]], item[0]),
    )
    kept_indices: set[int] = set()
    seen: set[str] = set()
    duplicates = 0
    for index, sample in ranked:
        reply = str(sample["messages"][-1]["content"])
        normalized = " ".join(reply.split())
        if normalized in seen:
            duplicates += 1
        else:
            seen.add(normalized)
            kept_indices.add(index)
    return [sample for index, sample in enumerate(samples) if index in kept_indices], duplicates


def _read_system_prompt() -> str:
    rolecard = json.loads(ROLECARD_PATH.read_text(encoding="utf-8"))
    prompt = rolecard.get("system_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"角色卡缺少 system_prompt：{ROLECARD_PATH}")
    return prompt


def _fetch_lines() -> list[Mapping[str, object]]:
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    import storage

    # 使用现有连接配置，只读导出，不执行 storage.connect() 的建表初始化。
    with storage._open_connection() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        return connection.execute(
            """SELECT script_name, entry_no, scope, speaker, content
               FROM game_dialogue_line
               WHERE scope IN ('common', 'hiyori')
               ORDER BY script_name ASC, entry_no ASC"""
        ).fetchall()


def export_dataset(output: Path, max_train_samples: int = 1500) -> dict[str, object]:
    output = output.expanduser().resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("原作全文与训练数据必须导出到仓库外")
    rows = _fetch_lines()
    selected_rows = [row for row in rows if selected_script(str(row["script_name"]))]
    system_prompt = _read_system_prompt()
    samples, excluded, raw_hiyori_lines = build_samples(selected_rows, system_prompt)
    script_splits = split_scripts({str(row["script_name"]) for row in selected_rows})
    samples, duplicate_count = deduplicate_targets(samples, script_splits)
    excluded["duplicate_target_response"] += duplicate_count

    split_samples: dict[str, list[dict[str, object]]] = {name: [] for name in SPLIT_NAMES}
    for sample in samples:
        split_samples[script_splits[str(sample["script_name"])]].append(sample)
    if len(split_samples["train"]) > max_train_samples:
        excluded["train_sample_limit"] += len(split_samples["train"]) - max_train_samples
        split_samples["train"] = split_samples["train"][:max_train_samples]

    output.mkdir(parents=True, exist_ok=True)
    split_report: dict[str, dict[str, int]] = {}
    for split in SPLIT_NAMES:
        split_path = output / f"{split}.jsonl"
        with split_path.open("w", encoding="utf-8", newline="\n") as handle:
            for sample in split_samples[split]:
                handle.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        split_report[split] = {
            "script_count": sum(assigned == split for assigned in script_splits.values()),
            "sample_script_count": len({str(sample["script_name"]) for sample in split_samples[split]}),
            "sample_count": len(split_samples[split]),
        }

    report: dict[str, object] = {
        "format": "messages",
        "seed": 42,
        "split_ratio": {"train": 0.8, "validation": 0.1, "test": 0.1},
        "max_train_samples": max_train_samples,
        "raw_hiyori_lines": raw_hiyori_lines,
        "candidate_samples": len(samples) + duplicate_count,
        "effective_samples": sum(len(split_samples[name]) for name in SPLIT_NAMES),
        "excluded_reasons": dict(sorted(excluded.items())),
        "splits": split_report,
    }
    (output / "dataset_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="导出本机妃爱角色微调 pilot 数据集")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="导出目录")
    parser.add_argument(
        "--max-train-samples", type=int, default=1500, help="训练样本上限（默认 1500）"
    )
    args = parser.parse_args()
    if args.max_train_samples < 1:
        parser.error("--max-train-samples 必须大于 0")
    report = export_dataset(args.output, args.max_train_samples)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
