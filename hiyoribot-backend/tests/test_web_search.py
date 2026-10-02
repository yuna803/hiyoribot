"""验证搜索边界、结果上限和失败提示，不依赖真实搜索引擎。"""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from ddgs.exceptions import DDGSException

import agent_tools
import web_search


class WebSearchTests(unittest.TestCase):
    def test_results_drop_unsafe_urls_deduplicate_and_limit_size(self):
        rows = [{"href": url, "title": "不安全", "body": "test"} for url in (
            "javascript:alert(1)", "file:///secret", "http://localhost/private", "http://127.0.0.1/",
            "http://10.0.0.1/", "http://user:secret@example.com/", "https://example.com:bad/",
        )]
        rows += [{"href": "https://example.com/0", "title": "第一条", "body": "x" * 800}]
        rows += [{"href": f"https://example.com/{i}", "title": "标题", "body": "摘要"}
                 for i in range(9)]
        with patch("web_search.DDGS") as client:
            client.return_value.text.return_value = rows
            result = web_search.search("公开关键词")
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(len({row["url"] for row in result["results"]}), 5)
        self.assertEqual(result["results"][0]["title"], "第一条")
        self.assertEqual(len(result["results"][0]["snippet"]), 500)
        self.assertIn("retrieved_at", result)
        self.assertIn("未读取网页全文", result["evidence"])

    def test_upstream_error_is_sanitized_and_not_an_empty_success(self):
        with patch("web_search.DDGS") as client:
            client.return_value.text.side_effect = DDGSException("secret upstream token")
            result = web_search.search("公开关键词")
        self.assertIn("error", result)
        self.assertNotIn("results", result)
        self.assertNotIn("secret", json.dumps(result))

    def test_tool_validates_optional_time_filter_before_search(self):
        with patch("web_search.search", return_value={"results": []}) as search:
            invalid = agent_tools.execute("search_web", '{"query":"版本","timelimit":"forever"}',
                                          uuid4(), "和泉妃爱")
            self.assertIn("error", invalid)
            search.assert_not_called()
            valid = agent_tools.execute("search_web", '{"query":"  最新版本  ","timelimit":"w"}',
                                        uuid4(), "和泉妃爱")
        search.assert_called_once_with("最新版本", "w")
        self.assertEqual(valid, {"results": []})

    def test_search_time_filter_reaches_provider(self):
        with patch("web_search.DDGS") as client:
            client.return_value.text.return_value = []
            web_search.search("官方更新", "w")
        self.assertEqual(client.return_value.text.call_args.kwargs["timelimit"], "w")
        self.assertEqual(client.return_value.text.call_args.kwargs["max_results"], 5)


if __name__ == "__main__":
    unittest.main()
