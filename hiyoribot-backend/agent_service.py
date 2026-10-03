"""有限轮次的模型→工具→模型循环，两种聊天接口共用。"""

import json
from collections.abc import Iterator
from uuid import UUID

import agent_tools
import llm_runtime

MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 8


class AgentError(RuntimeError):
    """模型返回不完整或不符合工具协议，本轮不能保存。"""


def _model_round(client, model: str, messages: list[dict], options: dict,
                 stream: bool, tool_choice: str | None, config=None, job=None, recalled_context=None) -> Iterator:
    if config is not None:
        try:
            messages = llm_runtime.fit_messages(messages, config, agent_tools.TOOLS if tool_choice else None)
        except (OSError, ValueError) as exc:
            raise AgentError("本地模型上下文或分词器不可用，请检查设定与模型文件") from exc
    arguments = {"model": model, "messages": list(messages), **options}
    if job:
        job.check()
        job.bind(client.close)
    if recalled_context is not None:
        # 在实际裁剪后检查标记；报告传入的参考资料，不声称模型一定遵循。
        system_text = "\n".join(item["content"] for item in messages if item["role"] == "system")
        used = {"notes": [row for row in recalled_context.get("notes", []) if f"[{row['source']}]" in system_text],
                "dialogue": [row for row in recalled_context.get("dialogue", [])
                             if f"[{row['script_name']} #{row['first_entry']}-{row['last_entry']}]" in system_text],
                "summary_used": "本会话摘要：" in system_text,
                "progress": recalled_context.get("progress")}
        yield "context_used", used
    if tool_choice is not None:
        arguments.update(tools=agent_tools.TOOLS, tool_choice=tool_choice)
    if not stream:
        response = client.chat.completions.create(**arguments)
        if job:
            job.check()
        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise AgentError("模型回复为空")
        answer = choice.message
        calls = []
        for call in getattr(answer, "tool_calls", None) or []:
            if getattr(call, "type", "function") != "function" or not getattr(call, "function", None):
                raise AgentError("模型返回了不支持的工具类型")
            calls.append({"id": call.id, "type": "function", "function": {
                "name": call.function.name, "arguments": call.function.arguments,
            }})
        message = {"role": "assistant", "content": answer.content or "",
                   "reasoning_content": getattr(answer, "reasoning_content", None) or ""}
        if message["reasoning_content"]:
            yield "reasoning_delta", {"text": message["reasoning_content"]}
        if message["content"]:
            yield "delta", {"text": message["content"]}
        finish_reason = getattr(choice, "finish_reason", None)
    else:
        content, reasoning, assembled = [], [], {}
        finish_reason = None
        response = client.chat.completions.create(**arguments, stream=True)
        if job:
            job.bind(getattr(response,"close",client.close))
        try:
            for chunk in response:
                if job:
                    job.check()
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                delta = choice.delta
                thought = getattr(delta, "reasoning_content", None)
                if thought:
                    reasoning.append(thought)
                    yield "reasoning_delta", {"text": thought}
                if delta.content:
                    content.append(delta.content)
                    yield "delta", {"text": delta.content}
                for fragment in getattr(delta, "tool_calls", None) or []:
                    if getattr(fragment, "type", None) not in (None, "function"):
                        raise AgentError("模型返回了不支持的工具类型")
                    call = assembled.setdefault(fragment.index, {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                    })
                    if fragment.id:
                        call["id"] = fragment.id
                    if fragment.function:
                        call["function"]["name"] += fragment.function.name or ""
                        call["function"]["arguments"] += fragment.function.arguments or ""
                    if len(assembled) > 32 or len(call["function"]["arguments"]) > 4096:
                        raise AgentError("模型工具请求过大")
        finally:
            if hasattr(response, "close"):
                response.close()
            if job:
                job.bind(client.close)
        calls = [assembled[index] for index in sorted(assembled)]
        message = {"role": "assistant", "content": "".join(content),
                   "reasoning_content": "".join(reasoning)}

    if finish_reason not in (None, "stop", "tool_calls"):
        raise AgentError("模型回复不完整，未保存这轮聊天")
    if finish_reason == "tool_calls" and not calls:
        raise AgentError("模型未返回有效工具请求")
    if calls:
        if len(calls) > 32 or any(not call["id"] or not call["function"]["name"]
                                  or not isinstance(call["function"]["arguments"], str)
                                  or len(call["function"]["arguments"]) > 4096 for call in calls):
            raise AgentError("模型工具请求不完整")
        if len({call["id"] for call in calls}) != len(calls):
            raise AgentError("模型工具请求标识重复")
        message["tool_calls"] = calls
    return message


def run(client, model: str, messages: list[dict], conversation_id: UUID,
        character_name: str, options: dict, *, stream: bool, tools_enabled: bool, config=None,
        job=None, recalled_context=None) -> Iterator:
    context = [dict(message) for message in messages]
    if tools_enabled:
        context[0]["content"] += "\n\n" + agent_tools.TOOL_PROMPT
    protocol, trace, thoughts, references = [], [], [], []
    cache = {}
    calls_used = 0
    for round_number in range(1, MAX_TOOL_ROUNDS + 2):
        final_only = round_number > MAX_TOOL_ROUNDS or calls_used >= MAX_TOOL_CALLS
        choice = ("none" if final_only else "auto") if tools_enabled else None
        yield "agent_round", {"round": round_number, "final_only": final_only}
        if job:
            job.check()
        iterator = _model_round(client, model, context, options, stream, choice, config, job, recalled_context)
        try:
            while True:
                try:
                    event, data = next(iterator)
                except StopIteration as completed:
                    message = completed.value
                    break
                if event == "context_used":
                    data = {"round": round_number, **data}
                    references.append(data)
                yield event, data
        finally:
            iterator.close()
        protocol.append(message)
        if message["reasoning_content"]:
            thoughts.append(message["reasoning_content"])
        calls = message.get("tool_calls", [])
        if not calls:
            if not message["content"].strip():
                raise AgentError("模型回复为空，未保存这轮聊天")
            yield "complete", {"reply": message["content"], "reasoning": "\n\n".join(thoughts),
                               "tool_calls": trace, "agent_messages": protocol,
                               "recalled_context": {"rounds": references}}
            return
        if not tools_enabled or final_only:
            raise AgentError("模型未遵守工具调用上限，未保存这轮聊天")
        context.append(message)
        for call in calls:
            if job:
                job.check()
            name, raw = call["function"]["name"], call["function"]["arguments"]
            try:
                parsed = json.loads(raw)
                cache_key = (name, json.dumps(parsed, sort_keys=True, ensure_ascii=False))
            except (ValueError, TypeError):
                parsed, cache_key = raw[:500], (name, raw)
            record = {"id": call["id"], "round": round_number, "name": name, "arguments": parsed}
            yield "tool_start", record
            cached = cache_key in cache
            if calls_used >= MAX_TOOL_CALLS:
                result = {"error": "本轮工具次数已用完，请根据已有结果直接回复。"}
            elif cached:
                result = cache[cache_key]
            else:
                progress_snapshot = ({"story_progress":recalled_context["progress"]}
                                     if recalled_context is not None and "progress" in recalled_context else {})
                result = agent_tools.execute(name,raw,conversation_id,character_name,**progress_snapshot)
                cache[cache_key] = result
            calls_used += 1
            record = {**record, "result": result, "cached": cached}
            trace.append(record)
            yield "tool_result", record
            tool_message = {"role": "tool", "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False)}
            context.append(tool_message)
            protocol.append(tool_message)
