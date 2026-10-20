# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""术语替换表的单测（`capsplus/voice_glossary.py`）。

为什么这批判据值得写（2026-10-20）：本机 SenseVoice int8 **没有热词能力**
（不是"懒得配"，是这个模型没有那条入口，理由写在模块 docstring 里），所以"专名总是认错"
只剩一层确定性后处理可用 —— 而一层会自动改写用户话的东西，判据的重点**不在"能不能替换"，
在"什么时候绝不许替换"**：

  * `JSQ` 不许命中 `JSQX` / `xJSQy`（ASCII 要词边界）；
  * 单字键一律丢弃（中文不分词，一个字能命中一切）；
  * 最长键优先（`JSQ` 不许抢走 `JSQX` 的地盘）；
  * 改完即止，**不许级联**（`A→B`、`B→C` 同时存在时 "A" 只能变 "B"）。

反例具型（`TestGuardHasTeeth`）逐条对着上面的"不许"，把规则拿掉必须红。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_glossary as G       # noqa: E402


def table(tmpdir: str, text: str) -> Path:
    path = Path(tmpdir) / "glossary.tsv"
    path.write_text(text, encoding="utf-8")
    return path


class TestParse(unittest.TestCase):
    def test_only_two_column_tab_lines_count(self):
        text = ("# 注释\n\n帮晒\tBonsai\n只用空格 分隔\n单列\n"
                "  前有空格\t后有空格  \n")
        self.assertEqual(G.parse_tsv(text), [("帮晒", "Bonsai"), ("前有空格", "后有空格")])

    def test_a_broken_line_does_not_take_the_table_down(self):
        self.assertEqual(G.parse_tsv("\t只有右边\n只有左边\t\n正常\t对\n"),
                         [("正常", "对")])


class TestMatchingRules(unittest.TestCase):
    """三条"不许"的正面表述。"""

    def setUp(self):
        self.g = G.Glossary([("JSQ", "GSQ"), ("帮晒", "Bonsai"), ("开元万岁", "开源万岁")])

    def test_ascii_keys_need_a_word_boundary(self):
        self.assertEqual(self.g.apply("用JSQ模型")[0], "用GSQ模型")
        self.assertEqual(self.g.apply("JSQX 不认识")[0], "JSQX 不认识",
                         "JSQ 命中 JSQX 了 —— 那会把型号名改坏")
        self.assertEqual(self.g.apply("xJSQy")[0], "xJSQy", "左边没边界也算命中，改到标识符里去了")

    def test_single_character_keys_are_dropped(self):
        g = G.Glossary([("万", "萬"), ("帮", "B")])
        self.assertEqual(len(g), 0, "单字键必须被丢掉：中文不分词，一个字会命中一切")
        self.assertEqual(g.apply("万岁，帮忙")[0], "万岁，帮忙")

    def test_longest_key_wins(self):
        g = G.Glossary([("JSQ", "GSQ"), ("JSQX", "GSQX")])
        self.assertEqual(g.apply("JSQX")[0], "GSQX", "短键抢走了长键的地盘")
        self.assertEqual(g.apply("JSQ")[0], "GSQ")

    def test_replacement_never_cascades(self):
        g = G.Glossary([("A", "B"), ("BB", "C")])          # A 太短会被丢，换成合法的两字键
        g = G.Glossary([("甲甲", "乙乙"), ("乙乙", "丙丙")])
        out, hits = g.apply("甲甲")
        self.assertEqual(out, "乙乙", "级联了：替换产生的新文本又被扫了一遍")
        self.assertEqual(hits, [("甲甲", "乙乙")])

    def test_hits_are_reported_per_occurrence(self):
        out, hits = self.g.apply("帮晒和帮晒")
        self.assertEqual(out, "Bonsai和Bonsai")
        self.assertEqual(hits, [("帮晒", "Bonsai"), ("帮晒", "Bonsai")])

    def test_identical_and_empty_pairs_are_ignored(self):
        g = G.Glossary([("一样", "一样"), ("", "空"), ("空", "")])
        self.assertEqual(len(g), 0)
        self.assertEqual(g.apply("一样")[0], "一样")

    def test_empty_table_is_a_pass_through(self):
        g = G.Glossary([])
        self.assertFalse(g)
        self.assertEqual(g.apply("原样")[0], "原样")
        self.assertEqual(g.apply("")[0], "")


