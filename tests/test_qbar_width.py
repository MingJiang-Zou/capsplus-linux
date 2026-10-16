# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：Qbar 不许比屏幕还宽（2026-10-16 用户实测「太宽了，都超出屏幕了」）。

根因不在 `set_default_size(680, 420)` 那个数字，而在结果列表：
`Gtk.CellRendererText` **没设 ellipsize**、`TreeViewColumn` 也没 `expand` ——
于是任何一条长历史 URL（我们存的历史就是完整网址）或一行提示文字，都会把 TreeView 的
**自然宽度**顶到屏幕外，窗口跟着膨胀。所以这里钉三层：

1. `logic.window_width()` 的钳位算法（纯函数，headless 可测）；
2. 源码层：渲染器必须带 `ellipsize`、标题列必须 `expand`、宽度必须走 `window_width()`（防回退）；
3. 真构造窗口时的宽度断言 —— 在 `tests/test_settings_window.py` 里（要显示服务器，界面用例门守着）。
"""

import ast
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar.logic import window_width  # noqa: E402  纯逻辑层，不 import gi

APP = REPO / "qbar" / "qbar_app.py"


class TestWindowWidthMath(unittest.TestCase):
    def test_clamps_to_default_when_screen_is_wide(self):
        self.assertEqual(window_width([1920, 1920, 960]), 680)

    def test_uses_the_narrowest_monitor_not_the_widest(self):
        """三块屏 2560/2560/640：按最宽的算就会盖不住小屏，按最窄的算两边都能显示。"""
        self.assertEqual(window_width([2560, 2560, 640]), 576)   # 640 * 0.9

    def test_respects_custom_ratio_and_floor(self):
        self.assertEqual(window_width([800], ratio=0.5), 400)
        self.assertEqual(window_width([200]), 240, "再窄也要留 240 的地板，否则结果没法读")

    def test_bad_monitor_data_falls_back_to_default(self):
        """拿不到显示器信息不能把界面搞崩：None / 空 / 负数 / 非 int 一律退回。"""
        for data in (None, [], [0], [-1920], ["1920"], [None]):
            self.assertEqual(window_width(data), 680, f"该退回 680：{data!r}")


class TestRendererMustEllipsize(unittest.TestCase):
    """源码级防回退：谁把 ellipsize / expand 摘掉，这里立刻红。"""

    def setUp(self):
        self.src = APP.read_text(encoding="utf-8")

    def test_cell_renderer_sets_ellipsize(self):
        self.assertIn('set_property("ellipsize", Pango.EllipsizeMode.END)', self.src,
                      "渲染器没有 ellipsize —— 长历史 URL 会把窗口撑出屏幕")

    def test_title_column_expands(self):
        self.assertIn("col.set_expand(True)", self.src,
                      "标题列没 expand：它会反过来要求窗口变宽，方向正好相反")

    def test_width_comes_from_the_clamp_function(self):
        self.assertIn("window_width(", self.src, "宽度又写死成常数了，应走 window_width()")
        self.assertNotIn("set_default_size(680,", self.src, "不许再写死 680")

    def test_pango_is_required_before_use(self):
        """`Pango.EllipsizeMode` 用了却没 require_version → 入口 ImportError，而单测可能全绿。"""
        if "Pango." not in self.src:
            self.skipTest("源码里没用 Pango，不必检查")
        self.assertIn('gi.require_version("Pango", "1.0")', self.src,
                      "用了 Pango 却没 gi.require_version('Pango','1.0')")
        tree = ast.parse(self.src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "gi.repository":
                imported |= {a.name for a in node.names}
        self.assertIn("Pango", imported, "Pango 没从 gi.repository 导入")


if __name__ == "__main__":
    unittest.main(verbosity=2)
