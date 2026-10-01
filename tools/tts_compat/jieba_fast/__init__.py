"""Windows 试训兼容层：用 jieba 代替没有 cp311 Windows wheel 的 jieba_fast。

日文文本不会调用中文分词；官方代码在导入时仍需要这个模块名。
"""

from jieba import *  # noqa: F403