class TestLoadAndEnv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop(G.ENV, None)

    def test_the_builtin_table_ships_and_parses(self):
        path = G.builtin_path()
        self.assertTrue(path.exists(), f"内置表不在仓里：{path}")
        pairs = G.parse_tsv(path.read_text(encoding="utf-8"))
        self.assertTrue(pairs, "内置表一条都没有 —— 那等于这层功能没上")

    def test_env_path_replaces_the_builtin(self):
        path = table(self._tmp.name, "自定义\tCUSTOM\n")
        os.environ[G.ENV] = str(path)
        g = G.load()
        self.assertEqual(g.apply("自定义")[0], "CUSTOM")
        self.assertEqual(g.apply("帮晒")[0], "帮晒", "指了别的表却还带着内置的条目")

    def test_a_missing_file_is_an_empty_table_not_a_crash(self):
        os.environ[G.ENV] = str(Path(self._tmp.name) / "不存在.tsv")
        self.assertEqual(len(G.load()), 0)

    def test_off_values_disable_the_layer(self):
        for value in ("0", "off", "none", "no", "false", "OFF"):
            os.environ[G.ENV] = value
            self.assertTrue(G.disabled(), f"{value!r} 没被当成关闭")
            self.assertEqual(len(G.load()), 0)

    def test_user_table_overrides_the_builtin_key(self):
        user = G.user_path()
        if user.exists():
            self.skipTest(f"本机真有用户表 {user}，这条会动到明江的东西")
        with mock.patch.object(G, "user_path", return_value=table(self._tmp.name, "帮晒\t我的写法\n")):
            os.environ[G.ENV] = ""
            g = G.load()
        self.assertEqual(g.apply("帮晒")[0], "我的写法", "用户表里的同名键没盖过内置")

    def test_describe_never_leaks_the_entries(self):
        path = table(self._tmp.name, "机密专名\tSECRET\n")
        os.environ[G.ENV] = str(path)
        text = G.describe()
        self.assertIn("1 条", text)
        self.assertNotIn("机密专名", text)
        self.assertNotIn("SECRET", text)


class TestGuardHasTeeth(unittest.TestCase):
    """反例具型：把规则拿掉，对应的断言必须真的红。

    三条规则各有一个"要是没有它会怎样"的现场，写成对照 —— 这样"判据有没有牙"是**可复算**的，
    不必靠读代码相信。
    """

    def test_naive_replace_would_break_identifiers(self):
        """场地证明：朴素的 `str.replace` 会命中 JSQX，而我们的表不会。"""
        text = "JSQX 与 JSQ"
        naive = text.replace("JSQ", "GSQ")
        self.assertEqual(naive, "GSQX 与 GSQ")
        ours = G.Glossary([("JSQ", "GSQ")]).apply(text)[0]
        self.assertEqual(ours, "JSQX 与 GSQ",
                         "这条红了说明词边界失效了 —— 那型号名会被改坏")

    def test_cascade_is_really_absent(self):
        """场地证明：级联实现会把 甲甲 一路滑到 丙丙。"""
        g = G.Glossary([("甲甲", "乙乙"), ("乙乙", "丙丙")])
        self.assertNotEqual(g.apply("甲甲")[0], "丙丙",
                            "这条红了说明替换被反复扫了（会无限漂、也能互相喂）")

    def test_short_key_guard_is_load_bearing(self):
        """场地证明：不丢单字键时，"万" 会把一切含它的词改掉。"""
        text = "万岁，万不得已"
        self.assertEqual(G.Glossary([("万", "萬")]).apply(text)[0], text)
        self.assertEqual(text.replace("万", "萬"), "萬岁，萬不得已",
                         "没有这条护栏时就是这个样子")


if __name__ == "__main__":
    unittest.main(verbosity=2)