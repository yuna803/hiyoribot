"""本地模式的调用隔离、完整上下文与显存释放检查。"""

import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from threading import Event, Thread
from unittest.mock import patch

from fastapi.testclient import TestClient
from openai import OpenAIError
from fastapi import HTTPException
import httpx

import llm_runtime
import memory_service
import tts_service
from main import AppConfig, app, resolve_config


class LocalModelTests(unittest.TestCase):
    def test_gpu_session_survives_stream_generator_thread_switch(self):
        config = SimpleNamespace(provider="local")
        ready, finish = Event(), Event()
        errors = []
        def stream():
            with llm_runtime.gpu_session(config):
                yield "chunk"
        iterator = stream()
        def begin():
            next(iterator)
            ready.set()
            finish.wait(3)
        def end():
            try:
                next(iterator, None)
            except Exception as exc:
                errors.append(exc)
        first = Thread(target=begin)
        first.start()
        self.assertTrue(ready.wait(3))
        second = Thread(target=end)
        second.start()
        second.join(3)
        finish.set()
        first.join(3)
        self.assertFalse(errors)
        self.assertFalse(second.is_alive())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        tokenizer = self.home / "tokenizer.json"
        tokenizer.write_text("{}", encoding="utf-8")
        self.config = AppConfig(provider="local", local_tokenizer_path=str(tokenizer),
                                local_model="hiyori-role", local_auxiliary_model="hiyori-base")

    def test_cloud_environment_is_ignored_and_local_failure_has_no_fallback(self):
        with (patch("main.load_config", return_value=self.config),
              patch.dict(os.environ, {"DEEPSEEK_API_KEY": "cloud-secret",
                                      "DEEPSEEK_BASE_URL": "https://cloud.invalid",
                                      "DEEPSEEK_MODEL": "cloud-model"}),
              patch("main.prepare_chat", return_value=("conversation", [{"role": "system", "content": "角色"}], [], "妃爱",{})),
              patch("main.OpenAI") as client):
            current = resolve_config()
            self.assertEqual(current.base_url, "http://127.0.0.1:11434/v1")
            self.assertEqual(current.api_key, "local-ignored")
            self.assertEqual(current.model, "hiyori-role")
            client.return_value.__enter__.return_value.chat.completions.create.side_effect = OpenAIError("offline")
            with patch("llm_runtime.fit_messages", return_value=[]):
                response = TestClient(app).post("/chat", json={"message": "你好"})
            self.assertEqual(response.status_code, 502)
            client.assert_called_once()
            self.assertEqual(client.call_args.kwargs["api_key"], "local-ignored")
            self.assertEqual(client.call_args.kwargs["base_url"], current.base_url)
            self.assertEqual(client.call_args.kwargs["max_retries"], 0)
            self.assertIsInstance(client.call_args.kwargs["http_client"], httpx.Client)
            self.assertFalse(TestClient(app).get("/model-status").json()["thinking_supported"])

    def test_local_mode_rejects_external_endpoint(self):
        self.config.local_base_url = "https://api.deepseek.com/v1"
        with patch("main.load_config", return_value=self.config):
            self.assertEqual(TestClient(app).get("/model-status").status_code, 503)

    def test_budget_removes_complete_old_turn_and_cloud_reasoning(self):
        class Counter:
            def encode(self, value, **_):
                return SimpleNamespace(ids=range(len(value)))
        messages = [{"role": "system", "content": "角色设定"},
                    {"role": "user", "content": "旧消息" * 2000},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "old"}]},
                    {"role": "tool", "content": "旧工具", "tool_call_id": "old"},
                    {"role": "assistant", "content": "旧回复", "reasoning_content": "旧思考"},
                    {"role": "user", "content": "现在问候"},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "new"}]},
                    {"role": "tool", "content": "新工具", "tool_call_id": "new"}]
        self.config.local_context_tokens = 4096
        with patch("llm_runtime._tokenizer", return_value=Counter()):
            fitted = llm_runtime.fit_messages(messages, self.config)
            self.assertEqual([m["role"] for m in fitted], ["system", "user", "assistant", "tool"])
            self.assertEqual(fitted[-1]["tool_call_id"], "new")
            self.assertNotIn("旧思考", json.dumps(fitted, ensure_ascii=False))
            with self.assertRaises(ValueError):
                llm_runtime.fit_messages([messages[0], {"role": "user", "content": "很长" * 3000}], self.config)

    def test_auxiliary_tasks_use_unmodified_base_and_no_deepseek_options(self):
        self.config = self.config.model_copy(update={"api_key": "local", "base_url": self.config.local_base_url})
        with (patch("memory_service.OpenAI") as client,
              patch("llm_runtime.fit_messages", side_effect=lambda messages, *_args, **_kwargs: messages),
              patch("memory_service.embed", return_value=[0.0] * 512), patch("storage.add_memory")):
            create = client.return_value.__enter__.return_value.chat.completions.create
            create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"facts":[]}'))])
            memory_service.extract_and_store("我喜欢猫", 1, self.config)
            self.assertEqual(create.call_args.kwargs["model"], "hiyori-base")
            self.assertNotIn("extra_body", create.call_args.kwargs)
        with patch("tts_service.OpenAI") as client, patch("llm_runtime.fit_messages", side_effect=lambda messages, *_args, **_kwargs: messages):
            create = client.return_value.__enter__.return_value.chat.completions.create
            create.return_value = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="お兄ちゃん、こんにちは。"), finish_reason="stop")])
            tts_service._translate_japanese("哥哥你好。", self.config)
            self.assertEqual(create.call_args.kwargs["model"], "hiyori-base")
            self.assertNotIn("extra_body", create.call_args.kwargs)

    def test_only_project_models_are_unloaded(self):
        self.config = self.config.model_copy(update={"model": "hiyori-role", "base_url": self.config.local_base_url})
        with patch("llm_runtime.httpx.Client") as client:
            llm_runtime.unload_local_models(self.config)
            posts = client.return_value.__enter__.return_value.post.call_args_list
            self.assertEqual([call.kwargs["json"] for call in posts],
                             [{"model": "hiyori-role", "keep_alive": 0},
                              {"model": "hiyori-base", "keep_alive": 0}])
            self.assertTrue(all(call.args[0] == "http://127.0.0.1:11434/api/generate" for call in posts))

    def test_tts_releases_gpu_first_and_stops_if_release_fails(self):
        dataset = self.home / "pilot_v1"
        dataset.mkdir()
        (dataset / "active_model.json").write_text("{}", encoding="utf-8")
        self.config = self.config.model_copy(update={"tts_home": str(self.home)})
        events = []
        def synthesize(_text, output, _home):
            events.append("synthesize")
            with wave.open(str(output), "wb") as audio:
                audio.setparams((1, 2, 32000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\0\0" * 32000)
        with (patch("tts_service._translate_japanese", return_value="こんにちは。"),
              patch("llm_runtime.unload_local_models", side_effect=lambda _config: events.append("unload")),
              patch("tts_service._synthesize", side_effect=synthesize)):
            tts_service.create_speech("哥哥你好。", self.config)
        self.assertEqual(events, ["unload", "synthesize"])
        with (patch("tts_service._translate_japanese", return_value="こんばんは。"),
              patch("llm_runtime.unload_local_models", side_effect=httpx.HTTPError("offline")),
              patch("tts_service._synthesize") as synthesize):
            with self.assertRaises(HTTPException) as caught:
                tts_service.create_speech("哥哥晚上好。", self.config)
            self.assertEqual(caught.exception.status_code, 503)
            synthesize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
