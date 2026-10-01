"""从本机游戏包导出妃爱日语 TTS 试训集；原始语音不进入仓库。

运行示例：
python tools/tts_pilot_dataset.py --script-pfs E:/game/root.pfs \
    --game-rar E:/game/game.rar --output E:/private/hiyori_tts/pilot
依赖：numpy、soundfile，以及本机 7-Zip。输出目录必须在本仓库之外且为空。
"""

import argparse
import hashlib
import io
import json
import re
import struct
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf


SCRIPT_RE = re.compile(r"^\[(\d+)\]=\{", re.MULTILINE)
VOICE_RE = re.compile(r'file="(fem_hiy_[^"]+)"')
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Utterance:
    voice_id: str
    script: str
    scope: str
    entry: int
    text: str


def entries(index: bytes) -> list[tuple[str, int, int]]:
    """读取 Artemis PF8 索引；offset 是归档中的绝对偏移。"""
    count = struct.unpack_from("<I", index)[0]
    if not 0 < count < 100_000:
        raise ValueError("PFS 条目数异常")
    cursor = 4
    result = []
    for _ in range(count):
        length = struct.unpack_from("<I", index, cursor)[0]
        cursor += 4
        name = index[cursor:cursor + length].decode("utf-8")
        cursor += length
        _reserved, offset, size = struct.unpack_from("<III", index, cursor)
        cursor += 12
        result.append((name, offset, size))
    if cursor > len(index):
        raise ValueError("PFS 索引越界")
    return result


def decrypt(raw: bytes, key: bytes) -> bytes:
    values = np.frombuffer(raw, dtype=np.uint8).copy()
    values ^= np.resize(np.frombuffer(key, dtype=np.uint8), values.size)
    return values.tobytes()


def is_selected_script(name: str) -> bool:
    return name.startswith("script\\") and name.endswith(".ast") and (
        name.removeprefix("script\\").startswith(("共通-", "妃愛-"))
    )


def parse_script(name: str, raw: bytes) -> list[Utterance]:
    text = raw.decode("utf-8-sig")
    start, end = text.find("\ntext={"), text.find("\nlabel={")
    if start < 0 or end <= start:
        raise ValueError(f"脚本缺少文本区：{name}")
    section = text[start:end]
    matches = list(SCRIPT_RE.finditer(section))
    scope = "common" if name.startswith("共通-") else "hiyori"
    result = []
    for index, match in enumerate(matches):
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        block = section[match.end():stop]
        voice_ids = VOICE_RE.findall(block)
        if not voice_ids:
            continue
        parts = []
        for line in block.splitlines():
            value = line.strip()
            if value.startswith('"') and value.endswith('",'):
                parts.append(json.loads(value[:-1], strict=False))
        spoken = "".join(parts).strip()
        if not spoken or len(voice_ids) != 1 or "|" in spoken:
            raise ValueError(f"语音与台词无法一一对应：{name} #{match.group(1)}")
        result.append(Utterance(voice_ids[0], name, scope, int(match.group(1)), spoken))
    return result


def read_scripts(path: Path) -> dict[str, Utterance]:
    with path.open("rb") as source:
        header = source.read(7)
        if header[:3] != b"pf8":
            raise ValueError("原版 root.pfs 不是 PF8")
        index_size = struct.unpack_from("<I", header, 3)[0]
        index = source.read(index_size)
        key = hashlib.sha1(index).digest()
        results = {}
        script_count = 0
        for name, offset, size in entries(index):
            if not is_selected_script(name):
                continue
            script_count += 1
            source.seek(offset)
            script = name.removeprefix("script\\")
            for item in parse_script(script, decrypt(source.read(size), key)):
                if item.voice_id in results:
                    raise ValueError(f"重复语音 ID：{item.voice_id}")
                results[item.voice_id] = item
    if script_count != 87 or len(results) != 2081:
        raise ValueError(f"脚本或语音数量不符：{script_count} 个脚本、{len(results)} 段语音")
    return results


def read_exact(stream, count: int, keep: bool = False) -> bytes:
    parts = []
    while count:
        chunk = stream.read(min(count, 4 * 1024 * 1024))
        if not chunk:
            raise EOFError("游戏语音包提前结束")
        if keep:
            parts.append(chunk)
        count -= len(chunk)
    return b"".join(parts) if keep else b""


