"""验证工具循环、流式参数拼接和完整历史回传，不调用外部 API。"""

import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

import agent_service
import agent_tools
from main import app, build_model_messages


def call(name, arguments="{}", identifier="call1"):
    return NS(id=identifier, function=NS(name=name, arguments=arguments))


def answer(content="", reasoning="", calls=None):
    return NS(choices=[NS(message=NS(content=content, reasoning_content=reasoning, tool_calls=calls),
                          finish_reason="tool_calls" if calls else "stop")])


def chunk(content=None, reasoning=None, calls=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=reasoning, tool_calls=calls),
                          finish_reason=finish)])


def fragment(index, name=None, arguments=None, identifier=None):
    return NS(index=index, id=identifier, function=NS(name=name, arguments=arguments))


class AgentLoopTests(unittest.TestCase):
    def run_agent(self, client, *, stream=False, tools_enabled=True):
        return list(agent_service.run(
            client, "test-model", [{"role": "system", "content": "角色"},
                                   {"role": "user", "content": "查询后回答"}],
            uuid4(), "和泉妃爱", {"extra_body": {"thinking": {"type": "enabled"}}},
            stream=stream, tools_enabled=tools_enabled,
        ))

    def test_two_tool_rounds_return_results_and_reasoning_to_model(self):
        client = Mock()
        create = client.chat.completions.create
        create.side_effect = [answer("先查一下", "第一轮", [call("get_current_time")]),
                              answer(reasoning="第二轮", calls=[call("search_user_memory", '{"query":"甜点"}', "call2")]),
                              answer("哥哥，记得你喜欢布丁。", "第三轮")]
        with patch("agent_tools.execute", side_effect=[{"time": "18:00"}, {"memories": ["喜欢布丁"]}]) as execute:
            events = self.run_agent(client)
        final = events[-1][1]
        self.assertEqual(final["reply"], "哥哥，记得你喜欢布丁。")
        self.assertEqual(len(final["tool_calls"]), 2)
        self.assertEqual(execute.call_count, 2)
        second = create.call_args_list[1].kwargs["messages"]
        self.assertEqual(second[-2]["reasoning_content"], "第一轮")
        self.assertEqual(second[-1]["tool_call_id"], "call1")
        third = create.call_args_list[2].kwargs["messages"]
        self.assertEqual(third[-2]["reasoning_content"], "第二轮")
        self.assertEqual([m["role"] for m in final["agent_messages"]],
                         ["assistant", "tool", "assistant", "tool", "assistant"])

    def test_repeated_calls_are_cached_and_last_request_forces_reply(self):
        client = Mock()
        create = client.chat.completions.create
        create.side_effect = [answer(calls=[call("get_current_time", identifier=f"call{i}")])
                              for i in range(4)] + [answer("好了")]
        with patch("agent_tools.execute", return_value={"time": "18:00"}) as execute:
            final = self.run_agent(client)[-1][1]
        execute.assert_called_once()
        self.assertEqual([row["cached"] for row in final["tool_calls"]], [False, True, True, True])
        self.assertEqual(create.call_args_list[-1].kwargs["tool_choice"], "none")
        self.assertEqual(create.call_count, 5)

    def test_batch_cannot_exceed_total_execution_budget(self):
        client = Mock()
        client.chat.completions.create.side_effect = [answer(calls=[
            call("search_user_memory", json.dumps({"query": str(i)}), f"call{i}") for i in range(9)
        ]), answer("根据已取得的信息回答")]
        with patch("agent_tools.execute", return_value={"memories": []}) as execute:
            final = self.run_agent(client)[-1][1]
        self.assertEqual(execute.call_count, 8)
        self.assertIn("error", final["tool_calls"][-1]["result"])
        self.assertEqual(client.chat.completions.create.call_args.kwargs["tool_choice"], "none")

    def test_invalid_arguments_can_be_corrected_in_next_round(self):
        client = Mock()
        client.chat.completions.create.side_effect = [
            answer(calls=[call("search_user_memory", "{broken")]),
            answer(calls=[call("search_user_memory", '{"query":"猫"}', "call2")]), answer("查好了")]
        with patch("memory_service.recall", return_value=[]) as recall:
            final = self.run_agent(client)[-1][1]
        self.assertIn("error", final["tool_calls"][0]["result"])
        self.assertEqual(final["tool_calls"][1]["result"], {"memories": []})
        recall.assert_called_once_with("猫")

    def test_noncompliant_model_cannot_start_a_fifth_tool_round(self):
        client = Mock()
        client.chat.completions.create.side_effect = [
            answer(calls=[call("get_current_time", identifier=f"call{i}")]) for i in range(5)]
        with patch("agent_tools.execute", return_value={"time": "18:00"}) as execute:
            with self.assertRaises(agent_service.AgentError):
                self.run_agent(client)
        execute.assert_called_once()
        self.assertEqual(client.chat.completions.create.call_count, 5)

    def test_disabling_tools_makes_one_plain_request(self):
        client = Mock()
        client.chat.completions.create.return_value = answer("你好")
        final = self.run_agent(client, tools_enabled=False)[-1][1]
        self.assertNotIn("tools", client.chat.completions.create.call_args.kwargs)
        self.assertEqual(final["tool_calls"], [])
        client.chat.completions.create.assert_called_once()

    def test_stream_fragments_reach_ui_and_are_saved_as_complete_protocol(self):
        streams = [iter([
            chunk(reasoning="先查旧聊天"),
            chunk(calls=[fragment(0, "search_chat_history", '{"que', "call1")]),
            chunk(calls=[fragment(0, arguments='ry":"布丁"}')], finish="tool_calls"),
        ]), iter([chunk(reasoning="找到了"), chunk(content="今晚一起吃布丁。", finish="stop")])]
        conversation = uuid4()
        config = NS(api_key="test", base_url="https://example.invalid", model="test", auto_extract_memory=False)
        with (patch("main.resolve_config", return_value=config),
              patch("main.prepare_chat", return_value=(conversation, [{"role": "system", "content": "角色"}], [], "和泉妃爱")),
              patch("main.OpenAI") as client, patch("storage.save_turn", return_value=42) as save,
              patch("agent_tools.execute", return_value={"messages": []}) as execute):
            client.return_value.__enter__.return_value.chat.completions.create.side_effect = streams
            response = TestClient(app).post("/chat/stream", json={"message": "记得布丁吗？"})
        self.assertIn("event: tool_start", response.text)
        self.assertIn("event: tool_result", response.text)
        self.assertIn('"round": 2', response.text)
        self.assertTrue(response.text.endswith("event: done\ndata: {}\n\n"))
        execute.assert_called_once_with("search_chat_history", '{"query":"布丁"}', conversation, "和泉妃爱")
        self.assertEqual(save.call_args.args[2], "今晚一起吃布丁。")
        protocol = save.call_args.kwargs["agent_messages"]
        self.assertEqual(protocol[0]["reasoning_content"], "先查旧聊天")
        self.assertEqual(protocol[-1]["reasoning_content"], "找到了")

    def test_history_replays_complete_tool_messages_and_reasoning(self):
        protocol = [{"role": "assistant", "content": "", "reasoning_content": "查时间",
                     "tool_calls": [{"id": "call1", "type": "function", "function": {
                         "name": "get_current_time", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "call1", "content": '{"time":"18:00"}'},
                    {"role": "assistant", "content": "六点了", "reasoning_content": "回复时间"}]
        history = [{"role": "user", "content": "旧话"},
                   {"role": "assistant", "content": "x" * 13000},
                   {"role": "user", "content": "几点"},
                   {"role": "assistant", "content": "六点了", "agent_messages": protocol}]
        messages = build_model_messages({"name": "妃爱", "system_prompt": "角色"}, [], history, "继续")
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(messages[2:5], protocol)


class ToolBoundaryTests(unittest.TestCase):
    def test_unknown_tools_and_other_conversation_ids_are_rejected(self):
        with patch("storage.search_chat_history") as search:
            self.assertIn("error", agent_tools.execute("run_sql", '{}', uuid4(), "妃爱"))
            self.assertIn("error", agent_tools.execute("search_chat_history",
                          '{"query":"猫","conversation_id":"other"}', uuid4(), "妃爱"))
        search.assert_not_called()

    def test_tool_failure_is_not_reported_as_empty_memory_or_leaked(self):
        with patch("memory_service.recall", side_effect=RuntimeError("secret connection")):
            result = agent_tools.execute("search_user_memory", '{"query":"猫"}', uuid4(), "妃爱")
        self.assertIn("error", result)
        self.assertNotIn("secret", json.dumps(result))

    def test_other_character_never_reads_hiyori_game_dialogue(self):
        with (patch("storage.role_knowledge_count", return_value=0),
              patch("storage.game_dialogue_counts") as game):
            result = agent_tools.execute("search_character_knowledge", '{"query":"录音"}', uuid4(), "别的角色")
        game.assert_not_called()
        self.assertEqual(result["dialogue"], [])
