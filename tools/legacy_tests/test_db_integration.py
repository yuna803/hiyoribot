"""可选的真实 PostgreSQL＋pgvector 验收；只创建并清理带唯一前缀的测试数据。"""

import os
import unittest
from uuid import uuid4

import storage


@unittest.skipUnless(os.getenv("RUN_DB_TEST") == "1", "设置 RUN_DB_TEST=1 才连接真实数据库")
class DatabaseIntegrationTest(unittest.TestCase):
    def test_conversation_and_vector_memory(self) -> None:
        storage.init_db()
        marker = f"hiyoribot-smoke-{uuid4()}"
        conversation_id = None
        try:
            conversation = storage.create_conversation(marker)
            conversation_id = conversation["id"]
            protocol = [{"role": "assistant", "content": "test reply", "reasoning_content": "test reasoning"}]
            source_id = storage.save_turn(conversation_id, marker + "%_", "test reply",
                                          "test reasoning", agent_messages=protocol)
            saved = storage.list_messages(conversation_id)
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[1]["agent_messages"], protocol)
            self.assertEqual(storage.recent_messages(conversation_id)[1]["agent_messages"], protocol)
            self.assertEqual(storage.search_chat_history(conversation_id, marker + "%_")[0]["id"], source_id)
            self.assertEqual(storage.search_chat_history(uuid4(), marker), [])

            first_vector = [1.0] + [0.0] * 511
            second_vector = [0.0, 1.0] + [0.0] * 510
            first = storage.add_memory(marker + " first", 3, first_vector, source_id)
            second = storage.add_memory(marker + " second", 4, second_vector, source_id)
            recalled = storage.recall_memories(first_vector, limit=2)
            self.assertEqual(recalled[0]["id"], first["id"])

            storage.update_memory(first["id"], marker + " edited", 5, first_vector)
            merged = storage.merge_memories(
                [first["id"], second["id"]], marker + " merged", 5, first_vector
            )
            self.assertEqual(merged["id"], first["id"])
            self.assertTrue(storage.delete_memory(merged["id"]))
        finally:
            # 即使断言失败，也只清理这次测试创建的行。
            with storage.connect() as conn:
                conn.execute("DELETE FROM memory WHERE content LIKE %s", (marker + "%",))
                if conversation_id is not None:
                    conn.execute("DELETE FROM conversation WHERE id = %s", (conversation_id,))


if __name__ == "__main__":
    unittest.main()