def stream_voice_archive(rar: Path, sevenzip: Path, wanted: set[str]):
    """7-Zip 标准输出只读流；不把 1.9GB 的语音包解压到仓库。"""
    command = [str(sevenzip), "e", "-so", "-r", str(rar), "*.pfs.001"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        header = read_exact(process.stdout, 11, True)
        if header[:3] != b"pf8":
            raise ValueError("没有找到 root.pfs.001 PF8 语音包")
        index_size = struct.unpack_from("<I", header, 3)[0]
        index = header[7:] + read_exact(process.stdout, index_size - 4, True)
        key = hashlib.sha1(index).digest()
        selected = []
        for name, offset, size in entries(index):
            if name.lower().endswith(".ogg") and Path(name).stem in wanted:
                selected.append((offset, size, Path(name).stem))
        if len(selected) != len(wanted):
            raise ValueError(f"语音文件不齐：需要 {len(wanted)}，找到 {len(selected)}")
        position = 7 + index_size
        for offset, size, voice_id in sorted(selected):
            read_exact(process.stdout, offset - position)
            raw = read_exact(process.stdout, size, True)
            position = offset + size
            yield voice_id, decrypt(raw, key)
    finally:
        process.terminate()
        process.stdout.close()
        process.wait(timeout=10)


def stable_order(value: str) -> str:
    return hashlib.sha256(("hiyori-pilot-v1:" + value).encode()).hexdigest()


def pick_diverse(rows: list[dict], seconds: float) -> list[dict]:
    """按脚本轮流取样，避免三十分钟全来自少数相邻场景。"""
    groups = defaultdict(list)
    for row in rows:
        groups[row["script"]].append(row)
    for group in groups.values():
        group.sort(key=lambda row: stable_order(row["voice_id"]))
    result, total = [], 0.0
    scripts = sorted(groups, key=stable_order)
    while total < seconds and any(groups.values()):
        for script in scripts:
            if groups[script]:
                row = groups[script].pop()
                result.append(row)
                total += row["duration"]
                if total >= seconds:
                    break
    if total < seconds:
        raise ValueError(f"有效语音只有 {total:.1f} 秒，不足 {seconds:.1f} 秒")
    return result


def choose_split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    train, test = [], []
    for scope in ("common", "hiyori"):
        scoped = [row for row in rows if row["scope"] == scope]
        groups = defaultdict(list)
        for row in scoped:
            groups[row["script"]].append(row)
        held_scripts = set()
        available = 0.0
        for script in sorted(groups, key=lambda value: stable_order("test:" + value)):
            held_scripts.add(script)
            available += sum(row["duration"] for row in groups[script])
            if available >= 150:
                break
        test.extend(pick_diverse(
            [row for row in scoped if row["script"] in held_scripts], 150
        ))
        train.extend(pick_diverse(
            [row for row in scoped if row["script"] not in held_scripts], 900
        ))
    assert not {row["script"] for row in train} & {row["script"] for row in test}
    return train, test


def collect_metrics(rar: Path, sevenzip: Path, utterances: dict[str, Utterance]) -> tuple[list[dict], Counter]:
    candidates = []
    excluded = Counter()
    for voice_id, ogg in stream_voice_archive(rar, sevenzip, set(utterances)):
        signal, rate = sf.read(io.BytesIO(ogg), dtype="float32", always_2d=True)
        mono = signal.mean(axis=1)
        duration = len(mono) / rate
        rms = float(np.sqrt(np.mean(mono * mono)))
        quiet = float(np.mean(np.abs(mono) < 0.003))
        if not 2 <= duration <= 12:
            excluded["duration_outside_2_12s"] += 1
            continue
        if not np.isfinite(rms) or rms < 0.006 or quiet > 0.75:
            excluded["low_energy_or_silence"] += 1
            continue
        item = utterances[voice_id]
        candidates.append({"voice_id": voice_id, "script": item.script,
                           "scope": item.scope, "entry": item.entry,
                           "text": item.text, "duration": duration,
                           "rms": rms, "quiet_fraction": quiet})
    return candidates, excluded


def build_dataset(script_pfs: Path, rar: Path, output: Path, sevenzip: Path) -> dict:
    output = output.resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("输出必须位于公开仓库之外")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"输出目录非空：{output}")
    utterances = read_scripts(script_pfs)
    candidates, excluded = collect_metrics(rar, sevenzip, utterances)
    train, test = choose_split(candidates)
    selected = {row["voice_id"]: (split, row)
                for split, rows in (("train", train), ("test", test)) for row in rows}
    output.mkdir(parents=True, exist_ok=True)
    for split in ("train", "test"):
        (output / split).mkdir()
    for voice_id, ogg in stream_voice_archive(rar, sevenzip, set(selected)):
        split, row = selected[voice_id]
        signal, rate = sf.read(io.BytesIO(ogg), dtype="float32", always_2d=True)
        wav_path = output / split / f"{voice_id}.wav"
        sf.write(wav_path, signal.mean(axis=1), rate, subtype="PCM_16")
        row["wav"] = str(wav_path)
    for split, rows in (("train", train), ("test", test)):
        with (output / f"{split}.jsonl").open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "train.list").open("w", encoding="utf-8") as file:
        for row in train:
            file.write(f"{row['wav']}|hiyori|ja|{row['text']}\n")
    with script_pfs.open("rb") as source:
        source_hash = hashlib.file_digest(source, "sha256").hexdigest()
    summary = {
        "source_script_pfs_sha256": source_hash,
        "scope": "common and hiyori main scripts only",
        "matched_voice_lines": len(utterances),
        "valid_candidates": len(candidates),
        "excluded": dict(excluded),
        "train_clips": len(train), "train_seconds": round(sum(x["duration"] for x in train), 1),
        "test_clips": len(test), "test_seconds": round(sum(x["duration"] for x in test), 1),
        "train_scripts": len({x["script"] for x in train}),
        "test_scripts": sorted({x["script"] for x in test}),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script-pfs", type=Path, required=True)
    parser.add_argument("--game-rar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sevenzip", type=Path, default=Path(r"C:\Program Files\7-Zip\7z.exe"))
    args = parser.parse_args()
    print(json.dumps(build_dataset(args.script_pfs, args.game_rar, args.output, args.sevenzip),
                     ensure_ascii=False, indent=2))
