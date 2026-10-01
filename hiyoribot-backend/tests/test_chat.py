"""无需真实 DeepSeek 和数据库的接口回归检查。"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from openai import OpenAIError

from main import app, build_model_messages


class ChatApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.conversation_id = uuid4()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config_patch = patch("main.CONFIG_PATH", Path(temporary.name) / "config.yaml")
        config_patch.start()
        self.addCleanup(config_patch.stop)

        defaults = {
            "storage.get_character": {"name": "HiyoriBot", "system_prompt": "自然聊天"},
            "storage.create_conversation": {"id": self.conversation_id},
            "storage.get_conversation": {"id": self.conversation_id},
            "storage.recent_messages": [],
            "storage.save_turn": 42,
            "memory_service.recall": [],
            "storage.role_knowledge_count": 0,
            "storage.game_dialogue_counts": {"lines": 0, "chunks": 0},
        }
        for target, value in defaults.items():
            current = patch(target, return_value=value)
            current.start()
            self.addCleanup(current.stop)
        extraction = patch("memory_service.extract_safely")
        extraction.start()
        self.addCleanup(extraction.stop)

    def test_home_page_is_available(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>HiyoriBot</title>", response.text)
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)
        self.assertEqual(self.client.get("/assets/styles.css").status_code, 200)

    def test_prompt_order_and_memory_boundary(self) -> None:
        result = build_model_messages(
            {"name": "小日和", "system_prompt": "温柔说话", "personality": "爱开玩笑"},
            [{"content": "用户喜欢猫"}],
            [{"role": "user", "content": "我养了一只猫"},
             {"role": "assistant", "content": "它叫什么？"}],
            "它叫小白",
        )
        self.assertEqual([item["role"] for item in result],
                         ["system", "system", "user", "assistant", "user"])
        self.assertIn("温柔说话", result[0]["content"])
        self.assertIn("爱开玩笑", result[0]["content"])
        self.assertIn("用户喜欢猫", result[1]["content"])
        self.assertEqual(result[-1]["content"], "它叫小白")

    def test_chat_saves_complete_turn(self) -> None:
        fake = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="你好！"))])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with patch("main.OpenAI") as client:
                completion = client.return_value.__enter__.return_value.chat.completions.create
                completion.return_value = fake
                response = self.client.post("/chat", json={"message": "  你好  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation_id"], str(self.conversation_id))
        self.assertEqual(response.json()["reply"], "你好！")
        self.assertEqual(completion.call_args.kwargs["messages"][-1],
                         {"role": "user", "content": "你好"})
        from storage import save_turn
        save_turn.assert_called_once_with(self.conversation_id, "你好", "你好！", "")

    def test_role_knowledge_recall_is_separate_from_user_memory(self) -> None:
        sample = {"kind": "style", "content": "妃爱工作时说话利落", "similarity": 0.75}
        fake = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="好"))])
        with (
            patch("storage.role_knowledge_count", return_value=1),
            patch("storage.recall_role_knowledge", return_value=[sample]),
            patch("memory_service.embed", return_value=[0.0] * 512),
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True),
            patch("main.OpenAI") as client,
        ):
            completion = client.return_value.__enter__.return_value.chat.completions.create
            completion.return_value = fake
            response = self.client.post("/chat", json={"message": "今天工作忙吗？"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recalled_memory_ids"], [])
        sent = completion.call_args.kwargs["messages"]
        self.assertIn("妃爱工作时说话利落", sent[1]["content"])
        self.assertEqual(sent[-1]["content"], "今天工作忙吗？")

    def test_game_dialogue_is_reference_only(self) -> None:
        sent = build_model_messages(
            {"name": "和泉妃爱", "system_prompt": "自然交流"}, [], [], "今天要做什么？",
            game_dialogue=[{"script_name": "妃愛-01.ast", "first_entry": 1,
                            "last_entry": 2, "content": "妃爱：今天要录音。"}],
        )
        self.assertIn("参考资料", sent[1]["content"])
        self.assertIn("妃愛-01.ast #1-2", sent[1]["content"])
        self.assertEqual(sent[-1]["content"], "今天要做什么？")

    def test_stream_saves_after_done_and_reports_errors(self) -> None:
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None, reasoning_content="先回应问候。"),
                finish_reason=None)]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="你"), finish_reason=None)]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="好"), finish_reason="stop")]),
        ]
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with patch("main.OpenAI") as client:
                completion = client.return_value.__enter__.return_value.chat.completions.create
                completion.return_value = iter(chunks)
                success = self.client.post("/chat/stream", json={"message": "你好"})
                completion.side_effect = OpenAIError("secret upstream details")
                failure = self.client.post("/chat/stream", json={"message": "再见"})

        self.assertEqual(success.status_code, 200)
        self.assertIn('event: meta\ndata: ', success.text)
        self.assertIn('event: reasoning_delta\ndata: {"text": "先回应问候。"}', success.text)
        self.assertIn('event: delta\ndata: {"text": "你"}', success.text)
        self.assertTrue(success.text.endswith("event: done\ndata: {}\n\n"))
        self.assertIn('event: error\ndata: {"message": "模型服务调用失败"}', failure.text)
        self.assertNotIn("secret upstream details", failure.text)
        from storage import save_turn
        save_turn.assert_called_once_with(self.conversation_id, "你好", "你好", "先回应问候。")

    def test_thinking_can_be_disabled(self) -> None:
        fake = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="好"), finish_reason="stop"
        )])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with patch("main.OpenAI") as client:
                completion = client.return_value.__enter__.return_value.chat.completions.create
                completion.return_value = fake
                response = self.client.post("/chat", json={"message": "你好", "thinking": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reasoning"], None)
        self.assertEqual(completion.call_args.kwargs["extra_body"],
                         {"thinking": {"type": "disabled"}})

    def test_incomplete_reply_is_not_saved(self) -> None:
        partial = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="只生成了一半"), finish_reason="length"
        )])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with patch("main.OpenAI") as client:
                client.return_value.__enter__.return_value.chat.completions.create.return_value = partial
                response = self.client.post("/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 502)
        from storage import save_turn
        save_turn.assert_not_called()

    def test_configuration_and_input_validation(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.client.post("/chat", json={"message": "你好"}).status_code, 503)
            self.assertEqual(self.client.post("/chat", json={"message": "   "}).status_code, 422)

        from main import CONFIG_PATH
        CONFIG_PATH.write_text('api_key: "yaml-key"\n', encoding="utf-8")
        fake = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="好"))])
        with patch("main.OpenAI") as client:
            client.return_value.__enter__.return_value.chat.completions.create.return_value = fake
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(self.client.post("/chat", json={"message": "你好"}).status_code, 200)
            client.assert_called_with(api_key="yaml-key", base_url="https://api.deepseek.com")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}, clear=True):
                self.assertEqual(self.client.post("/chat", json={"message": "你好"}).status_code, 200)
            client.assert_called_with(api_key="env-key", base_url="https://api.deepseek.com")

    def test_character_and_conversation_routes(self) -> None:
        character = {"id": 1, "name": "小日和", "system_prompt": "自然说话"}
        with (
            patch("storage.update_character", return_value=character),
            patch("storage.list_conversations", return_value=[{"id": self.conversation_id, "title": "你好"}]),
            patch("storage.list_messages", return_value=[{"role": "user", "content": "你好"}]),
        ):
            self.assertEqual(self.client.get("/character").status_code, 200)
            self.assertEqual(
                self.client.put("/character", json={"name": "小日和", "system_prompt": "自然说话"}).json()["name"],
                "小日和",
            )
            self.assertEqual(
                self.client.put("/character", json={"name": " ", "system_prompt": "自然说话"}).status_code,
                422,
            )
            self.assertEqual(len(self.client.get("/conversations").json()), 1)
            self.assertEqual(
                self.client.get(f"/conversations/{self.conversation_id}/messages").json()["messages"][0]["content"],
                "你好",
            )

    def test_memory_management_endpoints(self) -> None:
        sample = {"id": 7, "content": "喜欢猫", "importance": 4}
        with (
            patch("memory_service.embed", return_value=[0.0] * 512),
            patch("storage.add_memory", return_value=sample),
            patch("storage.update_memory", return_value=sample),
            patch("storage.delete_memory", return_value=True),
            patch("storage.merge_memories", return_value=sample),
        ):
            payload = {"content": "喜欢猫", "importance": 4}
            self.assertEqual(self.client.post("/memories", json=payload).status_code, 201)
            self.assertEqual(self.client.put("/memories/7", json=payload).status_code, 200)
            self.assertEqual(self.client.post("/memories/merge", json={**payload, "ids": [7, 8]}).status_code, 200)
            self.assertEqual(self.client.delete("/memories/7").status_code, 204)


if __name__ == "__main__":
    unittest.main()
