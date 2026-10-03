"""记忆确认、摘要并发、实际引用、取消与配音阶段的回归验证。"""

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

import agent_service
import agent_tools
import generation_control
from main import app
import storage
import summary_service
import tts_jobs


class DailyTests(unittest.TestCase):
    def test_cancel_stream_closes_upstream_and_never_saves_half_turn(self):
        started,closed = Event(),Event()

        class Stream:
            def __iter__(self):
                started.set()
                yield NS(choices=[NS(delta=NS(content="半句",tool_calls=None),finish_reason=None)])
                closed.wait(3)
                yield NS(choices=[NS(delta=NS(content="完整回复",tool_calls=None),finish_reason="stop")])

            def close(self):
                closed.set()

        stream = Stream()
        client = Mock()
        client.chat.completions.create.return_value = stream
        client.close.side_effect = stream.close
        config = NS(api_key="test",model="test",base_url="https://example.test",
                    auto_extract_memory=False,auto_summarize=False)
        identifier,conversation = uuid4(),uuid4()
        with (patch("main.resolve_config",return_value=config),
              patch("main.prepare_chat",return_value=(conversation,[{"role":"system","content":"角色"}],[],"角色",{})),
              patch("main.OpenAI") as sdk,patch("storage.save_turn") as save):
            sdk.return_value.__enter__.return_value = client
            web = TestClient(app)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(web.post,"/chat/stream",json={"message":"测试","generation_id":str(identifier)})
                self.assertTrue(started.wait(2))
                response = web.post(f"/generation/{identifier}/cancel")
                self.assertTrue(response.json()["accepted"])
                result = future.result(timeout=3)
            self.assertIn("event: cancelled",result.text)
            save.assert_not_called()
            self.assertTrue(closed.is_set())

    def test_cancel_before_start_and_save_commit_boundary(self):
        identifier = uuid4()
        self.assertTrue(generation_control.get(identifier).cancel()["accepted"])
        with self.assertRaises(generation_control.GenerationCancelled):
            generation_control.register(identifier).check()
        job = generation_control.Generation()
        job.begin_save()
        self.assertFalse(job.cancel()["accepted"])
        self.assertFalse(job.cancelled.is_set())

    def test_trace_reports_only_references_retained_after_context_trim(self):
        client = Mock()
        client.chat.completions.create.return_value = NS(choices=[NS(
            message=NS(content="你好",tool_calls=None),finish_reason="stop")])
        dialogue = {"script_name":"合成脚本.ast","first_entry":0,"last_entry":2,"content":"合成台词","similarity":1.0}
        messages = [{"role":"system","content":"角色"},
                    {"role":"system","content":"[合成脚本.ast #0-2]\n合成台词"},
                    {"role":"user","content":"你好"}]
        with patch("llm_runtime.fit_messages",side_effect=lambda rows,*_args: [rows[0],rows[-1]]):
            result = list(agent_service.run(client,"test",messages,uuid4(),"角色",{},stream=False,
                tools_enabled=False,config=NS(),recalled_context={"dialogue":[dialogue]}))[-1][1]
        self.assertEqual(result["recalled_context"]["rounds"][0]["dialogue"],[])
        self.assertNotIn("合成台词",str(client.chat.completions.create.call_args.kwargs["messages"]))

    def test_story_tool_keeps_the_turn_snapshot_when_live_progress_changes(self):
        with (patch("storage.role_knowledge_count",return_value=1),
              patch("storage.game_dialogue_counts",return_value={"chunks":1}),
              patch("storage.get_conversation",return_value={"story_progress":{"script_name":"后期章节"}}),
              patch("memory_service.embed",return_value=[1.0]+[0.0]*511),
              patch("story_service.recall",return_value=([],[])) as recall):
            agent_tools.execute("search_character_knowledge",'{"query":"过去的事"}',uuid4(),"和泉妃爱",story_progress=None)
        self.assertIsNone(recall.call_args.args[2])

    def test_summary_uses_auxiliary_model_and_rejects_stale_write(self):
        config = NS(api_key="test",model="chat",base_url="https://example.test")
        row = {"summary":"旧摘要","summary_pending_count":12,"summary_revision":7,"story_progress":None}
        source = [{"id":1,"role":"user","content":"这是一个提议"},{"id":2,"role":"assistant","content":"回应提议"}]
        response = NS(choices=[NS(message=NS(content='{"scene":"哥哥提出想法，尚未发生","agreements":[],"open_topics":["继续商量"]}'),finish_reason="stop")])
        with (patch("storage.summary_source",return_value=(row,source)),
              patch("summary_service.OpenAI") as client,
              patch("storage.save_summary",return_value=False) as save):
            client.return_value.__enter__.return_value.chat.completions.create.return_value=response
            with self.assertRaisesRegex(ValueError,"已变化"):
                summary_service.refresh(uuid4(),config)
            self.assertEqual(save.call_args.args[-1],7)

    def test_summary_cannot_promote_assistant_proposal_to_user_agreement(self):
        summary = summary_service.Summary(scene="模型猜测的地点",agreements=["明天一起去公园","陪妹妹试音"],open_topics=["讨论试音"])
        text = summary.text({"story_progress":{"scene":"用户指定的客厅"},"summary_user_texts":["我们约好明天一起去公园"],"summary_origin":"auto"})
        self.assertIn("当前情境（用户设置）：用户指定的客厅",text)
        self.assertIn("用户原话中的安排与意愿：我们约好明天一起去公园",text)
        self.assertIn("提议或未核实事项：陪妹妹试音",text)
        self.assertNotIn("模型猜测的地点",text)
        negative=summary.text({"summary_user_texts":["我不想陪妹妹试音。"],"summary_origin":"auto"})
        self.assertIn("用户原话中的安排与意愿：我不想陪妹妹试音。",negative)

    def test_tts_job_shows_stages_and_failure_does_not_leak_exception(self):
        started,release = Event(),Event()

        def speech(_text,_config,*,progress):
            progress("translating")
            progress("synthesizing")
            started.set()
            release.wait(2)
            raise RuntimeError("secret-key-from-upstream")

        with patch("tts_service.create_speech",side_effect=speech):
            task = tts_jobs.create("测试",NS())
            self.assertTrue(started.wait(1))
            self.assertEqual(tts_jobs.get(task["id"])["stage"],"synthesizing")
            release.set()
            # executor 队列哨兵保证前一任务已结束，无需固定 sleep。
            tts_jobs._executor.submit(lambda:None).result(timeout=2)
            result = tts_jobs.get(task["id"])
            self.assertEqual(result["stage"],"failed")
            self.assertNotIn("secret",result["error"])


