"""TTS 网页接口的缓存与音频读取，不调用模型或外部 API。"""

import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app


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

        with (patch("tts_service._spoken_japanese", return_value="お兄ちゃん、こんにちは。") as translate,
              patch("tts_service._synthesize", side_effect=fake_synthesize) as synthesize):
            first = self.client.post("/tts", json={"text": "哥哥，你好。"})
            second = self.client.post("/tts", json={"text": "哥哥，你好。"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["japanese"], "お兄ちゃん、こんにちは。")
        translate.assert_called_once()
        synthesize.assert_called_once()
        audio = self.client.get(first.json()["audio_url"])
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["content-type"], "audio/wav")
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_invalid_requests_do_not_generate_audio(self) -> None:
        self.assertEqual(self.client.post("/tts", json={"text": "   "}).status_code, 422)
        self.assertEqual(self.client.get("/tts/audio/not-a-key").status_code, 404)


if __name__ == "__main__":
    unittest.main()
