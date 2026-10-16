# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""渲染器测试：归一化、macro 行硬规矩、keyd 语法自检。"""

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import mapping_data as MD          # noqa: E402
from capsplus.render import render, normalized_bindings  # noqa: E402

# 参考文件（早期手工复刻的 v1 配置，用来做渲染等价验收）；可用环境变量指向别处
REF_CONF = Path(os.environ.get(
    "CAPSPLUS_REF_CONF",
    Path.home() / ".dsh/refs/capslock-v3.3.0/keyd-capslock-plus.conf"))


class TestRender(unittest.TestCase):
    def test_render_nonempty_and_has_sections(self):
        text = render()
        for section in ("[ids]", "[main]", "[capl]", "[capl+alt]"):
            self.assertIn(section, text)
        self.assertTrue(text.endswith("\n"))

    def test_macro_line_has_no_trailing_comment(self):
        """硬坑：macro 行的行尾注释会让 keyd 报 invalid key or action。"""
        for line in render().splitlines():
            stripped = line.strip()
            if "macro(" not in stripped or "=" not in stripped:
                continue          # 头部说明性注释不参与判断
            self.assertFalse(stripped.startswith("#"), f"注解行不该含 macro: {stripped}")
            value = stripped.partition("=")[2].strip()
            self.assertTrue(value.startswith("macro("), f"macro 动作格式异常: {stripped}")
            self.assertTrue(value.endswith(")"), f"macro 行尾有多余内容: {stripped}")
            self.assertNotIn("#", value, f"macro 行带了行尾注释: {stripped}")

    def test_normalized_bindings_rejects_macro_comment(self):
        bad = "[capl]\ne = macro(up up)  # 注释\n"
        with self.assertRaises(ValueError):
            normalized_bindings(bad)

    def test_render_is_deterministic(self):
        self.assertEqual(render(), render())

    def test_composite_layer_after_member_layer(self):
        """keyd 要求 [capl+alt] 写在 [capl] 之后。"""
        text = render()
        self.assertLess(text.index("[capl]"), text.index("[capl+alt]"))

    def test_all_macro_actions_are_wellformed(self):
        for _key, action in [(e[0], e[1]) for rows in [MD.CAPL, MD.CAPL_ALT]
                             for e in rows if not isinstance(e, str)]:
            if action.startswith("macro("):
                self.assertTrue(action.endswith(")"), action)
                inner = action[len("macro("):-1]
                self.assertTrue(inner.strip(), f"空宏: {action}")
                self.assertNotIn("#", inner)


@unittest.skipUnless(REF_CONF.exists(), f"参考配置不存在: {REF_CONF}")
class TestReferenceEquivalence(unittest.TestCase):
    """验收项：渲染结果与参考文件的「绑定集合」必须完全相等。

    归一化 = 去注释、去空行、拆成 (段, 键, 值) 的集合（顺序无关）。
    """

    def test_binding_sets_are_identical(self):
        ref = normalized_bindings(REF_CONF.read_text(encoding="utf-8"))
        got = normalized_bindings(render())
        only_ref = sorted(ref - got)
        only_rendered = sorted(got - ref)
        self.assertEqual(
            (only_ref, only_rendered), ([], []),
            f"只存在于参考文件: {only_ref}\n只存在于渲染结果: {only_rendered}")
        self.assertEqual(len(ref), len(got))
        # 参考文件归一化后是 57 条（capl 30 / capl+alt 25 / main 1 / ids 1）
        self.assertGreater(len(ref), 50, "参考文件解析出的绑定太少，归一化可能坏了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
