"""将原作角色知识导入独立的 role_knowledge 表。"""

import argparse
import json
from pathlib import Path

import memory_service
import storage


DATA_PATH = Path(__file__).resolve().parent.parent / "roleplay_data" / "hiyori_knowledge.jsonl"
ALLOWED_KINDS = {"fact", "style", "scene"}
EXPECTED_FIELDS = {"source_key", "kind", "content"}


def load_entries(path: Path = DATA_PATH) -> list[dict[str, str]]:
    """读取并校验 JSONL，避免格式错误或重复来源进入数据库。"""
    entries: list[dict[str, str]] = []
    seen_keys: set[str] = set()

    with path.open("r", encoding="utf-8") as source_file:
        for line_number, raw_line in enumerate(source_file, 1):
            if not raw_line.strip():
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是有效 JSON") from exc

            if not isinstance(entry, dict) or set(entry) != EXPECTED_FIELDS:
                raise ValueError(f"{path}:{line_number} 字段必须恰好为 {sorted(EXPECTED_FIELDS)}")
            if any(not isinstance(entry[field], str) or not entry[field].strip() for field in EXPECTED_FIELDS):
                raise ValueError(f"{path}:{line_number} 字段值必须是非空字符串")
            if entry["kind"] not in ALLOWED_KINDS:
                raise ValueError(f"{path}:{line_number} kind 必须是 {sorted(ALLOWED_KINDS)} 之一")
            if len(entry["content"].strip()) > 300:
                raise ValueError(f"{path}:{line_number} content 超过 300 字")
            if entry["source_key"] in seen_keys:
                raise ValueError(f"{path}:{line_number} source_key 重复：{entry['source_key']}")

            seen_keys.add(entry["source_key"])
            entries.append(entry)

    if not entries:
        raise ValueError(f"{path} 没有可导入的知识条目")
    return entries


def import_entries(path: Path = DATA_PATH) -> int:
    """为每条角色资料生成本地 embedding，并按来源键幂等写入。"""
    entries = load_entries(path)
    for entry in entries:
        storage.upsert_role_knowledge(
            entry["source_key"],
            entry["kind"],
            entry["content"],
            memory_service.embed(entry["content"]),
        )
    return len(entries)


def main() -> None:
    parser = argparse.ArgumentParser(description="导入妃愛的原作角色知识")
    parser.add_argument("path", nargs="?", type=Path, default=DATA_PATH, help="JSONL 文件路径")
    args = parser.parse_args()
    count = import_entries(args.path)
    print(f"已导入或更新 {count} 条角色知识。")


if __name__ == "__main__":
    main()
