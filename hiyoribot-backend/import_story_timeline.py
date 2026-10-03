"""只读 PF8 脚本的控制区，导入共通线、妃爱线的实际跳转图。"""

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path

import storage
from import_game_dialogue import selected_script


def table_end(text: str, start: int) -> int:
    depth, quoted, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("脚本控制表缺少闭合括号")


def transitions(text: str) -> list[dict]:
    """只识别明确 file 目标；内部 label 原样保留，不执行游戏条件表达式。"""
    control = text.split("\ntext={", 1)[0]
    result = []
    for match in re.finditer(r'\{\s*"(excall|select)"', control):
        stop = table_end(control, match.start())
        command = control[match.start():stop]
        target = re.search(r'\bfile=("(?:[^"\\]|\\.)*")', command)
        if not target:
            continue
        target_name = json.loads(target.group(1))
        if not target_name.endswith(".ast"):
            target_name += ".ast"
        entry = re.search(r'lang="lang_(\d+)"', control[stop:])
        if not entry:
            raise ValueError("无法确定脚本跳转发生位置")
        condition = re.search(r'\bcond=("(?:[^"\\]|\\.)*")', command)
        label = re.search(r'\blabel=("(?:[^"\\]|\\.)*")', command)
        result.append({"target_script": target_name, "source_entry": int(entry.group(1)),
                       "condition": json.loads(condition.group(1)) if condition else "",
                       "target_label": json.loads(label.group(1)) if label else ""})
    return result


def read_scripts(path: Path) -> tuple[str, dict[str, str]]:
    with path.open("rb") as stream:
        header = stream.read(7)
        if header[:3] != b"pf8":
            raise ValueError("需要原版 PF8 root.pfs，不能传汉化外层容器")
        index_size = struct.unpack_from("<I", header, 3)[0]
        if not 4 <= index_size <= 16 * 1024 * 1024:
            raise ValueError("PF8 索引大小异常")
        index = stream.read(index_size)
        count = struct.unpack_from("<I", index)[0]
        if not 0 < count < 100_000:
            raise ValueError("PF8 条目数异常")
        key, cursor, scripts = hashlib.sha1(index).digest(), 4, {}
        for _ in range(count):
            length = struct.unpack_from("<I", index, cursor)[0]
            cursor += 4
            name = index[cursor:cursor + length].decode("utf-8")
            cursor += length
            _, offset, size = struct.unpack_from("<III", index, cursor)
            cursor += 12
            if not name.startswith("script\\") or not name.endswith(".ast"):
                continue
            if offset + size > path.stat().st_size:
                raise ValueError("PF8 脚本条目越界")
            stream.seek(offset)
            raw = stream.read(size)
            scripts[name.removeprefix("script\\")] = bytes(
                value ^ key[position % len(key)] for position, value in enumerate(raw)
            ).decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "")
        stream.seek(0)
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return source_hash, scripts


def build_graph(scripts: dict[str, str]) -> tuple[list[dict], list[dict], list[str]]:
    selected = {name for name in scripts if selected_script("script\\" + name)}
    nodes, edges, warnings = [], [], []
    jumps = {name: transitions(text) for name, text in scripts.items()}
    for name in sorted(selected):
        entries = re.findall(r'^\[(\d+)\]=\{', scripts[name], re.MULTILINE)
        if not entries:
            raise ValueError(f"章节没有台词序号：{name}")
        control = scripts[name].split("\ntext={", 1)[0]
        local_entries = []
        for match in re.finditer(r'\{\s*"excall"', control):
            stop = table_end(control, match.start())
            command = control[match.start():stop]
            if re.search(r'\blabel=', command) and not re.search(r'\bfile=', command):
                entry = re.search(r'lang="lang_(\d+)"', control[stop:])
                local_entries.append(int(entry.group(1)) if entry else 0)
        uncertain = min(local_entries) if local_entries else None
        if uncertain is not None:
            warnings.append(f"内部标签跳转需核对：{name}，#{uncertain} 及之后保守排除")
        nodes.append({"script_name": name, "scope": "common" if name.startswith("共通-") else "hiyori",
                      "max_entry": max(map(int, entries)), "uncertain_from": uncertain})
        for jump in jumps[name]:
            pending = [(jump["target_script"], [], [jump["condition"]])]
            while pending:
                target, via, conditions = pending.pop()
                if target in selected:
                    if jump["target_label"]:
                        warnings.append(f"带入口标签的跳转需核对：{name} -> {target}")
                        continue
                    edges.append({"source_script": name, "source_entry": jump["source_entry"],
                                  "target_script": target,
                                  "condition": " && ".join(f"({c})" for c in conditions if c),
                                  "via": " -> ".join(via)})
                elif target not in via and len(via) < 20:
                    # 未收录场景只看跳转，不导入其台词；保留桥接证据。
                    for bridge in jumps.get(target, []):
                        if not bridge["target_label"]:
                            pending.append((bridge["target_script"], via + [target],
                                            conditions + [bridge["condition"]]))
    unique = {tuple(edge.values()): edge for edge in edges}
    return nodes, list(unique.values()), warnings


def import_timeline(path: Path, dry_run: bool = False) -> dict:
    source_hash, scripts = read_scripts(path)
    nodes, edges, warnings = build_graph(scripts)
    if len(nodes) != 87:
        raise ValueError(f"章节覆盖不符：预期 87，实际 {len(nodes)}")
    if not dry_run:
        with storage.connect() as conn:
            existing = {row["script_name"] for row in conn.execute(
                "SELECT DISTINCT script_name FROM game_dialogue_line").fetchall()}
            if existing != {row["script_name"] for row in nodes}:
                raise ValueError("剧情库与跳转图的脚本集合不同，请核对来源版本")
            # 只替换这两条路线的元数据，原台词与向量保持不变。
            conn.execute("DELETE FROM story_edge")
            conn.execute("DELETE FROM story_chapter")
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO story_chapter (script_name,scope,max_entry,source_hash,uncertain_from) VALUES (%s,%s,%s,%s,%s)",
                                [(n["script_name"], n["scope"], n["max_entry"], source_hash, n["uncertain_from"]) for n in nodes])
                cur.executemany("INSERT INTO story_edge VALUES (%s, %s, %s, %s, %s)",
                                [tuple(e.values()) for e in edges])
    return {"chapters": len(nodes), "edges": len(edges), "source_sha256": source_hash,
            "warnings": sorted(set(warnings))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(import_timeline(args.archive, args.dry_run), ensure_ascii=False, indent=2))
