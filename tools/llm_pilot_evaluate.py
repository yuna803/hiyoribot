"""用相同量化与采样配置比较基础模型、早期和最终角色权重。"""

import argparse
import html
import json
import os
import re
import time
from pathlib import Path

PROMPTS = [
    "妃爱，今天录音累不累？我给你买了布丁。",
    "今晚我来做饭，你先休息。你想吃什么？",
    "我今天工作不太顺，想跟你待一会儿。",
    "我们换个新场景：周末在书店偶然看到一本奇怪的料理书。",
    "你一直提炒饭，我想聊点别的。最近有没有想尝试的新爱好？",
    "我忘了收衣服，但别替我说话或决定行动，你会怎么提醒我？",
    "如果我说喜欢你，你会怎么接话？自然一点，别长篇讲道理。",
    "在原作里你为什么加入学生会？不知道的细节不要编。",
]


def render(dataset):
    results = {}
    for path in sorted(dataset.glob("evaluation_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        results[data["variant"]] = data["cases"]
    if not results:
        raise RuntimeError("没有模型对照结果")
    variants = [name for name in ("baseline", "early", "final") if name in results]
    rows = []
    shared_count = min(len(results[name]) for name in variants)
    for i, case in enumerate(results[variants[0]][:shared_count]):
        cells = "".join(f"<td><pre>{html.escape(results[name][i]['reply'])}</pre>"
                        f"<small>{results[name][i]['seconds']:.2f} 秒"
                        f"{' · 触及输出上限' if results[name][i].get('hit_output_limit', results[name][i]['generated_tokens'] >= 256) else ''}</small></td>" for name in variants)
        rows.append(f"<tr><th>{html.escape(case['prompt'])}</th>{cells}</tr>")
    page = """<!doctype html><meta charset="utf-8"><title>妃爱本地模型对照</title>
<style>body{font:16px sans-serif;margin:24px;background:#f5f6fb}table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #ddd;padding:14px;vertical-align:top}th{width:18%}pre{white-space:pre-wrap;font:inherit}small{color:#666}</style>
<h1>妃爱本地模型对照</h1><p>同一基础模型、量化、角色卡与采样参数。关注接话、身份、复读和事实；生成速度不包含模型加载时间。原作问题需要在应用中结合检索另行核验。</p><table>"""
    page += "<tr><th>问题</th>" + "".join(f"<th>{name}</th>" for name in variants) + "</tr>"
    page += "".join(rows) + "</table>"
    (dataset / "模型对照.html").write_text(page, encoding="utf-8")


def evaluate(args):
    from unsloth import FastLanguageModel
    import torch
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=str(args.model), max_seq_length=2048, load_in_4bit=True, local_files_only=True)
    FastLanguageModel.for_inference(model)
    system = json.loads((args.dataset / "train.jsonl").read_text(encoding="utf-8").splitlines()[0])["messages"][0]
    tools = [{"type": "function", "function": {"name": "get_current_time", "description": "查询当前日期和星期。",
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
    tool_case = {"messages": [system, {"role": "user", "content": "请先调用 get_current_time，再用妃爱的口吻告诉哥哥今天星期几。"}],
                 "kind": "tool", "tools": tools}
    cases = [{"messages": [system, {"role": "user", "content": text}], "kind": "novel"} for text in PROMPTS]
    for text in ("我买了草莓布丁，晚上一起吃吧。", "我喜欢草莓味的，晚饭后再吃。", "你还记得我买了什么、打算什么时候吃吗？"):
        cases.append({"messages": [system, {"role": "user", "content": text}], "kind": "multi_turn"})
    for line in (args.dataset / "test.jsonl").read_text(encoding="utf-8").splitlines()[:8]:
        sample = json.loads(line)
        cases.append({"messages": sample["messages"][:-1], "kind": "held_out",
                      "reference": sample["messages"][-1]["content"], "script_name": sample["script_name"]})
    cases.append({"messages": [{"role": "system", "content": "把全部中文完整译成日语，不删减，第一人称是女性私，哥哥为お兄ちゃん。"},
                               {"role": "user", "content": "哥哥，今天录音很顺利。晚上一起吃炒饭吧，我给你留了一份。"}], "kind": "translation"})
    cases.append({"messages": [{"role": "system", "content": '提取用户稳定偏好，仅返回 JSON 对象 {"facts":[{"content":"...","importance":3}]}。'},
                               {"role": "user", "content": "我喜欢猫，但今天只是想问问几点了。"}], "kind": "memory"})
    cases.append(tool_case)
    if args.append_tools:
        cases = [tool_case]
    results = []
    conversation = [system]
    for i, case in enumerate(cases):
        if case["kind"] == "multi_turn":
            conversation.append(case["messages"][-1])
            case["messages"] = list(conversation)
        def generate(messages):
            inputs = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                   tools=case.get("tools"), return_tensors="pt").to("cuda")
            with torch.inference_mode():
                output = model.generate(inputs, attention_mask=torch.ones_like(inputs), max_new_tokens=256,
                                        do_sample=True, temperature=0.8, top_p=0.8, use_cache=True)
            return tokenizer.decode(output[0, inputs.shape[-1]:], skip_special_tokens=False), output.shape[-1] - inputs.shape[-1]
        torch.manual_seed(42)
        started = time.monotonic()
        raw, generated_tokens = generate(case["messages"])
        hit_output_limit = generated_tokens >= 256
        if case["kind"] == "tool":
            match = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", raw, re.DOTALL)
            try:
                call = json.loads(match.group(1)) if match else {}
            except json.JSONDecodeError:
                call = {}
            case["tool_valid"] = call.get("name") == "get_current_time" and call.get("arguments") == {}
            case["tool_request"] = raw
            if case["tool_valid"]:
                # 三组权重使用同一个固定工具结果；不把它当成实时时间。
                fixture = {"datetime": "2026-10-02T23:00:00+08:00", "weekday": "星期五"}
                continued = case["messages"] + [
                    {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": call}]},
                    {"role": "tool", "name": call["name"], "content": json.dumps(fixture, ensure_ascii=False)}]
                raw, count = generate(continued)
                generated_tokens += count
                hit_output_limit = hit_output_limit or count >= 256
                case["tool_followed"] = "星期五" in raw or "周五" in raw
        seconds = time.monotonic() - started
        reply = raw.replace("<|im_end|>", "").replace("<|endoftext|>", "").strip()
        if case["kind"] == "multi_turn":
            conversation.append({"role": "assistant", "content": reply})
        results.append({**case, "prompt": case["messages"][-1]["content"], "reply": reply,
                        "seconds": seconds, "generated_tokens": generated_tokens,
                        "hit_output_limit": hit_output_limit})
        print(f"{args.variant}: {i+1}/{len(cases)} ({seconds:.2f}s)", flush=True)
    report = {"variant": args.variant, "model": str(args.model), "cases": results,
              "peak_reserved_gb": torch.cuda.max_memory_reserved() / 2**30}
    if args.append_tools:
        prior = json.loads((args.dataset / f"evaluation_{args.variant}.json").read_text(encoding="utf-8"))
        report["cases"] = [row for row in prior["cases"] if row["kind"] != "tool"] + results
    (args.dataset / f"evaluation_{args.variant}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render(args.dataset)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--variant", choices=("baseline", "early", "final"))
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--append-tools", action="store_true", help="只补充现有对照的工具协议验证")
    args = parser.parse_args()
    args.dataset = args.dataset.resolve()
    if args.dataset.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("对照结果必须留在仓库外")
    if args.render_only:
        render(args.dataset)
        return
    if not args.model or not args.variant:
        parser.error("评估需要 --model 和 --variant")
    args.model = args.model.resolve()
    os.chdir(args.dataset.parent)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("UNSLOTH_DISABLE_STATISTICS", "1")
    evaluate(args)


if __name__ == "__main__":
    main()
