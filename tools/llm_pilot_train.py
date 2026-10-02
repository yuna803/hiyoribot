"""在独立 WSL 环境做 QLoRA 显存检查与两轮角色试训。"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def train(args):
    # 必须先导入 Unsloth，随后才导入 Transformers/TRL。
    from unsloth import FastLanguageModel
    from unsloth.chat_templates import train_on_responses_only
    import torch
    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer

    if not torch.cuda.is_available():
        raise RuntimeError("训练环境未识别 CUDA，停止试训")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=str(args.model), max_seq_length=args.length,
        load_in_4bit=True, local_files_only=True,
    )
    model = FastLanguageModel.get_peft_model(
        model, r=8, lora_alpha=16, lora_dropout=0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        bias="none", use_gradient_checkpointing="unsloth", random_state=42,
    )
    counts = {}

    def read_dataset(name):
        rows, skipped = [], 0
        for line in (args.dataset / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            text = tokenizer.apply_chat_template(row["messages"], tokenize=False, add_generation_prompt=False)
            # 不截断目标台词；超过试训长度的样本记录后跳过。
            if len(tokenizer(text, add_special_tokens=False)["input_ids"]) > args.length:
                skipped += 1
                continue
            rows.append({"text": text})
        counts[name] = {"used": len(rows), "too_long": skipped}
        if not rows:
            raise RuntimeError(f"{name} 没有符合长度的训练样本")
        return Dataset.from_list(rows)

    training, validation = read_dataset("train"), read_dataset("validation")
    smoke = args.stage == "smoke"
    output = args.dataset / (f"smoke_{args.length}" if smoke else "training")
    trainer = SFTTrainer(
        model=model, processing_class=tokenizer, train_dataset=training, eval_dataset=validation,
        args=SFTConfig(
            output_dir=str(output), per_device_train_batch_size=1,
            per_device_eval_batch_size=1, gradient_accumulation_steps=8,
            learning_rate=1e-4, num_train_epochs=2, max_steps=20 if smoke else -1,
            warmup_ratio=0.05, logging_steps=5, optim="adamw_8bit",
            bf16=torch.cuda.is_bf16_supported(), fp16=not torch.cuda.is_bf16_supported(),
            seed=42, dataset_text_field="text", max_length=args.length,
            packing=False, dataset_num_proc=1, report_to="none",
            save_strategy="no" if smoke else "epoch", eval_strategy="no" if smoke else "epoch",
            save_total_limit=2, dataloader_num_workers=0,
        ),
    )
    trainer = train_on_responses_only(
        trainer, instruction_part="<|im_start|>user\n", response_part="<|im_start|>assistant\n",
    )
    started = time.monotonic()
    stats = trainer.train()
    report = {"stage": args.stage, "sequence_length": args.length, "counts": counts,
              "gpu": torch.cuda.get_device_name(), "peak_allocated_gb": torch.cuda.max_memory_allocated() / 2**30,
              "peak_reserved_gb": torch.cuda.max_memory_reserved() / 2**30,
              "seconds": time.monotonic() - started, "metrics": stats.metrics,
              "config": trainer.args.to_dict(), "versions": {"torch": torch.__version__}}
    args.dataset.joinpath(f"{args.stage}_{args.length}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not smoke:
        model.save_pretrained(str(output / "final_adapter"))
        tokenizer.save_pretrained(str(output / "final_adapter"))
    print(json.dumps({key: report[key] for key in ("stage", "counts", "seconds", "peak_reserved_gb")}), flush=True)


def auto(args):
    # 独立进程退出才能完全释放上一轮检查的 CUDA 资源。
    def run(stage, length):
        return subprocess.run([sys.executable, str(Path(__file__).resolve()),
                               "--model", str(args.model), "--dataset", str(args.dataset),
                               "--stage", stage, "--length", str(length)], check=False).returncode
    for length in (1024, 512):
        result = run("smoke", length)
        if result == 42:
            continue
        if result:
            return result
        return run("train", length)
    print("两种长度的 20 步显存检查均失败，停止试训。", flush=True)
    return 42


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--stage", choices=("auto", "smoke", "train"), default="auto")
    parser.add_argument("--length", type=int, choices=(512, 1024), default=1024)
    args = parser.parse_args()
    args.model, args.dataset = args.model.resolve(), args.dataset.resolve()
    repo = Path(__file__).resolve().parents[1]
    if args.dataset.is_relative_to(repo):
        parser.error("数据、权重与训练输出必须放在仓库外")
    previous = args.dataset / "training"
    if args.stage in ("auto", "train") and previous.is_dir() and any(previous.iterdir()):
        parser.error("已有试训权重，请新建试验目录，避免覆盖本轮结果")
    os.chdir(args.dataset.parent)  # Unsloth 编译缓存也留在仓库外。
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("UNSLOTH_DISABLE_STATISTICS", "1")
    if args.stage == "auto":
        return auto(args)
    try:
        train(args)
    except Exception as exc:
        import torch
        if isinstance(exc, torch.cuda.OutOfMemoryError):
            args.dataset.joinpath(f"oom_{args.stage}_{args.length}.json").write_text(
                json.dumps({"stage": args.stage, "length": args.length, "reason": "CUDA out of memory"}), encoding="utf-8")
            return 42
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
