# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：选区遮罩（第 2 步）—— 事件口径、几何换算、遮罩**像素**，以及"绝不闪"这条硬要求。

为什么像素也要机器判：明江换掉 Snipaste 的理由就是"截图会闪一下"。闪是**表现**，
机器只能钉住"没有任何动画"（`TestNoAnimation` 扫源码）与"遮罩画出来的样子符合口径"
（`TestPaintPixels` 渲染到 ImageSurface 上逐像素断言：**选区内全透明、选区外压暗**、
边框是亮青色）。这两条一起，人才不用靠肉眼去分辨闪没闪。

GUI 用例（`TestRealWindow`）默认 skip：只有 `CAPSPLUS_GUI=1` 才跑，且 GDK 连到**真实 Wayland
桌面**时整类拒跑（`CAPSPLUS_WAYLAND=1` 才放行）—— 与 `tests/test_settings_window.py` 同一套口径，
理由也一样：这台机是明江干活的机器，不许判据把窗口弹到他屏幕上。
CI 里用虚拟显示真跑：`xvfb-run -a env -u WAYLAND_DISPLAY GDK_BACKEND=x11 CAPSPLUS_GUI=1
python3 -m unittest tests.test_shot_overlay`。
"""

import ast
import math
import os
import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_overlay as OV          # noqa: E402
from capsplus import shot_frame as SF            # noqa: E402

try:
    import cairo
    HAVE_CAIRO = True
except Exception:                                 # pragma: no cover - 无 pycairo 的机器
    cairo = None
    HAVE_CAIRO = False

RUN_GUI = os.environ.get("CAPSPLUS_GUI") == "1"
ALLOW_WAYLAND_DESKTOP = os.environ.get("CAPSPLUS_WAYLAND") == "1"

MONITORS = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (3840, 540, 960, 540)]


def desktop_risk() -> str:
    """GDK 连上的到底是不是**真实 Wayland 桌面**（照抄 test_settings_window 的口径：
    `import gi` 之后再设 `GDK_BACKEND=x11` 无效，只能跑之前查）。"""
    if not RUN_GUI:
        return "没设 CAPSPLUS_GUI=1"
    try:
        import gi
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk
    except Exception as exc:
        return f"import gi 失败：{exc}"
    disp = Gdk.Display.get_default()
    if disp is None:
        return "连不上显示服务器"
    backend = type(disp).__name__
    if "Wayland" in backend and not ALLOW_WAYLAND_DESKTOP:
        return (f"GDK 连的是真实 Wayland 桌面（{backend}）：按口径拒跑，"
                "要用虚拟显示请先 `env -u WAYLAND_DISPLAY GDK_BACKEND=x11`")
    return ""


GUI_SKIP = desktop_risk()


def stub_overlay(settle_ms=0, **kw):
    """判据用的遮罩对象：**不建真窗**，用假窗口列表；settle 直接同步执行。"""
    ov = OV.Overlay(settle_ms=settle_ms, monitors=MONITORS, **kw)
    ov.windows = [_FakeWin(), _FakeWin(), _FakeWin()]
    ov._settle_then_fire = ov._fire          # 判据不跑主循环，settle 那一下直接落地
    return ov


class _FakeWin:
    def __init__(self):
        self.hidden = False

    def hide(self):
        self.hidden = True


class TestEventMapping(unittest.TestCase):
    def test_esc_cancels_and_enter_confirms(self):
        self.assertEqual(OV.key_action("Escape"), OV.ACTION_CANCEL)
        self.assertEqual(OV.key_action("Return"), OV.ACTION_CONFIRM)
        self.assertEqual(OV.key_action("KP_Enter"), OV.ACTION_CONFIRM,
                         "小键盘回车也要算确认（外接键盘上很常见）")

    def test_other_keys_do_nothing(self):
        for name in ("a", "Control_L", "space", ""):
            self.assertIsNone(OV.key_action(name))

    def test_left_button_starts_and_right_button_confirms(self):
        self.assertEqual(OV.button_action(1, False), OV.ACTION_START)
        self.assertEqual(OV.button_action(3, True), OV.ACTION_CONFIRM)

    def test_right_click_without_selection_cancels(self):
        self.assertEqual(OV.button_action(3, False), OV.ACTION_CANCEL,
                         "点了没反应会让人以为卡住了：右键在没选区时就当取消")

    def test_middle_button_is_ignored(self):
        self.assertEqual(OV.button_action(2, True), OV.ACTION_IGNORE)


class TestGeometry(unittest.TestCase):
    def test_monitor_at(self):
        self.assertEqual(OV.monitor_at(MONITORS, 10, 10), 0)
        self.assertEqual(OV.monitor_at(MONITORS, 2000, 500), 1)
        self.assertEqual(OV.monitor_at(MONITORS, 4000, 600), 2)
        self.assertIsNone(OV.monitor_at(MONITORS, 4000, 100), "第三块屏 y 从 540 起，100 不在它上面")

    def test_local_global_roundtrip(self):
        mon = MONITORS[2]
        g = OV.to_global(mon, (10, 20, 30, 40))
        self.assertEqual(g, (3850, 560, 30, 40))
        self.assertEqual(OV.to_local(mon, g), (10, 20, 30, 40))

    def test_dim_bands_cover_everything_except_the_selection(self):
        area = (0, 0, 100, 100)
        rect = (20, 30, 40, 50)
        bands = OV.dim_bands(rect, area)
        covered = sum(b[2] * b[3] for b in bands)
        self.assertEqual(covered, 100 * 100 - 40 * 50,
                         "四条带的面积必须正好等于『整屏减去选区』，不多不少（多一块=选区被压暗了）")
        for bx, by, bw, bh in bands:
            self.assertFalse(bx < 60 and bx + bw > 20 and by < 80 and by + bh > 30
                             and (bw * bh) > 0 and bx >= 20 and by >= 30 and bx + bw <= 60
                             and by + bh <= 80, "压暗带不许伸进选区里")

    def test_selection_touching_the_edge_omits_that_band(self):
        self.assertEqual(len(OV.dim_bands((0, 0, 100, 100), (0, 0, 100, 100))), 0,
                         "选区铺满整屏时没有一条带")

    def test_readout_moves_inside_near_the_top_edge(self):
        area = (0, 0, 800, 600)
        x, y = OV.readout_position((10, 0, 100, 100), area)
        self.assertGreaterEqual(y, 0)
        x2, y2 = OV.readout_position((10, 300, 100, 100), area)
        self.assertLess(y2, 300, "正常情况下读数在选区上方外侧")

    def test_readout_text_is_width_by_height(self):
        self.assertEqual(OV.readout_text((5, 5, 1280, 720)), "1280 × 720")


class TestOverlayStateMachine(unittest.TestCase):
    """交互状态机（不建真窗、不跑主循环）。反例：删掉 `confirm` 里的 `accept_rect` 就红。"""

    def test_drag_then_enter_gives_global_rect(self):
        got = []
        ov = stub_overlay(on_confirm=got.append)
        self.assertEqual(ov.on_press((100, 100), 1), OV.ACTION_START)
        self.assertEqual(ov.on_motion(1, (300, 250)), OV.ACTION_UPDATE)
        self.assertEqual(ov.on_release(), OV.ACTION_IGNORE, "松手不确认（第 2 步的口径：右键/Enter 才确认）")
        self.assertEqual(ov.on_key("Return"), OV.ACTION_CONFIRM)
        self.assertEqual(got, [(2020, 100, 200, 150)],
                         "选区必须换算成**全局逻辑坐标**（抓帧用的是这套坐标）")
        self.assertTrue(all(w.hidden for w in ov.windows), "确认后遮罩窗必须全部藏起来，"
                                                          "否则抓帧会把自己的遮罩拍进去")

    def test_reverse_drag_is_normalized(self):
        got = []
        ov = stub_overlay(on_confirm=got.append)
        ov.on_press((300, 300), 1)
        ov.on_motion(0, (100, 100))
        ov.on_key("Return")
        self.assertEqual(got, [(100, 100, 200, 200)])

    def test_tiny_selection_is_not_confirmed(self):
        got = []
        ov = stub_overlay(on_confirm=got.append)
        ov.on_press((100, 100), 1)
        ov.on_motion(0, (101, 101))
        self.assertEqual(ov.on_key("Return"), OV.ACTION_IGNORE)
        self.assertEqual(got, [], "1×1 的选区不许出图（那只是一次手抖）")
        self.assertFalse(any(w.hidden for w in ov.windows))

    def test_right_click_confirms_after_release(self):
        got = []
        ov = stub_overlay(on_confirm=got.append)
        ov.on_press((10, 10), 1)
        ov.on_motion(0, (110, 60))
        ov.on_release()
        self.assertEqual(ov.on_press((0, 0), 3), OV.ACTION_CONFIRM)
        self.assertEqual(got, [(10, 10, 100, 50)])

    def test_esc_gives_none_and_hides_windows(self):
        got = []
        ov = stub_overlay(on_confirm=got.append)
        ov.on_press((10, 10), 1)
        ov.on_motion(0, (110, 60))
        self.assertEqual(ov.on_key("Escape"), OV.ACTION_CANCEL)
        self.assertEqual(got, [None], "取消也要回调一次（否则主循环没人退出）")
        self.assertTrue(all(w.hidden for w in ov.windows))

    def test_second_drag_replaces_the_first_selection(self):
        ov = stub_overlay()
        ov.on_press((10, 10), 1)
        ov.on_motion(0, (110, 60))
        ov.on_release()
        ov.on_press((200, 200), 1)
        self.assertIsNone(ov.rect, "重新按下即重划：旧选区先清掉")
        ov.on_motion(0, (260, 240))
        self.assertEqual(ov.rect, (200, 200, 60, 40))


class TestNoAnimation(unittest.TestCase):
    """**"绝不闪"这条硬要求**的机器判据：遮罩文件里不许有任何动画机制。"""

    SRC = (REPO / "capsplus" / "shot_overlay.py").read_text(encoding="utf-8")

    @staticmethod
    def code_only(text: str) -> str:
        """去掉注释与 docstring：注释里解释"我们不用 ease/transition"不算实现。"""
        text = re.sub(r'"""[\s\S]*?"""', "", text)
        text = re.sub(r"/\*[\s\S]*?\*/", "", text)
        return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())

    def test_source_has_no_animation_primitives(self):
        code = self.code_only(self.SRC)
        # 用 `\b` 卡词首：`on_release(` 里也有 "ease("，别把 release 当成 ease（第一次就误报过）
        for banned in ("ease(", "set_opacity", "transition", "Clutter", "animate",
                       "fade", "FadeIn", "Gtk.Window.set_opacity"):
            self.assertIsNone(re.search(r"\b" + re.escape(banned), code),
                              f"{banned} 是动画/过渡机制：明江换掉 Snipaste 就是因为截图会闪一下，"
                              "遮罩必须一 map 就是最终样子")

    def test_no_per_frame_timer(self):
        """允许的定时器只有一个：藏窗后的 settle（等合成器提交一帧）。"""
        self.assertEqual(self.code_only(self.SRC).count("timeout_add("), 1,
                         "多出来的定时器多半是动画/轮询：抓帧前那一个 settle 才是要的")

    def test_only_cairo_draws_the_mask(self):
        src = ast.parse(self.SRC)
        imported = set()
        for node in src.body:                     # 只看**模块级**：函数里的懒加载正是我们要的
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "time"},
                         f"顶层不许 import gi / cairo（CI 不装它们），实际：{sorted(imported)}")
        self.assertIn("_gtk()", self.SRC)
        self.assertIn("_cairo()", self.SRC)


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：像素判据跳过")
class TestPaintPixels(unittest.TestCase):
    """把遮罩渲染到 ImageSurface 上，逐像素核对口径 —— 不需要显示服务器。"""

    W, H = 200, 160

    def _render(self, rect, hint=False):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.W, self.H)
        cr = cairo.Context(surface)
        OV.paint(cr, (0, 0, self.W, self.H), rect, hint=hint)
        return surface

    @staticmethod
    def _px(surface, x, y):
        surface.flush()
        stride = surface.get_stride()
        buf = bytes(surface.get_data())
        off = y * stride + x * 4
        b, g, r, a = buf[off], buf[off + 1], buf[off + 2], buf[off + 3]     # ARGB32 是小端 BGRA
        return (r / 255, g / 255, b / 255, a / 255)

    def test_everything_is_dimmed_when_there_is_no_selection(self):
        s = self._render(None)
        for x, y in ((1, 1), (100, 80), (198, 158)):
            self.assertAlmostEqual(self._px(s, x, y)[3], OV.DIM_ALPHA, places=2)

    def test_outside_is_dimmed_and_inside_is_untouched(self):
        s = self._render((40, 40, 60, 50))
        for x, y in ((5, 5), (5, 100), (150, 40), (150, 140)):
            self.assertAlmostEqual(self._px(s, x, y)[3], OV.DIM_ALPHA, places=2,
                                   msg=f"({x},{y}) 在选区外，必须被压暗")
        for x, y in ((50, 50), (90, 80), (95, 85), (45, 45)):   # 都躲开 1px 边框
            self.assertEqual(self._px(s, x, y)[3], 0.0,
                             f"({x},{y}) 在选区内，必须全透明（否则用户在遮罩里看到的不是屏幕原样）")

    def test_selection_border_is_the_accent_colour(self):
        s = self._render((40, 40, 60, 50))
        r, g, b, a = self._px(s, 70, 40)
        self.assertGreater(a, 0.9, "边框必须是不透明的")
        self.assertGreater(b, 0.5)
        self.assertLess(r, 0.5)

    def test_readout_text_is_drawn(self):
        s = self._render((40, 80, 60, 50))
        box_x, box_y = OV.readout_position((40, 80, 60, 50), (0, 0, self.W, self.H))
        found = any(self._px(s, x, y)[3] > 0.5 and self._px(s, x, y)[0] > 0.8
                    for x in range(box_x, min(box_x + 80, self.W))
                    for y in range(box_y, min(box_y + OV.FONT_SIZE + 2 * OV.READOUT_PAD, self.H)))
        self.assertTrue(found, "选区定了以后必须能看见「宽 × 高」读数")

    def test_paint_is_idempotent_and_deterministic(self):
        """同一状态画两遍像素完全一样 —— 顺带钉住"没有逐帧动画"这件事。"""
        a = bytes(self._render((40, 40, 60, 50)).get_data())
        b = bytes(self._render((40, 40, 60, 50)).get_data())
        self.assertEqual(a, b)


@unittest.skipUnless(not GUI_SKIP, f"跳过界面用例：{GUI_SKIP}")
class TestRealWindow(unittest.TestCase):
    """真建窗（只在 CAPSPLUS_GUI=1 且不是真实 Wayland 桌面时跑）。"""

    def test_windows_are_built_per_monitor_without_animating(self):
        ov = OV.Overlay(settle_ms=0, monitors=MONITORS[:1])
        loops = []
        ok = {"v": False}

        def build_and_stop():
            try:
                import gi
                gi.require_version("Gdk", "3.0")
                from gi.repository import Gdk, GLib
                display = Gdk.Display.get_default()
                loop = GLib.MainLoop()
                ov.build(display, loop.quit)
                ok["v"] = len(ov.windows) == 1 and ov.windows[0].get_visible()
                GLib.timeout_add(200, lambda: (loop.quit(), False)[1])
                loop.run()
            except Exception as exc:      # pragma: no cover
                loops.append(repr(exc))
            return False

        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        loop = GLib.MainLoop()
        GLib.timeout_add(0, lambda: (build_and_stop(), False)[1])
        GLib.timeout_add(1500, lambda: (loop.quit(), False)[1])
        loop.run()
        self.assertEqual(loops, [], f"建窗抛异常：{loops}")
        self.assertTrue(ok["v"], "遮罩窗没建出来（或不可见）")
        ov.hide_all()

    def _sender(self, gw):
        """造事件并**走 GDK 真派发**（`Gtk.main_do_event`）。

        为什么不用 `Gdk.test_simulate_button`：本机 xvfb 实测（2026-10-19）它一个事件都送不到
        （内部拿不到 pointer device，只留一句 `gdk_device_grab_info: assertion 'GDK_IS_DEVICE …'`）；
        `Gtk.main_do_event` 这条路是真通的（事件会带着 `send_event=True` 走 GTK 的正常派发）。
        """
        import gi
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk

        def send(kind, **kw):
            ev = Gdk.Event.new(kind)
            ev.window = gw
            ev.send_event = True
            for key, value in kw.items():
                setattr(ev, key, value)
            Gtk.main_do_event(ev)
        return send

    def test_real_events_drive_a_full_selection(self):
        """真事件走一遍：按下 → 拖动 → 松手（不确认）→ 回车确认 → 拿到全局坐标、窗已藏。

        这条是**唯一**能证明"窗口上的事件真的接到了状态机上、并且 settle 真的等过"的判据
        （纯逻辑那组测的是状态机本身）。它一上线就抓出过一个真 bug：确认时把主循环立刻退了，
        settle 的 timeout 从来没跑过 —— 那样抓帧有可能把遮罩自己拍进去。
        """
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gdk, GLib
        got = []
        monitors, display = OV.monitor_rects()
        self.assertTrue(monitors, "xvfb 下应当有至少一块显示器")
        ov = OV.Overlay(settle_ms=60, on_confirm=got.append, monitors=monitors)
        state = {}

        def drive():
            send = self._sender(ov.windows[0].get_window())
            send(Gdk.EventType.BUTTON_PRESS, x=10.0, y=10.0, x_root=10.0, y_root=10.0, button=1)
            send(Gdk.EventType.MOTION_NOTIFY, x=110.0, y=60.0, x_root=110.0, y_root=60.0)
            send(Gdk.EventType.BUTTON_RELEASE, x=110.0, y=60.0, x_root=110.0, y_root=60.0, button=1)
            state["after_release"] = ov.rect
            state["callback_before_settle"] = list(got)
            send(Gdk.EventType.KEY_PRESS, keyval=Gdk.KEY_Return, state=0)
            state["callback_right_after_key"] = list(got)
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(200, drive)
        GLib.timeout_add(4000, lambda: (loop.quit(), False)[1])
        ov.build(display, loop.quit)
        loop.run()
        self.assertEqual(state.get("after_release"), (10, 10, 100, 50),
                         "拖动应当在**全局坐标**下给出 100×50 的选区（松手不确认）")
        self.assertEqual(state.get("callback_right_after_key"), [],
                         "确认那一下**不能**立刻回调：要先藏窗、等 settle 一帧（否则抓帧会拍到遮罩）")
        self.assertEqual(got, [(10, 10, 100, 50)], "settle 过了才把选区交给调用方")
        self.assertFalse(any(w.get_visible() for w in ov.windows), "遮罩必须已经藏起来")

    def test_escape_path_returns_none_and_quits(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gdk, GLib
        got = []
        monitors, display = OV.monitor_rects()
        ov = OV.Overlay(settle_ms=1000, on_confirm=got.append, monitors=monitors)

        def drive():
            send = self._sender(ov.windows[0].get_window())
            send(Gdk.EventType.KEY_PRESS, keyval=Gdk.KEY_Escape, state=0)
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(200, drive)
        GLib.timeout_add(4000, lambda: (loop.quit(), False)[1])
        ov.build(display, loop.quit)
        loop.run()
        self.assertEqual(got, [None], "Esc 要回调 None，且**不等** settle（取消没什么可等的）")

    def test_fullscreen_is_requested_on_the_right_monitor(self):
        self.assertIn("fullscreen_on_monitor", OV.SRC_SENTINEL if hasattr(OV, "SRC_SENTINEL") else
                      (REPO / "capsplus" / "shot_overlay.py").read_text(encoding="utf-8"),
                      "Wayland 下只能用 fullscreen_on_monitor 指定屏幕（move() 会被忽略）")


if __name__ == "__main__":
    unittest.main()