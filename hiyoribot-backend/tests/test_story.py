"""验证真实跳转顺序、分支和剧情边界；使用合成台词，不保存原作全文。"""

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import HTTPException

import agent_tools
import storage
import story_service as story
from import_story_timeline import build_graph, transitions
from main import app, build_model_messages, ChatRequest, prepare_chat


ROOT = "共通-01.ast"
BRANCH_A, BRANCH_B, MERGED = "共通-99b.ast", "共通-02a.ast", "妃愛-01.ast"
CHAPTERS = [{"script_name": name, "scope": "hiyori" if name == MERGED else "common", "max_entry": 10}
            for name in (ROOT, BRANCH_A, BRANCH_B, MERGED)]
EDGES = [{"source_script": source, "source_entry": 9, "target_script": target, "condition": "", "via": ""}
         for source, target in [(ROOT, BRANCH_A), (ROOT, BRANCH_B), (BRANCH_A, MERGED), (BRANCH_B, MERGED)]]


class StoryTests(unittest.TestCase):
    def test_branches_are_not_merged_or_sorted_by_filename(self):
        state = story.StoryProgress(route="hiyori", script_name=MERGED, entry_no=2)
        bounds = story.bounds_for_progress(state.model_dump(), CHAPTERS, EDGES)
        self.assertEqual(bounds, {ROOT: 9, MERGED: 2})
        state.completed_scripts = [BRANCH_A]
        self.assertEqual(story.bounds_for_progress(state.model_dump(), CHAPTERS, EDGES)[BRANCH_A], 10)
        state.completed_scripts = [BRANCH_A, BRANCH_B]
        with self.assertRaisesRegex(ValueError, "同一路径"):
            story.validate_progress(state, CHAPTERS, EDGES)
        with self.assertRaisesRegex(ValueError, "之后"):
            story.validate_progress(story.StoryProgress(script_name=ROOT, completed_scripts=[BRANCH_A]), CHAPTERS, EDGES)

    def test_unknown_progress_and_unlinked_scene_are_conservative(self):
        self.assertEqual(story.bounds_for_progress(None, CHAPTERS, EDGES), {})
        rows = CHAPTERS + [{"script_name": "妃愛-af.ast", "scope": "hiyori", "max_entry": 12}]
        state = story.StoryProgress(route="hiyori", script_name="妃愛-af.ast", entry_no=4)
        self.assertEqual(story.bounds_for_progress(state.model_dump(), rows, EDGES), {"妃愛-af.ast": 4})
        with self.assertRaisesRegex(ValueError, "范围"):
            story.validate_progress(state.model_copy(update={"entry_no": 13}), rows, EDGES)

    def test_loop_does_not_hang_or_claim_optional_paths(self):
        edges = EDGES + [{"source_script": BRANCH_A, "source_entry": 5,
                          "target_script": BRANCH_B, "condition": "loop", "via": ""},
                         {"source_script": BRANCH_B, "source_entry": 5,
                          "target_script": BRANCH_A, "condition": "loop", "via": ""}]
        self.assertEqual(story.graph_info(CHAPTERS, edges)["dominators"][MERGED], {ROOT, MERGED})

    def test_unresolved_internal_choices_do_not_leak_alternate_dialogue(self):
        rows = [{**row, "uncertain_from": 3 if row["script_name"] == MERGED else None} for row in CHAPTERS]
        state = story.StoryProgress(route="hiyori", script_name=MERGED, entry_no=8).model_dump()
        self.assertEqual(story.bounds_for_progress(state, rows, EDGES)[MERGED], 2)

    def test_parser_only_uses_control_commands_and_follows_bridges(self):
        def script(command):
            return f'ast={{\n{{\n{command}\nlang="lang_00003",\n}},\n}}\ntext={{\n[0]={{\n"正文 file=虚构",\n}},\n}}\nlabel={{}}'
        scripts = {ROOT: script('{"excall",file="桥接",cond="f.choice==1"},'),
                   "桥接.ast": script('{"excall",file="妃愛-01"},'),
                   MERGED: script('{"select",file="共通-01",text="select"},')}
        self.assertEqual(transitions(scripts[ROOT])[0]["source_entry"], 3)
        nodes, edges, warnings = build_graph(scripts)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(edges[0]["target_script"], MERGED)
        self.assertEqual(edges[0]["via"], "桥接.ast")
        self.assertIn("f.choice==1", edges[0]["condition"])
        self.assertEqual(warnings, [])

    def test_summary_and_dialogue_share_boundaries_before_similarity_limit(self):
        state = story.StoryProgress(route="hiyori", script_name=MERGED, entry_no=2).model_dump()
        with (patch("storage.story_graph", return_value=(CHAPTERS, EDGES)),
              patch("storage.recall_role_knowledge", return_value=[]) as notes,
              patch("storage.recall_game_dialogue", return_value=[]) as dialogue):
            self.assertEqual(story.recall([1.0] * 512, "和泉妃爱", state), ([], []))
        self.assertEqual(notes.call_args.kwargs["completed_scripts"], [])
        self.assertEqual(dialogue.call_args.kwargs["bounds"], {ROOT: 9, MERGED: 2})
        with patch("storage.recall_game_dialogue") as query:
            self.assertEqual(story.recall([1.0] * 512, "和泉妃爱", None), ([], []))
            query.assert_not_called()

    def test_tool_uses_current_conversation_progress(self):
        state = story.StoryProgress(route="hiyori", script_name=MERGED, entry_no=2).model_dump()
        with (patch("storage.role_knowledge_count", return_value=1),
              patch("storage.game_dialogue_counts", return_value={"chunks": 1}),
              patch("storage.get_conversation", return_value={"story_progress": state}),
              patch("memory_service.embed", return_value=[1.0] * 512),
              patch("story_service.recall", return_value=([], [])) as recall):
            agent_tools.execute("search_character_knowledge", '{"query":"过去的事"}', uuid4(), "和泉妃爱")
        self.assertEqual(recall.call_args.args[2], state)

    def test_progress_api_and_prompt(self):
        identifier = uuid4()
        state = story.StoryProgress(route="hiyori", script_name=MERGED, entry_no=2,
                                    relationship="兄妹，已交往", scene="晚饭后在家").model_dump()
        client = TestClient(app)
        with (patch("storage.get_conversation", return_value={"id": identifier}),
              patch("storage.story_graph", return_value=(CHAPTERS, EDGES)),
              patch("storage.update_story_progress", return_value={"story_progress": state}) as save):
            self.assertEqual(client.put(f"/conversations/{identifier}/story-progress", json=state).status_code, 200)
            bad = {**state, "entry_no": 99}
            self.assertEqual(client.put(f"/conversations/{identifier}/story-progress", json=bad).status_code, 422)
            self.assertEqual(client.post("/story/validate-progress", json=state).json(), state)
            self.assertEqual(client.post("/story/validate-progress", json=bad).status_code, 422)
            self.assertEqual(save.call_count, 1)
        messages = build_model_messages({"name": "和泉妃爱", "system_prompt": "角色卡"}, [], [], "你好",
                                        story_progress=state)
        self.assertIn("智宏", messages[0]["content"])
        self.assertIn("晚饭后在家", messages[0]["content"])
        self.assertIn("截至台词 #2", messages[0]["content"])

    def test_new_chat_saves_progress_and_existing_chat_cannot_override_it_inline(self):
        identifier = uuid4()
        state = story.StoryProgress(route="hiyori", script_name=MERGED, entry_no=2)
        with (patch("storage.get_character", return_value={"name": "和泉妃爱", "system_prompt": "角色卡"}),
              patch("storage.story_graph", return_value=(CHAPTERS, EDGES)),
              patch("storage.create_conversation", return_value={"id": identifier, "story_progress": state.model_dump()}) as create,
              patch("storage.recent_messages", return_value=[]),
              patch("storage.role_knowledge_count", return_value=0),
              patch("storage.game_dialogue_counts", return_value={"chunks": 0}),
              patch("memory_service.recall", return_value=[])):
            result = prepare_chat(ChatRequest(message="你好", story_progress=state))
            create.assert_called_once_with("你好", state.model_dump())
            self.assertIn("截至台词 #2", result[1][0]["content"])
            with patch("storage.get_conversation", return_value={"id": identifier}):
                # 不依赖模型配置；直接验证入口拒绝随聊天隐式改进度。
                with self.assertRaises(HTTPException) as error:
                    prepare_chat(ChatRequest(message="你好", conversation_id=identifier, story_progress=state))
                self.assertEqual(error.exception.status_code, 422)


