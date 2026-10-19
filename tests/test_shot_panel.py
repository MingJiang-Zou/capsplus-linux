# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：结果面板（第二批）—— 后端标签、原文/译文两栏、复制与"重跑到翻译"的口径。

三条口径（每条在这个文件里都有对应的红法）：
  ① **后端必须一眼看清**：`headline()` 里必须出现"本地"或"云"；把后端标签去掉就红。
  ② **出错要有可操作的下一步**：`set_error` 只收话术，话术由 provider 给；没有话术时
     `normalize_result` 会补一句"没带原因"，不许静默。
  ③ **"送去翻译"永远用 OCR 原文**：`translate_input()` 只读 `ocr_text`；把它改成"读译文"就红
     —— 那会变成"越翻越走样"的死循环。
"""

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_panel as SP          # noqa: E402

RUN_GUI = __import__("os").environ.get("CAPSPLUS_GUI") == "1"
ALLOW_WAYLAND_DESKTOP = __import__("os").environ.get("CAPSPLUS_WAYLAND") == "1"



def desktop_risk() -> str:
    """GDK 连上的到底是不是**真实 Wayland 桌面**：是、又没显式授权，就别跑界面用例。

    口径与 `tests/test_settings_window.py` / `tests/test_shot_overlay.py` 一致：
    这台机是明江干活的机器，判据不许把窗口弹到他屏幕上（`CAPSPLUS_WAYLAND=1` 才放行）。
    """
    if not RUN_GUI:
        return "没设 CAPSPLUS_GUI=1"
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk
    except Exception as exc:
        return f"import gi 失败：{exc}"
    disp = Gdk.Display.get_default()
    if disp is None:
        return "连不上显示服务器"
    backend = type(disp).__name__
    if "Wayland" in backend and ALLOW_WAYLAND_DESKTOP is False:
        return f"GDK 连的是真实 Wayland 桌面（{backend}）：按口径拒跑"
    return ""


GUI_SKIP = desktop_risk()

class TestBackendLabelling(unittest.TestCase):
    def test_labels_are_chinese_and_cover_both_paths(self):
        self.assertEqual(SP.backend_label(SP.BACKEND_LOCAL), "本地")
        self.assertEqual(SP.backend_label(SP.BACKEND_CLOUD), "云")

    def test_unknown_backend_is_not_faked_as_local(self):
        self.assertEqual(SP.backend_label("somemodel-27b"), "somemodel-27b",
                         "认不出来的后端不许被贴上『本地』的标签（那是在骗人）")
        self.assertEqual(SP.backend_label(""), "未指定")

    def test_headline_always_names_the_backend(self):
        m = SP.PanelModel()
        m.set_ocr("你好", SP.BACKEND_CLOUD, ms=820, note="baidu accurate_basic")
        self.assertIn("OCR 识别", m.headline())
        self.assertIn("云", m.headline())
        self.assertIn("820 ms", m.headline())
        self.assertIn("baidu", m.headline())

    def test_translation_headline_names_local(self):
        m = SP.PanelModel()
        m.set_translation("hello", "你好", SP.BACKEND_LOCAL, ms=1500, note="qwen3.8-27b")
        self.assertIn("翻译", m.headline())
        self.assertIn("本地", m.headline())

    def test_switch_label_says_where_it_goes(self):
        m = SP.PanelModel()
        m.backend = SP.BACKEND_LOCAL
        self.assertIn("云", m.switch_label())
        m.backend = SP.BACKEND_CLOUD
        self.assertIn("本地", m.switch_label())
        self.assertEqual(SP.other_backend(SP.BACKEND_LOCAL), SP.BACKEND_CLOUD)
        self.assertEqual(SP.other_backend(SP.BACKEND_CLOUD), SP.BACKEND_LOCAL)


class TestTwoColumnsAndCopy(unittest.TestCase):
    def test_translation_view_toggles_between_src_and_dst(self):
        m = SP.PanelModel()
        m.set_translation("hello", "你好", SP.BACKEND_LOCAL)
        self.assertEqual(m.view, SP.KIND_TRANSLATE)
        self.assertEqual(m.body(), "你好")
        self.assertEqual(m.view_label(), "看原文", "按钮上写的是『切过去会看到什么』")
        m.view = SP.KIND_OCR
        self.assertEqual(m.body(), "hello")
        self.assertEqual(m.view_label(), "看译文")

    def test_copy_prefers_the_translation(self):
        m = SP.PanelModel()
        m.set_ocr("hello", SP.BACKEND_CLOUD)
        self.assertEqual(m.primary_text(), "hello", "还没翻译就复制原文")
        m.set_translation("hello", "你好", SP.BACKEND_LOCAL)
        self.assertEqual(m.primary_text(), "你好", "有译文就复制译文（人通常就是要这个）")

    def test_copy_is_disabled_when_there_is_nothing(self):
        m = SP.PanelModel()
        self.assertFalse(m.can_copy())
        m.set_ocr("   ", SP.BACKEND_CLOUD)
        self.assertFalse(m.can_copy(), "空白文本不该让『复制』亮着")

    def test_retranslate_is_enabled_only_with_ocr_text(self):
        m = SP.PanelModel()
        self.assertFalse(m.can_retranslate())
        m.set_translation("手工文本", "译", SP.BACKEND_LOCAL)
        self.assertFalse(m.can_retranslate(), "没有 OCR 原文时不给重跑（重跑要有个确定的输入）")
        m.set_ocr("hello", SP.BACKEND_LOCAL)
        self.assertTrue(m.can_retranslate())


class TestTranslateInputContract(unittest.TestCase):
    """口径 ③：重跑翻译的输入**只能是 OCR 原文**。"""

    def test_uses_ocr_text_only(self):
        m = SP.PanelModel()
        m.set_ocr("原文", SP.BACKEND_CLOUD)
        m.set_translation("原文", "第一次的译文", SP.BACKEND_LOCAL)
        self.assertEqual(m.translate_input(), "原文",
                         "重跑翻译必须回到同一份原文：串到译文上会越翻越走样")

    def test_empty_when_no_ocr(self):
        m = SP.PanelModel()
        m.set_translation("手工文本", "译", SP.BACKEND_LOCAL)
        self.assertEqual(m.translate_input(), "")

    def test_running_state_is_visible_and_overwritten_by_the_result(self):
        m = SP.PanelModel()
        m.start(SP.KIND_OCR, SP.BACKEND_LOCAL)
        self.assertTrue(m.busy)
        self.assertEqual(m.backend, SP.BACKEND_LOCAL)
        m.set_ocr("ok", SP.BACKEND_LOCAL, ms=12)
        self.assertFalse(m.busy)
        self.assertEqual(m.ms, 12)


class TestErrorMessages(unittest.TestCase):
    def test_error_keeps_the_actionable_hint(self):
        m = SP.PanelModel()
        hint = "✗ 本机模型没起：先 `curl 127.0.0.1:12087/v1/models` 看看，或按 Ctrl+…切云"
        m.set_error(hint, note="local")
        self.assertEqual(m.message, hint)
        self.assertFalse(m.busy)

    def test_hints_exist_for_both_kinds(self):
        for kind in SP.KINDS:
            self.assertIn("F2" if kind == SP.KIND_TRANSLATE else "F4", SP.PANEL_HINTS[kind])

    def test_unknown_kind_is_refused_loudly(self):
        with self.assertRaises(AssertionError):
            SP.PanelModel().start("telepathy", SP.BACKEND_LOCAL)


@unittest.skipUnless(not GUI_SKIP, f"跳过界面用例：{GUI_SKIP}")
class TestPanelWindow(unittest.TestCase):
    """真建一次面板（xvfb 里跑）——按钮连没连上、文本刷没刷新，只有真建窗才看得出。"""

    def test_panel_builds_and_updates(self):
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        copied = []
        pv = SP.PanelView(on_copy=lambda m: copied.append(m.primary_text()),
                          on_retranslate=lambda m: copied.append("retranslate"),
                          on_switch=lambda m: copied.append("switch"),
                          on_toggle_view=lambda: copied.append("toggle"))
        widget = pv.build()
        m = SP.PanelModel()
        m.set_ocr("识别到的文字", SP.BACKEND_LOCAL, ms=30)
        pv.update(m)
        self.assertIn("本地", pv._headline.get_text())
        buf = pv._text.get_buffer()
        self.assertEqual(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False),
                         "识别到的文字")
        self.assertTrue(pv._copy.get_sensitive())
        pv._copy.clicked()
        self.assertEqual(copied, ["识别到的文字"])
        pv._switch.clicked()
        self.assertEqual(copied[-1], "switch")
        self.assertIsInstance(widget, Gtk.Box)

    def test_empty_panel_hides_the_translate_toggle(self):
        import gi
        gi.require_version("Gtk", "3.0")
        pv = SP.PanelView()
        pv.build()
        pv.update(SP.PanelModel())
        self.assertFalse(pv._toggle.get_visible(), "没翻译结果时不该出现『看原文/看译文』")


if __name__ == "__main__":
    unittest.main()