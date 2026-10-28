"""日常聊天以用户人设为核心，原作召回是可丢弃的辅助参考。"""
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

import agent_tools
import llm_runtime
import story_service
from main import AppConfig, CharacterUpdate, build_model_messages


class ChatReferenceTests(unittest.TestCase):
    def test_full_core_is_retained_and_natural_chat_rules_override_template(self):
        card=json.loads((Path(__file__).resolve().parents[2]/'rolecards/izumi-hiyori.json').read_text(encoding='utf8'))
        validated=CharacterUpdate.model_validate(card)
        self.assertGreater(len(validated.system_prompt),8000)
        messages=build_model_messages(card,[],[],'今天有点累',knowledge_mode='reference')
        system=messages[0]['content']
        self.assertIn('不照顾哥哥就会状态失常',system)
        self.assertIn('隐藏机制（不主动展示）',system)
        self.assertNotIn('每次回复按以下顺序输出',system)
        self.assertNotIn('每10轮对话中，最多触发3次推进事件',system)
        self.assertIn('无需固定五段输出',system)
        self.assertIn('不需要用户选择路线或章节',system)
        self.assertNotIn('尚未设置，不能声称原作事件已经发生',system)

    def test_reference_recall_does_not_require_a_chapter_or_story_graph(self):
        hit=dict(script_name='妃愛-01.ast',first_entry=2,last_entry=5,similarity=.8)
        with patch('storage.story_graph',side_effect=AssertionError('不应依赖剧情图')),\
             patch('storage.recall_role_knowledge',return_value=[dict(content='合成语气参考',similarity=.8)]) as notes,\
             patch('storage.recall_game_dialogue',return_value=[hit]) as dialogue,\
             patch('storage.expand_game_dialogue',return_value=[hit]) as expand:
            n,d=story_service.reference_recall([1.0]*512,'和泉妃爱')
        self.assertEqual(len(n),1)
        self.assertEqual(d,[hit])
        self.assertNotIn('completed_scripts',notes.call_args.kwargs)
        self.assertNotIn('bounds',dialogue.call_args.kwargs)
        self.assertEqual(expand.call_args.args[1],{'妃愛-01.ast':8})

    def test_tool_uses_same_reference_path_and_not_current_plot(self):
        with patch('storage.role_knowledge_count',return_value=1),\
             patch('storage.game_dialogue_counts',return_value={'chunks':1}),\
             patch('memory_service.embed',return_value=[1.0]*512),\
             patch('storage.get_conversation') as conversation,\
             patch('story_service.reference_recall',return_value=([],[])) as recall:
            result=agent_tools.execute('search_character_knowledge','{"query":"妃爱说话方式"}',uuid4(),'和泉妃爱',knowledge_mode='reference')
        self.assertEqual(result['character'],'和泉妃爱')
        recall.assert_called_once()
        conversation.assert_not_called()

    def test_budget_drops_optional_original_excerpts_before_chat_history(self):
        class Counter:
            def encode(self,text,**kwargs):
                return NS(ids=range(len(text)))
        config=AppConfig(provider='local',local_context_tokens=4096)
        messages=[dict(role='system',content='核心设定'),
                  dict(role='system',content='以下是原作汉化对话'+'参考'*2100),
                  dict(role='user',content='上一轮我说自己今天有点累'),
                  dict(role='assistant',content='合成回应'),dict(role='user',content='继续刚才的话题')]
        with patch('llm_runtime._tokenizer',return_value=Counter()):
            fitted=llm_runtime.fit_messages(messages,config)
        self.assertEqual([m['role'] for m in fitted],['system','user','assistant','user'])
        self.assertEqual(fitted[1],messages[2])

    def test_16k_supported_but_larger_context_requires_another_change(self):
        self.assertEqual(AppConfig(local_context_tokens=16384).local_context_tokens,16384)
        with self.assertRaises(ValueError):
            AppConfig(local_context_tokens=32768)
