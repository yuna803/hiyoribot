"""有限轮次的模型→工具→模型循环，两种聊天接口共用。"""

import json
from collections.abc import Iterator
from uuid import UUID

import agent_tools

MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 8


class AgentError(RuntimeError):
    """模型返回不完整或不符合工具协议，本轮不能保存。"""


def _model_round(client, model: str, messages: list[dict], options: dict,
                 stream: bool, tool_choice: str | None) -> Iterator:
    arguments = {"model": model, "messages": list(messages), **options}
    if tool_choice is not None:
        arguments.update(tools=agent_tools.TOOLS, tool_choice=tool_choice)
    if not stream:
        response = client.chat.completions.create(**arguments)
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
        try:
            for chunk in response:
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
        character_name: str, options: dict, *, stream: bool, tools_enabled: bool) -> Iterator:
    context = [dict(message) for message in messages]
    if tools_enabled:
        context[0]["content"] += "\n\n" + agent_tools.TOOL_PROMPT
    protocol, trace, thoughts = [], [], []
    cache = {}
    calls_used = 0
    for round_number in range(1, MAX_TOOL_ROUNDS + 2):
        final_only = round_number > MAX_TOOL_ROUNDS or calls_used >= MAX_TOOL_CALLS
        choice = ("none" if final_only else "auto") if tools_enabled else None
        yield "agent_round", {"round": round_number, "final_only": final_only}
        message = yield from _model_round(client, model, context, options, stream, choice)
        protocol.append(message)
        if message["reasoning_content"]:
            thoughts.append(message["reasoning_content"])
        calls = message.get("tool_calls", [])
        if not calls:
            if not message["content"].strip():
                raise AgentError("模型回复为空，未保存这轮聊天")
            yield "complete", {"reply": message["content"], "reasoning": "\n\n".join(thoughts),
                               "tool_calls": trace, "agent_messages": protocol}
            return
        if not tools_enabled or final_only:
            raise AgentError("模型未遵守工具调用上限，未保存这轮聊天")
        context.append(message)
        for call in calls:
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
                result = agent_tools.execute(name, raw, conversation_id, character_name)
                cache[cache_key] = result
            calls_used += 1
            record = {**record, "result": result, "cached": cached}
            trace.append(record)
            yield "tool_result", record
            tool_message = {"role": "tool", "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False)}
            context.append(tool_message)
            protocol.append(tool_message)
