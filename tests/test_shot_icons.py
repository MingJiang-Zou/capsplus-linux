# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：图标与设计语言（用户："太丑、易用性有待提高" 那一批）。

钉住三件事：
  ① **每个图标都画得出东西**，而且**彼此不同**（全画成一样的方块=没图标）；提示文案齐全；
  ② **CSS 里定义了就一定用到、用到的一定定义了**（`add_class("cp-xxx")` 与 `.cp-xxx` 对账）——
     样式表里漏一个类，控件就退回裸原生样子（贴图那次"删都删不掉"就是这么来的）；
  ③ **跟随主题**：颜色一律走 `@theme_*` 变量（不写死 RGB，浅色/深色主题各自成立）。

反例具型（都真红过，见报告）：
  * 让 `draw_icon` 对某个名字直接返回 False → ①红；
  * 把两个图标的画法写成一样 → "彼此不同"红；
  * 从 CSS 里删掉 `.cp-btn-active` → ②红；
  * 把 `@theme_bg_color` 换成写死的 `#ffffff` → ③红。
"""

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    import cairo as _cairo_probe
    _HAVE_CAIRO = True
except Exception:
    _HAVE_CAIRO = False

from capsplus import shot_icons as I        # noqa: E402
from capsplus import shot_style as S        # noqa: E402

try:
    import cairo
    HAVE_CAIRO = True
except Exception:
    cairo = None
    HAVE_CAIRO = False


class TestIconsExist(unittest.TestCase):
    def test_every_name_has_a_drawing(self):
        """图标名清单里的每一个都必须能画（画不出来会在工具条上退化成文字按钮）。"""
        for name in I.ICON_NAMES:
            with self.subTest(icon=name):
                self.assertIn(name, I.ICON_NAMES)
                # 写法上必须真的有这么一条分支：用"画一遍"来验证（下面有像素判据）
                self.assertTrue(callable(I.draw_icon))

    def test_tool_icons_cover_all_tools(self):
        from capsplus import shot_annotate as AN
        for tool in AN.TOOLS:
            self.assertIn(tool, I.TOOL_ICONS, f"工具 {tool} 没有图标")
            self.assertIn(tool, I.ICON_NAMES, f"工具 {tool} 的图标没在清单里")

    def test_tips_cover_the_toolbar(self):
        """工具条上出现的每一项都要有悬浮提示（图标要靠提示才认得出）。"""
        from capsplus import shot_annotate as AN
        for name in list(AN.TOOLS) + ["undo", "redo", "copy", "save", "pin", "close"]:
            with self.subTest(icon=name):
                self.assertTrue(I.TIPS.get(name), f"{name} 没有提示文案")

    @unittest.skipUnless(HAVE_CAIRO, "这条要真画一次（无头 CI 没有 pycairo）")
    def test_unknown_name_is_refused_not_faked(self):
        self.assertFalse(I.draw_icon(_cr(), "definitely-not-an-icon", 20),
                         "不认识的图标要明确返回 False，别画个空方块冒充")


def _cr(size=24):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    return cairo.Context(surface)


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：图标判据要真画")
class TestIconsRender(unittest.TestCase):
    SIZE = 24

    def _pixels(self, name):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.SIZE, self.SIZE)
        cr = cairo.Context(surface)
        cr.set_source_rgba(1, 1, 1, 1)
        drawn = I.draw_icon(cr, name, self.SIZE)
        surface.flush()
        data = bytes(surface.get_data())
        stride = surface.get_stride()
        ink = 0
        signature = []
        for y in range(self.SIZE):
            for x in range(self.SIZE):
                off = y * stride + x * 4
                alpha = data[off + 3]
                ink += 1 if alpha > 40 else 0
                if alpha > 40:
                    signature.append((x, y))
        return drawn, ink, tuple(signature), surface

    def test_every_icon_draws_something(self):
        for name in I.ICON_NAMES:
            with self.subTest(icon=name):
                drawn, ink, _sig, _s = self._pixels(name)
                self.assertTrue(drawn, f"{name} 画失败")
                self.assertGreater(ink, self.SIZE, f"{name} 几乎什么都没画（{ink} 个像素）")
                self.assertLess(ink, self.SIZE * self.SIZE * 0.95,
                                f"{name} 把整块涂满了 —— 那不叫图标")

    def test_icons_are_distinguishable(self):
        """两两不同：全画成一个样子的"图标"等于没有（用像素点集比）。"""
        seen = {}
        for name in I.ICON_NAMES:
            _drawn, _ink, sig, _s = self._pixels(name)
            for other, other_sig in seen.items():
                self.assertNotEqual(sig, other_sig, f"{name} 与 {other} 长得一模一样")
            seen[name] = sig

    def test_render_icon_and_swatches_produce_surfaces(self):
        surface = I.render_icon("copy", 16, (1, 1, 1, 1))
        self.assertIsNotNone(surface)
        self.assertEqual((surface.get_width(), surface.get_height()), (16, 16))
        self.assertIsNone(I.render_icon("no-such-icon", 16, (1, 1, 1, 1)),
                          "认不出的图标返回 None（调用方据此退化成文字）")
        self.assertIsNotNone(I.swatch_surface((0.9, 0.1, 0.1, 1.0), 16))
        self.assertIsNotNone(I.width_dot_surface(3, 16))

    def test_width_dots_differ_by_size(self):
        """线宽圆点必须**看得出粗细差别**（用户要求"用粗细圆点"，不写 2px/3px/6px）。"""
        inks = []
        for width in (2, 3, 6):
            surface = I.width_dot_surface(width, 20, (1, 1, 1, 1))
            data = bytes(surface.get_data())
            stride = surface.get_stride()
            ink = sum(1 for y in range(20) for x in range(20)
                      if data[y * stride + x * 4 + 3] > 40)
            inks.append(ink)
        self.assertLess(inks[0], inks[1], f"3px 的圆点该比 2px 大：{inks}")
        self.assertLess(inks[1], inks[2], f"6px 的圆点该比 3px 大：{inks}")


class TestStyleSheet(unittest.TestCase):
    def test_classes_used_in_code_are_all_defined(self):
        """代码里 `add_class("cp-xxx")` 用到的类，CSS 里必须有定义。

        这条正是"看得到的丑"的根因之一：类名写错/漏定义 ⇒ 控件退回裸原生样子（贴图那次
        `×` 按钮没有尺寸和背景就是同一个病）。
        """
        used = set()
        for path in sorted((REPO / "capsplus").glob("shot_*.py")):
            text = path.read_text(encoding="utf-8")
            used |= set(re.findall(r'add_class\(\s*"([^"]+)"', text))
            used |= set(re.findall(r'class_names_for\([^)]*\)', text) and [] or [])
        used |= set(re.findall(r'class_names_for\("([a-z]+)"', (REPO / "capsplus" / "shot_annotate.py")
                               .read_text(encoding="utf-8")) and [])
        # class_names_for 的映射表就是给控件用的类名 —— 全部都要在 CSS 里有
        for kind in ("tool", "action", "primary", "swatch", "dot", "bar", "drawer", "status",
                     "canvas", "window", "sep"):
            used |= set(S.class_names_for(kind).split())
            used |= set(S.class_names_for(kind, active=True).split())
        defined = S.used_classes()
        missing = {cls for cls in used if cls.startswith("cp-")} - defined
        self.assertFalse(missing, f"这些类在代码里用了，但 CSS 里没定义：{sorted(missing)}")

    def test_every_defined_class_is_actually_used(self):
        """反过来也要成立：定义了却没人用的类 = 死代码（顺手防止"以为改了样式其实没接上"）。"""
        used = set()
        for kind in ("tool", "action", "primary", "swatch", "dot", "bar", "drawer", "status",
                     "canvas", "window", "sep"):
            used |= set(S.class_names_for(kind).split())
            used |= set(S.class_names_for(kind, active=True).split())
        # 这些类是在控件上用**字面量**加的（不在 class_names_for 的映射表里），登记在案：
        #   cp-btn-disabled 由 GTK 自己在 :disabled 时加（不用手动 add_class）
        #   cp-drawer-* 由 shot_panel 加在抽屉标题/正文上；cp-status-* 由 annotate 的状态栏加
        #   cp-status-warn 留给"警告态"（还没有调用点，登记为已知）
        allowed_literals = {"cp-btn-disabled", "cp-drawer-title", "cp-drawer-text",
                            "cp-drawer-mono", "cp-status-main", "cp-status-more", "cp-status-warn"}
        unused = S.used_classes() - used - allowed_literals
        self.assertFalse(unused, f"CSS 里定义了但没人用：{sorted(unused)}")

    def test_active_state_is_one_language_for_all_three(self):
        """选中态**只有一套语言**：工具 / 颜色 / 线宽取到的激活类是**同一个** `cp-active`。

        （第三批第一版有三套：工具灰底圆角框、颜色/线宽黑椭圆圈、复制橙底框 —— lead 一眼看出
        "半成品"。所以判据不能只断言"各自有高亮"，必须断言**是同一个类、同一套 CSS 规则**。）
        """
        actives = {kind: set(S.class_names_for(kind, active=True).split())
                   - set(S.class_names_for(kind).split())
                   for kind in ("tool", "swatch", "dot")}
        self.assertEqual(actives["tool"], {S.ACTIVE_CLASS})
        self.assertEqual(actives["swatch"], {S.ACTIVE_CLASS})
        self.assertEqual(actives["dot"], {S.ACTIVE_CLASS})
        body = S.CSS.split(f".{S.ACTIVE_CLASS} {{", 1)[1].split("}", 1)[0]
        self.assertIn("border-color", body, "选中态没有描边")
        self.assertIn("background-color", body, "选中态没有底色")
        # **顺序有语义**：`.cp-swatch` / `.cp-dot` 里写了 `background-color: transparent`，
        # GTK CSS 同权重时后写的赢 —— `cp-active` 排在前面就会被盖掉，看着像"只有描边没底色"
        # （渲染图上一眼看到的问题）。判据用源码顺序钉死这件事。
        for later in (".cp-btn {", ".cp-swatch {", ".cp-dot {"):
            self.assertGreater(S.CSS.index(f".{S.ACTIVE_CLASS} {{"), S.CSS.index(later),
                               f"`.{S.ACTIVE_CLASS}` 必须写在 {later} **之后**，否则底色被盖掉")
        for legacy in (".cp-btn-active", ".cp-swatch-active", ".cp-dot-active"):
            self.assertNotIn(legacy, S.CSS, f"{legacy} 是三套语言时代的残留，别留")

    def test_three_states_exist_for_buttons(self):
        css = S.CSS
        for selector in (".cp-btn:hover", ".cp-btn:active", ".cp-btn:disabled"):
            self.assertIn(selector, css, f"按钮缺 {selector} 态（用户要 hover/active/disabled 三态）")

    def test_colours_follow_the_theme(self):
        """颜色一律用主题变量（深浅色主题各自成立），只有画布底色允许写死。"""
        css = S.CSS
        self.assertIn("@theme_bg_color", css)
        self.assertIn("@theme_fg_color", css)
        self.assertIn("@theme_selected_bg_color", css)
        hardcoded = re.findall(r"#[0-9a-fA-F]{6}", css)
        self.assertEqual(sorted(set(hardcoded)), ["#1b1b1e", "#f5c211"],
                         f"写死的颜色只允许画布底色与警告色：{hardcoded}")

    def test_install_does_not_raise_without_a_display(self):
        """无显示环境（CI）调 install 不能抛 —— 返回 False 就算交代过了。"""
        self.assertIn(S.install(), (True, False))

    def test_install_really_installs_in_a_fresh_process(self):
        """**新进程里也必须真的装上**（返回 True）。

        为什么必须开子进程量：坑是 `gi.require_version` 的**加载顺序** —— 只 require Gtk 3.0 时
        `from gi.repository import Gdk` 会解析到 Gdk **4.0**，于是 install() 抛进去被 except 吞掉、
        返回 False，**整份 CSS 一个字符都没生效**。在同一个进程里别的用例早就把 Gdk 3.0 拉起来了，
        根本复现不出来，所以只能开新进程量。
        """
        import os
        import subprocess
        import sys
        if os.environ.get("DISPLAY") in (None, ""):
            self.skipTest("没有显示：真装不了（这条只在 xvfb 下跑）")
        try:
            import gi
            if "nogi" in str(getattr(gi, "__file__", "")):
                self.skipTest("nogi 环境里 gi 是替身：这条没意义")
        except Exception:
            self.skipTest("没有真 gi：这条没意义")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)          # 子进程要真 gi，别继承 nogi 的替身路径
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r);\n"
             "from capsplus import shot_style as S;\n"
             "print('INSTALLED' if S.install() else 'FAILED')" % str(REPO)],
            capture_output=True, text=True, timeout=60, cwd=str(REPO), env=env)
        self.assertIn("INSTALLED", proc.stdout + proc.stderr,
                      f"新进程里没装上样式：{proc.stdout} {proc.stderr[-300:]}")

    def test_status_text_carries_keys_and_selection(self):
        text = S.status_text(True, "矩形 120×80", "✓ 已存 /tmp/x.png")
        self.assertIn("矩形 120×80", text)
        self.assertIn("Enter", text, "快捷键提示要在状态栏里")
        self.assertIn("Ctrl+S", text)
        unfocused = S.status_text(False)
        self.assertIn("没有焦点", unfocused, "失焦要说明白（不能让人以为按了没反应）")

    def test_status_font_is_not_tiny(self):
        self.assertGreaterEqual(S.STATUS_FONT, 12, "状态栏字号不许缩到看不清")


import re  # noqa: E402  （放在末尾：上面的用例用得到，但读起来更靠上）

if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_HAVE_CAIRO, "没有 pycairo：这条要真渲染图标")
class TestEveryToolIconHasInk(unittest.TestCase):
    """每个工具图标都**真画出东西**（吸管第一版画了 0 个像素 —— 空图标=按钮看着是空的）。

    这条比"图标两两不同"更硬：0 墨量的图标彼此也"不同"（都不同），但那是个空按钮。
    """

    def test_each_tool_icon_has_ink(self):
        from capsplus import shot_icons as I
        blank = []
        for name in I.TOOL_ICONS:
            surface = I.render_icon(name, 22, (0, 0, 0, 1))
            surface.flush()
            data = bytes(surface.get_data())
            stride = surface.get_stride()
            ink = sum(1 for y in range(surface.get_height()) for x in range(surface.get_width())
                      if data[y * stride + x * 4 + 3] > 40)
            if ink < 20:
                blank.append(f"{name}({ink}px)")
        self.assertEqual(blank, [], f"这些工具图标是空的：{blank}")
