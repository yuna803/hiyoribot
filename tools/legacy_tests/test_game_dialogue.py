"""剧本解析只用合成文本，不在代码库保存原作台词。"""

import unittest

from import_game_dialogue import chunk_lines, parse_script, selected_script


class GameDialogueTest(unittest.TestCase):
    def test_parser_keeps_speaker_order_and_multiline_text(self) -> None:
        script = '''ast={
}
text={
[1]={
name={name="甲"},
ja={
{
"第一句",
"第二句",
},
},
},
[2]={
ja={
{
"旁白内容",
},
},
},
[3]={
ja={
},
},
}
label={
}
'''
        slots, lines = parse_script("共通-01.ast", script.encode("utf-8"))
        self.assertEqual(slots, 3)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].speaker, "甲")
        self.assertEqual(lines[0].content, "第一句\n第二句")
        self.assertEqual(lines[1].entry_no, 2)
        self.assertIsNone(lines[1].speaker)
        chunks = chunk_lines(lines)
        self.assertEqual(len(chunks), 1)
        self.assertIn("旁白：旁白内容", chunks[0][4])

    def test_only_selected_routes_are_imported(self) -> None:
        self.assertTrue(selected_script("script\\妃愛-01.ast"))
        self.assertTrue(selected_script("script\\共通-01.ast"))
        self.assertFalse(selected_script("script\\妃愛hシーン01.ast"))
        self.assertFalse(selected_script("script\\華乃-01.ast"))


if __name__ == "__main__":
    unittest.main()
