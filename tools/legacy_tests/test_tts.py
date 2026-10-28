"""TTS 网页接口的缓存与音频读取，不调用模型或外部 API。"""

import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from tts_service import _synthesize, _translate_japanese, extract_spoken_text
from tts_worker import NLTK_RESOURCES, prepare_nltk_resources, split_japanese_text


class SpeechRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        dataset = home / "pilot_v1"
        dataset.mkdir()
        (dataset / "active_model.json").write_text(
            json.dumps({"model_version": "v2Pro", "gpt_epoch": 5, "sovits_epoch": 4}),
            encoding="utf-8",
        )
        self.client = TestClient(app)
        config = SimpleNamespace(tts_home=str(home), api_key="unused", base_url="", model="")
        current = patch("main.resolve_config", return_value=config)
        current.start()
        self.addCleanup(current.stop)

    def test_click_generates_once_and_history_can_reuse_audio(self) -> None:
        def fake_synthesize(_japanese, path, _home):
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 32000, 0, "NONE", "not compressed"))
                output.writeframes(b"\0\0" * 32000)

        with (patch("tts_service._translate_japanese", return_value="お兄ちゃん、こんにちは。") as translate,
              patch("tts_service._synthesize", side_effect=fake_synthesize) as synthesize):
            first = self.client.post("/tts", json={"text": "哥哥，你好。"})
            second = self.client.post("/tts", json={"text": "哥哥，你好。"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["japanese"], "お兄ちゃん、こんにちは。")
        self.assertEqual(first.json()["source_text"], "哥哥，你好。")
        translate.assert_called_once()
        synthesize.assert_called_once()
        audio = self.client.get(first.json()["audio_url"])
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["content-type"], "audio/wav")
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_invalid_requests_do_not_generate_audio(self) -> None:
        self.assertEqual(self.client.post("/tts", json={"text": "   "}).status_code, 422)
        self.assertEqual(self.client.get("/tts/audio/not-a-key").status_code, 404)

    def test_old_short_audio_cache_is_not_reused(self) -> None:
        from main import resolve_config
        home = Path(resolve_config().tts_home)
        dataset = home / "pilot_v1"
        text = "哥哥，你好。"
        old_key = hashlib.sha256((dataset / "active_model.json").read_bytes()
                                 + b"\0" + text.encode("utf-8")).hexdigest()
        cache = dataset / "web_audio"
        cache.mkdir()
        (cache / f"{old_key}.wav").write_bytes(b"old short audio")
        (cache / f"{old_key}.json").write_text('{"japanese":"old"}', encoding="utf-8")
        with (patch("tts_service._translate_japanese", return_value="新しい全文。") as translate,
              patch("tts_service._synthesize", side_effect=HTTPException(502, "test stop"))):
            response = self.client.post("/tts", json={"text": text})
        self.assertEqual(response.status_code, 502)
        translate.assert_called_once_with(text, resolve_config())

    def test_action_only_reply_does_not_call_translation(self) -> None:
        with patch("tts_service._translate_japanese") as translate:
            response = self.client.post("/tts", json={"text": "（笑着挥手）"})
        self.assertEqual(response.status_code, 422)
        translate.assert_not_called()


class SpeechFidelityTests(unittest.TestCase):
    def test_missing_pronunciation_data_is_reported_before_loading_model(self) -> None:
        fake = SimpleNamespace(data=SimpleNamespace(path=[], find=Mock(side_effect=LookupError)))
        with patch.dict("sys.modules", {"nltk": fake}):
            with self.assertRaisesRegex(RuntimeError, "TTS_RESOURCE_MISSING:cmudict"):
                prepare_nltk_resources(Path("tts-home"))
        self.assertEqual(fake.data.find.call_count, len(NLTK_RESOURCES))

    def test_resource_failure_is_actionable_and_full_diagnostic_stays_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "env").mkdir()
            (home / "env" / "python.exe").touch()
            (home / "GPT-SoVITS").mkdir()
            output = home / "audio.wav"
            result = SimpleNamespace(returncode=1, stderr="trace\nRuntimeError: TTS_RESOURCE_MISSING:cmudict\n",
                                     stdout="private pronunciation context")
            with patch("tts_service.subprocess.run", return_value=result):
                with self.assertRaises(HTTPException) as caught:
                    _synthesize("日文に suddenly が混じる。", output, home)
            self.assertEqual(caught.exception.status_code, 503)
            self.assertIn("cmudict", caught.exception.detail)
            self.assertNotIn("private", caught.exception.detail)
            self.assertIn(result.stdout, output.with_suffix(".error.log").read_text(encoding="utf-8"))

    def test_all_spoken_paragraphs_are_preserved_in_order(self) -> None:
        source = "（把碗放下）哥哥，晚上一起吃布丁吧。\n\n（清嗓（模仿别人））那句话是别人说的。\n\n你想听哪种语气？\n\n（转身收拾）饭快凉了。"
        self.assertEqual(extract_spoken_text(source),
                         "哥哥，晚上一起吃布丁吧。\n\n那句话是别人说的。\n\n你想听哪种语气？\n\n饭快凉了。")

    def test_formatting_is_removed_without_summarizing_words(self) -> None:
        self.assertEqual(extract_spoken_text("**哥哥**，看一下[这份资料](https://example.com)。"),
                         "哥哥，看一下这份资料。")

    def test_long_translation_is_accepted_and_partial_translation_rejected(self) -> None:
        config = SimpleNamespace(api_key="test", base_url="https://example.invalid", model="test")
        translated = "お兄ちゃん、全部の台詞を読むよ。" * 20
        with patch("tts_service.OpenAI") as client:
            create = client.return_value.__enter__.return_value.chat.completions.create
            create.return_value = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=translated), finish_reason="stop")])
            self.assertEqual(_translate_japanese("完整中文台词", config), translated)
            create.return_value.choices[0].finish_reason = "length"
            with self.assertRaises(HTTPException):
                _translate_japanese("完整中文台词", config)

    def test_audio_chunks_keep_every_character_and_fit_limit(self) -> None:
        text = "お兄ちゃん、こんばんは。" * 30 + "あ" * 220 + "！"
        chunks = split_japanese_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(0 < len(chunk) <= 160 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
