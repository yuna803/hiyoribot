"""使用合成台词检查本地训练数据的角色与脚本隔离。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.llm_pilot_dataset import (
    build_samples,
    export_dataset,
    deduplicate_targets,
    selected_script,
    split_scripts,
)


def line(entry_no, speaker, content, script="script\\妃愛-01.ast", scope="hiyori"):
    return {
        "script_name": script,
        "entry_no": entry_no,
        "scope": scope,
        "speaker": speaker,
        "content": content,
    }


class LlmDatasetTest(unittest.TestCase):
    def test_repository_output_is_rejected_before_reading_database(self):
        with patch("tools.llm_pilot_dataset._fetch_lines") as fetch:
            with self.assertRaises(ValueError):
                export_dataset(ROOT / "local_training_data")
            fetch.assert_not_called()

    def test_selected_script_accepts_database_names(self):
        self.assertTrue(selected_script("共通-01.ast"))
        self.assertTrue(selected_script("妃愛-01.ast"))
        self.assertTrue(selected_script("script\\妃愛-01.ast"))
        self.assertFalse(selected_script("華乃-01.ast"))

    def test_other_speakers_and_null_cut_context(self):
        rows = [
            line(1, "智宏", "第一个问题"),
            line(2, "妃愛", "第一个回复"),
            line(3, "同学", "切断对话"),
            line(4, "妃愛", "没有智宏上下文"),
            line(5, None, "旁白内容"),
            line(6, "智宏", "新的场景问题"),
            line(7, "妃愛", "新的场景回复"),
        ]

        samples, excluded, raw_hiyori_lines = build_samples(rows, "system prompt")

        self.assertEqual(raw_hiyori_lines, 3)
        self.assertEqual(len(samples), 2)
        self.assertEqual(excluded["missing_tomohiro_context"], 1)
        self.assertEqual(samples[0]["messages"][1:], [
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "第一个回复"},
        ])
        self.assertEqual(samples[1]["messages"][1:], [
            {"role": "user", "content": "新的场景问题"},
            {"role": "assistant", "content": "新的场景回复"},
        ])
        self.assertTrue(all(
            message["role"] != "assistant" or message["content"] in {"第一个回复", "新的场景回复"}
            for sample in samples
            for message in sample["messages"]
        ))

    def test_only_complete_recent_three_rounds_and_hiyori_assistant(self):
        rows = []
        for round_no in range(1, 5):
            rows.extend(
                [
                    line(round_no * 2 - 1, "智宏", f"智宏第{round_no}轮问题"),
                    line(round_no * 2, "妃愛", f"妃爱第{round_no}轮回答"),
                ]
            )

        samples, _, _ = build_samples(rows, "system prompt")
        messages = samples[-1]["messages"]

        self.assertEqual(len(messages), 7)
        self.assertEqual([message["role"] for message in messages], [
            "system", "user", "assistant", "user", "assistant", "user", "assistant"
        ])
        self.assertEqual(messages[1]["content"], "智宏第2轮问题")
        self.assertEqual(messages[-1]["content"], "妃爱第4轮回答")
        self.assertEqual(samples[-1]["entry_no"], 8)

    def test_script_splits_are_disjoint_and_duplicate_prefers_test(self):
        assignments = split_scripts([f"script\\妃愛-{index:02}.ast" for index in range(30)])
        split_sets = {
            split: {script for script, assigned in assignments.items() if assigned == split}
            for split in ("train", "validation", "test")
        }
        self.assertEqual([len(split_sets[name]) for name in ("train", "validation", "test")], [24, 3, 3])
        self.assertFalse(split_sets["train"] & split_sets["validation"])
        self.assertFalse(split_sets["train"] & split_sets["test"])
        self.assertFalse(split_sets["validation"] & split_sets["test"])

        train_script = next(iter(split_sets["train"]))
        validation_script = next(iter(split_sets["validation"]))
        test_script = next(iter(split_sets["test"]))
        samples = [
            {"script_name": train_script, "messages": [{"role": "assistant", "content": "相同\n回复"}]},
            {"script_name": validation_script, "messages": [{"role": "assistant", "content": "相同 回复"}]},
            {"script_name": test_script, "messages": [{"role": "assistant", "content": "相同  回复"}]},
        ]

        kept, duplicate_count = deduplicate_targets(samples, assignments)

        self.assertEqual(duplicate_count, 2)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["script_name"], test_script)


if __name__ == "__main__":
    unittest.main()
