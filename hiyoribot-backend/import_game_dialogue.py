"""从用户本机的汉化 PFS 包导入共通线与妃爱线台词。

用法：python import_game_dialogue.py E:\\path\\to\\hamidashi.pfs.099
源文件只读，原话写入本机数据库，不复制到项目目录。
"""

import argparse
import hashlib
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import memory_service
import storage

ENTRY_RE = re.compile(r"^\[(\d+)\]=\{", re.MULTILINE)
SPEAKER_RE = re.compile(r'^name=\{name=("(?:[^"\\]|\\.)*")', re.MULTILINE)
EXPECTED_SCRIPTS = 87
EXPECTED_SLOTS = 14_195


@dataclass(frozen=True)
class Line:
    script_name: str
    entry_no: int
    scope: str
    speaker: str | None
    content: str


def selected_script(name: str) -> bool:
    """仅收录明确选定的两个线路前缀，排除额外场景文件。"""
    return name.startswith("script\\") and name.endswith(".ast") and (
        name.removeprefix("script\\").startswith(("共通-", "妃愛-"))
    )


def read_archive(path: Path) -> tuple[str, list[tuple[str, bytes]]]:
    data = path.read_bytes()
    if data[:3] != b"pf6":
        raise ValueError("需要汉化包内的 PF6 文件（hamidashi.pfs.099）")
    index_size, count = struct.unpack_from("<II", data, 3)
    if not (0 < count < 100_000 and 11 <= index_size + 7 <= len(data)):
        raise ValueError("PFS 索引无效")
    cursor = 11
    scripts = []
    for _ in range(count):
        name_length = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4
        name = data[cursor:cursor + name_length].decode("utf-8")
        cursor += name_length
        _reserved, offset, size = struct.unpack_from("<III", data, cursor)
        cursor += 12
        if offset + size > len(data):
            raise ValueError(f"PFS 条目越界：{name}")
        if selected_script(name):
            scripts.append((name.removeprefix("script\\"), data[offset:offset + size]))
    # PF6 索引在文件目录后还有一段附加表，目录结束位置无需等于数据起点。
    if cursor > index_size + 7:
        raise ValueError("PFS 索引长度不一致")
    if len(scripts) != EXPECTED_SCRIPTS:
        raise ValueError(f"线路脚本数不符：预期 {EXPECTED_SCRIPTS}，实际 {len(scripts)}")
    return hashlib.sha256(data).hexdigest(), scripts


def parse_script(script_name: str, data: bytes) -> tuple[int, list[Line]]:
    text = data.decode("utf-8-sig")
    start = text.find("\ntext={")
    end = text.find("\nlabel={", start)
    if start < 0 or end < 0:
        raise ValueError(f"找不到文本区：{script_name}")
    section = text[start:end]
    matches = list(ENTRY_RE.finditer(section))
    scope = "common" if script_name.startswith("共通-") else "hiyori"
    lines = []
    for index, match in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        block = section[match.end():block_end]
        speaker_match = SPEAKER_RE.search(block)
        speaker = json.loads(speaker_match.group(1), strict=False) if speaker_match else None
        parts = []
        for raw_line in block.splitlines():
            value = raw_line.strip()
            if value.startswith('"') and value.endswith('",'):
                # 汉化脚本有一处未转义的制表符，strict=False 可忠实读取。
                parts.append(json.loads(value[:-1], strict=False))
        content = "\n".join(parts).strip()
        if content:
            lines.append(Line(script_name, int(match.group(1)), scope, speaker, content))
    return len(matches), lines


def chunk_lines(lines: list[Line]) -> list[tuple[int, int, str, str, str]]:
    chunks = []
    current: list[Line] = []
    current_size = 0

    def flush() -> None:
        if current:
            content = "\n".join(
                f"{line.speaker or '旁白'}：{line.content}" for line in current
            )
            chunks.append((current[0].entry_no, current[-1].entry_no,
                           current[0].script_name, current[0].scope, content))

    for line in lines:
        size = len(line.content) + len(line.speaker or "旁白") + 2
        if current and (len(current) >= 6 or current_size + size > 350):
            flush()
            current = []
            current_size = 0
        current.append(line)
        current_size += size
    flush()
    return chunks


def import_archive(path: Path, dry_run: bool = False) -> dict[str, int | str]:
    source_hash, scripts = read_archive(path)
    parsed = []
    slots = 0
    line_count = 0
    chunk_count = 0
    for script_name, data in scripts:
        script_slots, lines = parse_script(script_name, data)
        chunks = chunk_lines(lines)
        slots += script_slots
        line_count += len(lines)
        chunk_count += len(chunks)
        parsed.append((script_name, lines, chunks))
    if slots != EXPECTED_SLOTS or line_count != EXPECTED_SLOTS - 2:
        raise ValueError(f"文本覆盖不完整：{slots} 个槽位，{line_count} 条正文")

    if not dry_run:
        embedder = memory_service.get_embedder()
        with storage.connect() as conn:
            for script_name, lines, chunks in parsed:
                # 同一脚本重新导入时先清旧数据，避免分块边界变化留下旧索引。
                conn.execute("DELETE FROM game_dialogue_chunk WHERE script_name = %s", (script_name,))
                conn.execute("DELETE FROM game_dialogue_line WHERE script_name = %s", (script_name,))
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO game_dialogue_line
                           (script_name, entry_no, scope, speaker, content, source_hash)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        [(line.script_name, line.entry_no, line.scope, line.speaker,
                          line.content, source_hash) for line in lines],
                    )
                    texts = [chunk[4] for chunk in chunks]
                    vectors = list(embedder.embed(texts, batch_size=32))
                    if len(vectors) != len(chunks):
                        raise RuntimeError(f"向量数量不符：{script_name}")
                    cur.executemany(
                        """INSERT INTO game_dialogue_chunk
                           (first_entry, last_entry, script_name, scope, content, embedding)
                           VALUES (%s, %s, %s, %s, %s, %s::vector)""",
                        [(first, last, name, scope, content,
                          storage.vector_literal(vector.tolist()))
                         for (first, last, name, scope, content), vector in zip(chunks, vectors)],
                    )
            conn.commit()
    return {"scripts": len(scripts), "slots": slots, "lines": line_count,
            "chunks": chunk_count, "source_sha256": source_hash}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="汉化 PFS 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="仅检查，不写入数据库")
    args = parser.parse_args()
    print(json.dumps(import_archive(args.archive, args.dry_run), ensure_ascii=False))
