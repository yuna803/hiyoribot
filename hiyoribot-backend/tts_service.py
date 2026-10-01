"""按需把助手回复转成妃爱的日语配音，缓存留在仓库外。"""

import hashlib
import json
import logging
import os
import re
import subprocess
import wave
from pathlib import Path
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException
from openai import APITimeoutError, OpenAI, OpenAIError

_lock = Lock()  # 单张显卡一次只运行一个合成进程。
_log = logging.getLogger(__name__)
_worker = Path(__file__).with_name("tts_worker.py")


def _dataset(tts_home: str) -> tuple[Path, Path]:
    if not tts_home:
        raise HTTPException(status_code=503, detail="尚未配置本机 TTS 目录")
    home = Path(tts_home).expanduser().resolve()
    dataset = home / "pilot_v1"
    if not (dataset / "active_model.json").is_file():
        raise HTTPException(status_code=503, detail="未找到已选定的 TTS 模型")
    return home, dataset


def _spoken_japanese(text: str, config) -> str:
    """只取回复中适合角色说出口的内容；思考文本不进入这里。"""
    prompt = (
        "你为和泉妃爱的配音准备台词。输入是她对哥哥智宏的一段中文回复。"
        "只提取她实际说出口的内容，删掉动作、旁白、Markdown 和舞台说明；"
        "最多选最重要的一两句，不要补写原文没有的话。"
        "翻译成自然、符合角色口吻的日语，最多 120 个日文字符。"
        "只输出日语台词，不加引号、说明或前缀。若完全没有可说的台词，只输出 NONE。"
    )
    try:
        with OpenAI(api_key=config.api_key, base_url=config.base_url,
                    timeout=30, max_retries=0) as client:
            result = client.chat.completions.create(
                model=config.model,
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": text}],
                max_tokens=240,
                extra_body={"thinking": {"type": "disabled"}},
            )
    except APITimeoutError as exc:
        raise HTTPException(status_code=504, detail="DeepSeek 翻译超时，请稍后重试") from exc
    except OpenAIError as exc:
        raise HTTPException(status_code=502, detail="日语台词翻译失败") from exc
    choice = result.choices[0] if result.choices else None
    japanese = (choice.message.content or "").strip() if choice else ""
    if japanese.upper() == "NONE":
        raise HTTPException(status_code=422, detail="这条回复没有可配音的台词")
    if not japanese or len(japanese) > 180 or getattr(choice, "finish_reason", None) not in (None, "stop"):
        raise HTTPException(status_code=502, detail="日语台词生成不完整")
    return japanese


def _synthesize(japanese: str, output: Path, home: Path) -> None:
    python = home / "env" / "python.exe"
    gpt_root = home / "GPT-SoVITS"
    if not python.is_file() or not gpt_root.is_dir():
        raise HTTPException(status_code=503, detail="本机 TTS 环境不完整")
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment["PATH"] = os.pathsep.join((str(python.parent / "Library" / "bin"),
                                            environment.get("PATH", "")))
    try:
        result = subprocess.run(
            [str(python), str(_worker), "--home", str(home), "--output", str(output)],
            input=japanese, text=True, encoding="utf-8", capture_output=True,
            cwd=gpt_root, env=environment, timeout=240, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("TTS 进程无法完成：%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="本机配音生成超时或无法启动") from exc
    if result.returncode != 0:
        _log.error("TTS 进程失败，退出码 %s，错误末尾：%s",
                   result.returncode, result.stderr[-1000:])
        raise HTTPException(status_code=502, detail="本机配音生成失败")
    try:
        with wave.open(str(output), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if not 0.3 <= duration <= 90:
                raise ValueError("生成音频时长异常")
    except (OSError, ValueError, wave.Error) as exc:
        raise HTTPException(status_code=502, detail="配音文件无效") from exc


def create_speech(text: str, config) -> dict[str, str]:
    home, dataset = _dataset(config.tts_home)
    selected = (dataset / "active_model.json").read_bytes()
    key = hashlib.sha256(selected + b"\0" + text.encode("utf-8")).hexdigest()
    cache = dataset / "web_audio"
    audio = cache / f"{key}.wav"
    metadata = cache / f"{key}.json"
    with _lock:
        if audio.is_file() and metadata.is_file():
            japanese = json.loads(metadata.read_text(encoding="utf-8"))["japanese"]
        else:
            japanese = _spoken_japanese(text, config)
            cache.mkdir(exist_ok=True)
            temporary = cache / f"{key}.{uuid4().hex}.wav"
            try:
                _synthesize(japanese, temporary, home)
                os.replace(temporary, audio)
                metadata.write_text(json.dumps({"japanese": japanese}, ensure_ascii=False),
                                    encoding="utf-8")
            finally:
                temporary.unlink(missing_ok=True)
    return {"japanese": japanese, "audio_url": f"/tts/audio/{key}"}


def cached_audio(key: str, tts_home: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise HTTPException(status_code=404, detail="配音不存在")
    _home, dataset = _dataset(tts_home)
    path = dataset / "web_audio" / f"{key}.wav"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="配音不存在")
    return path