@unittest.skipUnless(os.getenv("RUN_DB_TEST")=="1","设置 RUN_DB_TEST=1 才连接真实数据库")
class DailyDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.marker = "daily-test-"+str(uuid4())
        self.first = storage.create_conversation(self.marker+" A")["id"]
        self.second = storage.create_conversation(self.marker+" B")["id"]
        self.vector = [1.0]+[0.0]*511

    def tearDown(self):
        with storage.connect() as conn:
            conn.execute("DELETE FROM memory WHERE content LIKE %s",(self.marker+"%",))
            conn.execute("DELETE FROM conversation WHERE id=ANY(%s)",([self.first,self.second],))

    def test_confirmation_scope_hypothesis_and_explicit_correction(self):
        real = storage.add_memory(self.marker+"现实喜好",3,self.vector)
        actor = storage.add_memory(self.marker+"角色约定",3,self.vector,kind="roleplay",conversation_id=self.first)
        hypothesis = storage.add_memory(self.marker+"假设",3,self.vector,kind="hypothetical",status="pending")
        candidate = storage.add_memory(self.marker+"新现实喜好",3,self.vector,status="pending")
        ids = {real["id"],actor["id"],hypothesis["id"],candidate["id"]}
        first = {r["id"] for r in storage.recall_memories(self.vector,limit=100,conversation_id=self.first)} & ids
        second = {r["id"] for r in storage.recall_memories(self.vector,limit=100,conversation_id=self.second)} & ids
        self.assertEqual(first,{real["id"],actor["id"]})
        self.assertEqual(second,{real["id"]})
        with self.assertRaisesRegex(ValueError,"同类"):
            storage.review_memory(candidate["id"],"real","confirmed",None,[actor["id"]])
        storage.review_memory(candidate["id"],"real","confirmed",None,[real["id"]])
        entries={r["id"]:r for r in storage.list_memories() if r["id"] in ids}
        self.assertEqual(entries[real["id"]]["status"],"superseded")
        self.assertEqual(entries[candidate["id"]]["replaces_ids"],[real["id"]])
        again=storage.add_memory(self.marker+"现实喜好",3,self.vector,status="pending")
        self.assertNotEqual(again["id"],real["id"])
        reviewed=storage.review_memory(hypothesis["id"],"hypothetical","confirmed",None,[])
        self.assertEqual(reviewed["status"],"rejected")
        # 归档条目不占用活动去重键，后续重新提出同样的假设仍能留下新来源。
        archived = storage.add_memory(self.marker+"归档假设",3,self.vector,kind="hypothetical",status="rejected")
        retry = storage.add_memory(self.marker+"归档假设",3,self.vector,kind="hypothetical",status="pending")
        self.assertNotEqual(archived["id"],retry["id"])

    def test_summary_keeps_recent_turns_and_progress_change_invalidates_stale_summary(self):
        for index in range(20):
            storage.save_turn(self.first,f"{self.marker} 问{index}",f"答{index}",
                              recalled_context={"rounds":[{"dialogue":[]}]})
        row,messages=storage.summary_source(self.first)
        self.assertEqual(row["summary_pending_count"],16)
        self.assertEqual(len(messages),16)
        self.assertEqual(messages[-1]["role"],"assistant")
        self.assertTrue(storage.save_summary(self.first,"已确认的约定",messages[-1]["id"],row["summary_revision"]))
        snapshot=storage.get_conversation(self.first)
        storage.update_story_progress(self.first,{"scene":"新场景"})
        self.assertFalse(storage.save_summary(self.first,"过期摘要",messages[-1]["id"],snapshot["summary_revision"]))
        self.assertEqual(storage.get_conversation(self.first)["summary"],"")
        self.assertEqual(storage.list_messages(self.first)[1]["recalled_context"],{"rounds":[{"dialogue":[]}]})


if __name__=="__main__":
    unittest.main()