@unittest.skipUnless(os.getenv("RUN_DB_TEST") == "1", "设置 RUN_DB_TEST=1 才连接真实数据库")
class StoryDatabaseTests(unittest.TestCase):
    def test_sql_filters_before_limit_and_neighbours_stop_at_current_entry(self):
        marker = f"timeline-test-{uuid4()}"
        earlier, future = marker + "-old.ast", marker + "-future.ast"
        vector = [1.0] + [0.0] * 511
        try:
            with storage.connect() as conn:
                for name in [earlier, future]:
                    for entry in range(5):
                        conn.execute("INSERT INTO game_dialogue_line VALUES (%s,%s,'common','测试',%s,'test')",
                                     (name, entry, f"{name} #{entry}"))
                    conn.execute("INSERT INTO game_dialogue_chunk VALUES (%s,0,4,'common','包含未来的原分块',%s::vector)",
                                 (name, storage.vector_literal(vector)))
                conn.execute("INSERT INTO role_knowledge VALUES (%s,'测试角色','fact','未来摘要',%s::vector,now())",
                             ("script\\" + future + ":999", storage.vector_literal(vector)))
            hits = storage.recall_game_dialogue(vector, limit=1, bounds={earlier: 2})
            self.assertEqual([row["script_name"] for row in hits], [earlier])
            expanded = storage.expand_game_dialogue(hits, {earlier: 2})
            self.assertEqual(expanded[0]["last_entry"], 2)
            self.assertNotIn("#3", expanded[0]["content"])
            self.assertNotIn("原分块", expanded[0]["content"])
            self.assertEqual(storage.recall_role_knowledge(vector, "测试角色", completed_scripts=[earlier]), [])
            self.assertEqual(storage.recall_game_dialogue(vector, bounds={}), [])
        finally:
            with storage.connect() as conn:
                conn.execute("DELETE FROM role_knowledge WHERE source_key=%s", ("script\\" + future + ":999",))
                conn.execute("DELETE FROM game_dialogue_chunk WHERE script_name=ANY(%s)", ([earlier, future],))
                conn.execute("DELETE FROM game_dialogue_line WHERE script_name=ANY(%s)", ([earlier, future],))


if __name__ == "__main__":
    unittest.main()
