"""调用官方 GPT-SoVITS v2Pro 脚本，完成本机 30 分钟试训。

语音和权重都放在 --dataset 指定的仓库外目录。可用 --stage 单独重跑某阶段。
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import yaml


EXPERIMENT = "hiyori_pilot_30m"
COMPAT_DIR = Path(__file__).with_name("tts_compat")


def run(python: Path, script: Path, root: Path, environment: dict, *args: str) -> None:
    print(f"运行：{script.name}", flush=True)
    environment = environment.copy()
    # Windows PowerShell 重定向默认可能用 GBK，Rich 进度条含 Unicode 字符。
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment["PATH"] = os.pathsep.join((
        str(python.parent / "Library" / "bin"), str(python.parent / "Scripts"),
        environment.get("PATH", ""),
    ))
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(COMPAT_DIR), str(root / "GPT_SoVITS"), str(root / "tools"), str(root),
        environment.get("PYTHONPATH", ""),
    )))
    subprocess.run(
        [str(python), "-s", str(script), *args], cwd=root, env=environment, check=True
    )


def require_count(path: Path, pattern: str, expected: int) -> None:
    actual = len(list(path.glob(pattern)))
    if actual != expected:
        raise RuntimeError(f"{path} 中 {pattern} 应有 {expected} 个，实际 {actual} 个")


def preprocess(root: Path, dataset: Path, python: Path, count: int) -> None:
    exp = dataset / "experiment"
    exp.mkdir(exist_ok=True)
    models = root / "GPT_SoVITS" / "pretrained_models"
    base = root / "GPT_SoVITS" / "prepare_datasets"
    environment = os.environ.copy()
    environment.update({
        "inp_text": str(dataset / "train.list"), "inp_wav_dir": "",
        "exp_name": EXPERIMENT, "opt_dir": str(exp), "i_part": "0", "all_parts": "1",
        "_CUDA_VISIBLE_DEVICES": "0", "is_half": "True", "version": "v2Pro",
        "bert_pretrained_dir": str(models / "chinese-roberta-wwm-ext-large"),
        "cnhubert_base_dir": str(models / "chinese-hubert-base"),
        "sv_path": str(models / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt"),
        "pretrained_s2G": str(models / "v2Pro" / "s2Gv2Pro.pth"),
        "s2config_path": str(root / "GPT_SoVITS" / "configs" / "s2v2Pro.json"),
    })
    text_path = exp / "2-name2text.txt"
    if not text_path.exists():
        run(python, base / "1-get-text.py", root, environment)
        (exp / "2-name2text-0.txt").replace(text_path)
    if len(text_path.read_text(encoding="utf-8").splitlines()) != count:
        raise RuntimeError("日语文本前端丢失了训练台词")

    if len(list((exp / "4-cnhubert").glob("*.pt"))) != count:
        run(python, base / "2-get-hubert-wav32k.py", root, environment)
    require_count(exp / "4-cnhubert", "*.pt", count)
    require_count(exp / "5-wav32k", "*.wav", count)

    if len(list((exp / "7-sv_cn").glob("*.pt"))) != count:
        run(python, base / "2-get-sv.py", root, environment)
    require_count(exp / "7-sv_cn", "*.pt", count)

    semantic_path = exp / "6-name2semantic.tsv"
    if not semantic_path.exists():
        run(python, base / "3-get-semantic.py", root, environment)
        partial = (exp / "6-name2semantic-0.tsv").read_text(encoding="utf-8").strip()
        semantic_path.write_text("item_name\tsemantic_audio\n" + partial + "\n", encoding="utf-8")
    if len(semantic_path.read_text(encoding="utf-8").splitlines()) - 1 != count:
        raise RuntimeError("语义 token 提取数量与训练集不一致")


def train_sovits(root: Path, dataset: Path, python: Path) -> None:
    exp = dataset / "experiment"
    models = root / "GPT_SoVITS" / "pretrained_models"
    config = json.loads((root / "GPT_SoVITS" / "configs" / "s2v2Pro.json").read_text())
    train = config["train"]
    train.update({
        "batch_size": 1, "epochs": 4, "fp16_run": True,
        "text_low_lr_rate": 0.4, "if_save_latest": False,
        "if_save_every_weights": True, "save_every_epoch": 2,
        "gpu_numbers": "0", "grad_ckpt": False,
        "pretrained_s2G": str(models / "v2Pro" / "s2Gv2Pro.pth"),
        "pretrained_s2D": str(models / "v2Pro" / "s2Dv2Pro.pth"),
    })
    weight_dir = dataset / "weights" / "sovits"
    weight_dir.mkdir(parents=True, exist_ok=True)
    (exp / "logs_s2_v2Pro").mkdir(exist_ok=True)
    config["data"]["exp_dir"] = config["s2_ckpt_dir"] = str(exp)
    config["model"]["version"] = config["version"] = "v2Pro"
    config["save_weight_dir"] = str(weight_dir)
    config["name"] = EXPERIMENT
    path = dataset / "sovits_config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if not list(weight_dir.glob("*_e4_*.pth")):
        run(python, root / "GPT_SoVITS" / "s2_train.py", root, os.environ.copy(),
            "--config", str(path))
    if not list(weight_dir.glob("*_e4_*.pth")):
        raise RuntimeError("SoVITS 第 4 轮权重未生成")


def train_gpt(root: Path, dataset: Path, python: Path) -> None:
    exp = dataset / "experiment"
    models = root / "GPT_SoVITS" / "pretrained_models"
    config = yaml.safe_load((root / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml")
                            .read_text(encoding="utf-8"))
    weight_dir = dataset / "weights" / "gpt"
    weight_dir.mkdir(parents=True, exist_ok=True)
    config["train"].update({
        "batch_size": 1, "epochs": 5, "save_every_n_epoch": 1,
        "if_save_every_weights": True, "if_save_latest": False,
        "if_dpo": False, "half_weights_save_dir": str(weight_dir),
        "exp_name": EXPERIMENT,
    })
    config.update({
        "pretrained_s1": str(models / "s1v3.ckpt"),
        "train_semantic_path": str(exp / "6-name2semantic.tsv"),
        "train_phoneme_path": str(exp / "2-name2text.txt"),
        "output_dir": str(exp / "logs_s1_v2Pro"),
    })
    path = dataset / "gpt_config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    environment = os.environ.copy()
    environment.update({"_CUDA_VISIBLE_DEVICES": "0", "hz": "25hz"})
    if not list(weight_dir.glob("*e5*.ckpt")):
        run(python, root / "GPT_SoVITS" / "s1_train.py", root, environment,
            "--config_file", str(path))
    if not list(weight_dir.glob("*e5*.ckpt")):
        raise RuntimeError("GPT 第 5 轮权重未生成")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpt-root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--stage", choices=("all", "preprocess", "sovits", "gpt"), default="all")
    args = parser.parse_args()
    root, dataset = args.gpt_root.resolve(), args.dataset.resolve()
    count = len((dataset / "train.list").read_text(encoding="utf-8").splitlines())
    if args.stage in ("all", "preprocess"):
        preprocess(root, dataset, args.python.resolve(), count)
    if args.stage in ("all", "sovits"):
        train_sovits(root, dataset, args.python.resolve())
    if args.stage in ("all", "gpt"):
        train_gpt(root, dataset, args.python.resolve())
