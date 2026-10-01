"""记忆提取、召回和向量边界的最小检查。"""

import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import memory_service
import storage


class MemoryTest(unittest.TestCase):
    def test_recall_filters_irrelevant_and_uses_importance(self) -> None:
        candidates = [
            {"id": 1, "content": "喜欢猫", "importance": 1, "similarity": 0.70},
            {"id": 2, "content": "养了一只猫", "importance": 5, "similarity": 0.68},
            {"id": 3, "content": "无关内容", "importance": 5, "similarity": 0.20},
        ]
        with (
            patch("storage.memory_count", return_value=3),
            patch("memory_service.embed", return_value=[0.0] * 512),
            patch("storage.recall_memories", return_value=candidates),
        ):
            result = memory_service.recall("猫咪")
        self.assertEqual([item["id"] for item in result], [2, 1])

    def test_extract_stores_valid_fact(self) -> None:
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"facts":[{"content":"用户喜欢猫","importance":4}]}'
        ))])
        config = SimpleNamespace(api_key="test-key", base_url="https://example.test", model="test-model")
        with (
            patch("memory_service.OpenAI") as client,
            patch("memory_service.embed", return_value=[0.0] * 512),
            patch("storage.add_memory") as add,
        ):
            client.return_value.__enter__.return_value.chat.completions.create.return_value = response
            count = memory_service.extract_and_store("我喜欢猫", 12, config)
        self.assertEqual(count, 1)
        add.assert_called_once_with("用户喜欢猫", 4, [0.0] * 512, 12)

    def test_vector_rejects_wrong_dimension(self) -> None:
        with self.assertRaises(ValueError):
            storage.vector_literal([0.0] * 3)

    def test_database_url_comes_from_yaml_or_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text('database_url: "postgresql://yaml-host/db"\n', encoding="utf-8")
            with patch("storage.CONFIG_PATH", path), patch("storage.psycopg.connect") as connect:
                with patch.dict(os.environ, {}, clear=True):
                    storage._open_connection()
                self.assertEqual(connect.call_args.args[0], "postgresql://yaml-host/db")
                with patch.dict(os.environ, {"DATABASE_URL": "postgresql://env-host/db"}, clear=True):
                    storage._open_connection()
                self.assertEqual(connect.call_args.args[0], "postgresql://env-host/db")

            path.write_text("database_url: \"\"\n", encoding="utf-8")
            with patch("storage.CONFIG_PATH", path), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(psycopg.OperationalError):
                    storage._open_connection()


if __name__ == "__main__":
    unittest.main()
