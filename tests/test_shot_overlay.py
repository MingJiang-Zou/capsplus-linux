# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：选区遮罩（第二批 P0 重写版）—— **背景必须是冻结图**、选区跨屏、整屏捷径、贴边吸附。

这一版为什么重写（2026-10-19 明江真机「框选的时候全黑屏是盲选」）：
旧版是「全屏半透明窗 + 压暗 + 指望合成器把桌面透出来」，Wayland 下拿不到 ARGB 就是**全黑**，
而 xvfb（X11 有 ARGB 视觉）**永远复现不了** —— 当时那批像素判据全绿却拦不住真机 bug。

所以判据分两层，**关键的那层不看 alpha**：
  * `TestFrozenBackgroundIsTheContent`：拿一张**有图案的假冻结图**当背景，断言「选区里能看到图案」
    —— 拦的是「背景不是冻结图」（透明、纯色、纯黑都会红）；
  * `TestNoTransparencyReliance`：再补一条「整块画布每个像素都不透明」，把病根直接焊死；
  * `TestNoAnimation`：不闪（明江换掉 Snipaste 就是因为闪）—— 扫源码里不许出现动画 API。

GUI 用例默认 skip：`CAPSPLUS_GUI=1` 才跑，且 GDK 连到**真实 Wayland 桌面**时整类拒跑
（`CAPSPLUS_WAYLAND=1` 才放行）—— 这台机是明江干活的机器，不许判据把窗口弹到他屏幕上。
CI 里用虚拟显示真跑：`xvfb-run -a env -u WAYLAND_DISPLAY GDK_BACKEND=x11 CAPSPLUS_GUI=1
python3 -m unittest tests.test_shot_overlay`。
"""

import inspect
import os
import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_frames as SFR        # noqa: E402
from capsplus import shot_overlay as OV        # noqa: E402

try:
    import cairo
    HAVE_CAIRO = True
except Exception:
    cairo = None
    HAVE_CAIRO = False

RUN_GUI = os.environ.get("CAPSPLUS_GUI") == "1"
ALLOW_WAYLAND_DESKTOP = os.environ.get("CAPSPLUS_WAYLAND") == "1"

BLUE = (0.2, 0.4, 0.9)
GREEN = (0.1, 0.85, 0.3)


def guard() -> str:
    """没设 CAPSPLUS_GUI=1、或 GDK 连的是明江的真实 Wayland 桌面，就别跑界面用例。"""
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
    if "Wayland" in type(disp).__name__ and not ALLOW_WAYLAND_DESKTOP:
        return "GDK 连的是真实 Wayland 桌面：按口径拒跑"
    return ""


GUI_SKIP = guard()


def pattern_surface(w: int, h: int, left=BLUE, right=GREEN):
    """左半一种颜色、右半另一种：跨屏/裁切都能一眼看出「哪一块来自哪张冻结图」。"""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, w), max(1, h))
    cr = cairo.Context(surface)
    cr.set_source_rgb(*left)
    cr.rectangle(0, 0, w / 2, h)
    cr.fill()
    cr.set_source_rgb(*right)
    cr.rectangle(w / 2, 0, w / 2, h)
    cr.fill()
    return surface


def px(surface, x: int, y: int):
    surface.flush()
    data = bytes(surface.get_data())
    off = y * surface.get_stride() + x * 4
    return (data[off + 2], data[off + 1], data[off], data[off + 3])   # r, g, b, a


def render(area, rect, frame=None, **kw):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, area[2], area[3])
    cr = cairo.Context(surface)
    OV.paint(cr, area, rect, frame=frame, **kw)
    return surface


class TestKeyAndButtonMap(unittest.TestCase):
    def test_escape_cancels_and_enter_confirms(self):
        self.assertEqual(OV.key_action("Escape"), OV.ACTION_CANCEL)
        self.assertEqual(OV.key_action("Return"), OV.ACTION_CONFIRM)
        self.assertEqual(OV.key_action("KP_Enter"), OV.ACTION_CONFIRM)

    def test_ctrl_a_is_full_screen(self):
        self.assertEqual(OV.key_action("a", ctrl=True), OV.ACTION_FULL)
        self.assertEqual(OV.key_action("A", ctrl=True), OV.ACTION_FULL)

    def test_letter_keys_are_not_stolen_without_ctrl(self):
        """遮罩窗短暂持有键盘焦点：**不许**抢明江的普通按键（'a' 不该被当成全选）。"""
        for name in ("a", "c", "s", "z", "space", "F5", ""):
            self.assertIsNone(OV.key_action(name), f"{name} 不该被遮罩吃掉")

    def test_buttons(self):
        self.assertEqual(OV.button_action(1, False), OV.ACTION_START)
        self.assertEqual(OV.button_action(3, True), OV.ACTION_CONFIRM)
        self.assertEqual(OV.button_action(3, False), OV.ACTION_CANCEL,
                         "右键时没选区 = 取消（点一下什么都不发生会让人以为卡了）")
        self.assertEqual(OV.button_action(2, True), OV.ACTION_IGNORE)


class TestGeometryHelpers(unittest.TestCase):
    def setUp(self):
        self.monitors = [(0, 0, 800, 600), (800, 0, 1200, 800)]

    def test_monitor_at(self):
        self.assertEqual(OV.monitor_at(self.monitors, 10, 10), 0)
        self.assertEqual(OV.monitor_at(self.monitors, 900, 10), 1)
        self.assertIsNone(OV.monitor_at(self.monitors, 5000, 5000))

    def test_local_global_round_trip(self):
        for mon in self.monitors:
            rect = (mon[0] + 5, mon[1] + 7, 20, 30)
            local = OV.to_local(mon, rect)
            self.assertEqual(OV.to_global(mon, local), rect)

    @unittest.skipUnless(HAVE_CAIRO, "这条要造 cairo 假冻结图")
    def test_union_and_frame_at(self):
        self.assertEqual(SFR.union_rect(self.monitors), (0, 0, 2000, 800))
        frames = [SFR.Frame((0, 0, 800, 600), pattern_surface(800, 600)),
                  SFR.Frame((800, 0, 1200, 800), pattern_surface(1200, 800))]
        self.assertIs(frames[1], SFR.frame_at(frames, 900, 100))
        self.assertIsNone(SFR.frame_at(frames, 5000, 100))

    def test_readout_shows_size_and_coordinates(self):
        self.assertEqual(OV.readout_text((10, 20, 640, 480)), "640 × 480")
        self.assertEqual(OV.coord_text((10, 20, 640, 480)), "10, 20")

    def test_readout_flips_inside_when_at_the_top_edge(self):
        area = (0, 0, 800, 600)
        self.assertLess(OV.readout_position((10, 0, 100, 100), area)[1], 100)

    def test_hint_bar_sits_at_the_bottom(self):
        _x, y = OV.hint_position((0, 0, 800, 600))
        self.assertGreater(y, 600 - OV.FONT_SIZE - 40, "提示条要贴底，别盖住内容")

    def test_dim_bands_never_cover_the_selection(self):
        area = (0, 0, 800, 600)
        rect = (100, 100, 400, 300)
        for bx, by, bw, bh in OV.dim_bands(rect, area):
            self.assertFalse(bx < rect[0] + rect[2] and bx + bw > rect[0] and
                             by < rect[1] + rect[3] and by + bh > rect[1],
                             f"压暗带 {(bx, by, bw, bh)} 压到选区里了")

    def test_no_bands_when_the_selection_covers_everything(self):
        self.assertEqual(OV.dim_bands((0, 0, 800, 600), (0, 0, 800, 600)), [])

    def test_eight_handles_and_hit_test(self):
        rect = (100, 100, 400, 300)
        self.assertEqual(len(OV.handle_points(rect)), 8)
        self.assertTrue(OV.handle_at(rect, (100, 100)))
        self.assertTrue(OV.handle_at(rect, (300, 100)))       # 上边中点
        self.assertFalse(OV.handle_at(rect, (250, 250)))
        self.assertFalse(OV.handle_at(None, (100, 100)))


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：像素判据跳过")
class TestFrozenBackgroundIsTheContent(unittest.TestCase):
    """**关键判据**：选区里的内容必须来自冻结图。这条不看 alpha，只看「图案在不在」。"""

    def setUp(self):
        self.area = (0, 0, 800, 600)
        self.frame = pattern_surface(800, 600)

    def test_pattern_is_visible_inside_the_selection(self):
        rect = (100, 100, 300, 200)
        out = render(self.area, rect, frame=self.frame)
        inside_left = px(out, 150, 200)
        inside_right = px(out, 350, 200)
        self.assertGreater(inside_left[2], 0.7, f"选区左半该是冻结图的蓝色，实际 {inside_left}")
        self.assertGreater(inside_right[1], 0.7, f"选区右半该是冻结图的绿色，实际 {inside_right}")

    def test_selection_is_brighter_than_the_dimmed_outside(self):
        rect = (100, 100, 300, 200)
        out = render(self.area, rect, frame=self.frame)
        inside = px(out, 150, 200)
        outside = px(out, 700, 500)
        self.assertGreater(sum(inside[:3]), sum(outside[:3]),
                           "选区外要压暗（压暗是压在冻结图上，不是压在透明上）")

    def test_outside_still_shows_the_frame_content(self):
        """压暗之后**内容还得看得见** —— 这正是「盲选」的反面。"""
        out = render(self.area, (300, 200, 100, 100), frame=self.frame)
        left_outside = px(out, 100, 500)
        right_outside = px(out, 700, 500)
        self.assertGreater(left_outside[2], left_outside[1],
                           f"选区外左半边仍该能看出冻结图的蓝色，实际 {left_outside}")
        self.assertGreater(right_outside[1], right_outside[2],
                           f"选区外右半边仍该能看出冻结图的绿色，实际 {right_outside}")

    def test_no_rect_still_draws_the_frozen_desktop(self):
        """还没开始拖的时候，看到的也得是屏幕内容（旧版这时候是**全黑**）。"""
        out = render(self.area, None, frame=self.frame)
        self.assertGreater(px(out, 100, 300)[2], 0.4)
        self.assertGreater(px(out, 700, 300)[1], 0.4)

    def test_missing_frame_falls_back_to_an_opaque_background(self):
        """没有冻结图时（理论上不该发生）也不许画透明 —— 那正是这次 P0 的病根。"""
        out = render(self.area, None, frame=None)
        self.assertEqual(px(out, 10, 10)[3], 255, "兜底底色也必须不透明")


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：像素判据跳过")
class TestNoTransparencyReliance(unittest.TestCase):
    """把病根焊死：整块画布**每个像素**都必须不透明（拿不到 ARGB 也不会变黑）。"""

    def test_every_pixel_is_opaque(self):
        area = (0, 0, 200, 120)
        frame = pattern_surface(200, 120)
        for rect in (None, (10, 10, 50, 40), (0, 0, 200, 120)):
            with self.subTest(rect=rect):
                out = render(area, rect, frame=frame)
                out.flush()
                data = bytes(out.get_data())
                alphas = {data[i] for i in range(3, len(data), 4)}
                self.assertEqual(alphas, {255},
                                 f"画布上出现了非不透明像素（rect={rect}）：alpha 取值 {alphas}")

    def test_source_code_does_not_clear_to_transparent(self):
        """源码层再补一刀：`OPERATOR_SOURCE` + 全透明是旧病根，不许再出现。"""
        body = _overlay_body()
        self.assertNotIn("OPERATOR_SOURCE", body,
                         "不许出现 OPERATOR_SOURCE：清成透明就是「盲选」的来路")
        self.assertNotIn("set_app_paintable", body)

    def test_no_rgba_visual_dependency(self):
        self.assertNotIn("get_rgba_visual", _overlay_body(),
                         "背景是冻结图 ⇒ 不需要 RGBA visual；依赖它就又回到「拿不到就全黑」")


def _overlay_body() -> str:
    """遮罩源码去掉注释后的正文（反面教材写在注释里，扫全文会误报）。"""
    text = (REPO / "capsplus" / "shot_overlay.py").read_text(encoding="utf-8")
    return "\n".join(l.split("#", 1)[0] for l in text.splitlines()
                     if not l.strip().startswith("#"))


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：像素判据跳过")
class TestCrossScreenRendering(unittest.TestCase):
    """跨屏：每块屏只画自己那一块（按屏裁剪），拼起来才是完整选区。"""

    def setUp(self):
        self.monitors = [(0, 0, 800, 600), (800, 0, 800, 600)]
        self.frames = [SFR.Frame((0, 0, 800, 600), pattern_surface(800, 600)),
                       SFR.Frame((800, 0, 800, 600), pattern_surface(800, 600))]

    def test_each_monitor_draws_its_own_slice_of_a_spanning_selection(self):
        rect = (700, 100, 200, 100)          # 横跨两块屏
        left = render((0, 0, 800, 600), OV.to_local(self.monitors[0], rect),
                      frame=self.frames[0].surface)
        right = render((0, 0, 800, 600), OV.to_local(self.monitors[1], rect),
                       frame=self.frames[1].surface)
        self.assertGreater(px(left, 750, 150)[2], 0.7, "第一块屏上这 100px 该是冻结图内容")
        self.assertGreater(px(right, 50, 150)[1], 0.7, "第二块屏上这 100px 该是冻结图内容")
        self.assertLess(sum(px(left, 300, 150)[:3]), sum(px(left, 750, 150)[:3]),
                        "超出本屏的那一半在这块屏上是压暗的（不是内容）")

    def test_window_draws_the_local_slice_not_the_global_rect(self):
        """每块屏画的是**本屏本地坐标**下的那一块（跨屏选区靠这个各自裁剪）。

        这条直接钉 `_local_rect()`：之前只测 `to_local()` 的话，把 `_local_rect` 改成返回
        全局矩形也照样全绿（2026-10-19 反例具型 W 实测漏过）。
        """
        ov = OV.Overlay(monitors=self.monitors)
        ov.rect = (700, 100, 200, 100)
        self.assertEqual(ov._local_rect(0), (700, 100, 200, 100))
        self.assertEqual(ov._local_rect(1), (-100, 100, 200, 100),
                         "第二块屏的本地坐标要减掉它自己的偏移（800）")

    def test_local_rect_may_be_negative_and_cairo_clips(self):
        rect = (-50, 10, 200, 100)
        local = OV.to_local(self.monitors[0], rect)
        self.assertLess(local[0], 0)
        out = render((0, 0, 800, 600), local, frame=self.frames[0].surface)
        self.assertGreater(px(out, 20, 50)[2], 0.7)

    def test_crop_from_frames_reassembles_both_halves(self):
        out = SFR.crop_from_frames((700, 100, 200, 100), self.frames)
        self.assertEqual((out.get_width(), out.get_height()), (200, 100))
        self.assertGreater(px(out, 10, 50)[2], 0.7, "左半来自第一块屏")
        self.assertGreater(px(out, 190, 50)[1], 0.7, "右半来自第二块屏")

    def test_crop_refuses_a_rect_outside_every_screen(self):
        with self.assertRaises(ValueError):
            SFR.crop_from_frames((5000, 5000, 100, 100), self.frames)


class TestOverlayStateMachine(unittest.TestCase):
    """纯状态机（不建窗）：跨屏拖拽、整屏捷径、贴边吸附、确认/取消的收尾。"""

    def setUp(self):
        self.monitors = [(0, 0, 800, 600), (800, 0, 800, 600)]
        self.fired = []
        self.ov = OV.Overlay(frames=[], monitors=self.monitors, on_confirm=self.fired.append)

    def test_drag_start_clears_the_old_selection(self):
        self.ov.rect = (1, 2, 3, 4)
        self.assertEqual(self.ov.on_press(0, (10, 10)), OV.ACTION_START)
        self.assertIsNone(self.ov.rect)
        self.assertTrue(self.ov.dragging)

    def test_anchor_is_global_so_a_cross_screen_drag_works(self):
        """起点在**第二块屏**（有 800 偏移）、拖回第一块屏：选区必须横跨两屏。

        起点特意选有偏移的那块屏 —— 从 (0,0) 那块屏起手时本地坐标恰好等于全局坐标，
        锚点用错坐标系也测不出来（2026-10-19 反例具型 X 实测漏过）。
        """
        self.ov.on_press(1, (100, 100))          # 全局 (900, 100)
        self.assertEqual(self.ov.on_motion(0, (700, 200)), OV.ACTION_UPDATE)   # 全局 (700, 200)
        self.ov.on_release()
        self.assertEqual(self.ov.rect, (700, 100, 200, 100),
                         f"跨屏拖拽算错了（锚点必须是全局坐标）：{self.ov.rect}")

    def test_anchor_offset_is_applied_going_the_other_way_too(self):
        self.ov.on_press(0, (700, 100))          # 全局 (700, 100)
        self.ov.on_motion(1, (100, 200))         # 全局 (900, 200)
        self.ov.on_release()
        self.assertEqual(self.ov.rect, (700, 100, 200, 100))

    def test_snap_on_release(self):
        self.ov.on_press(0, (3, 4))
        self.ov.on_motion(0, (797, 596))
        self.assertEqual(self.ov.on_release(), OV.ACTION_SNAP)
        self.assertEqual(self.ov.rect, (0, 0, 800, 600), "贴边要吸附到屏幕边界（全屏要能一拖就到）")

    def test_snap_can_be_switched_off(self):
        ov = OV.Overlay(monitors=self.monitors, snap=False)
        ov.on_press(0, (3, 4))
        ov.on_motion(0, (797, 596))
        self.assertEqual(ov.on_release(), OV.ACTION_IGNORE)
        self.assertEqual(ov.rect, (3, 4, 794, 592))

    def test_ctrl_a_selects_the_whole_union(self):
        self.assertEqual(self.ov.on_key("a", ctrl=True), OV.ACTION_FULL)
        self.assertEqual(self.ov.rect, (0, 0, 1600, 600))

    def test_double_click_selects_that_single_monitor(self):
        self.assertEqual(self.ov.on_press(1, (100, 100), button=1, double=True), OV.ACTION_FULL)
        self.assertEqual(self.ov.rect, (800, 0, 800, 600), "双击 = 指针那块屏（多屏时别被迫全选）")

    def test_confirm_requires_a_usable_selection(self):
        self.assertEqual(self.ov.on_key("Return"), OV.ACTION_IGNORE, "没有选区时回车无效")
        self.ov.rect = (10, 10, 1, 1)
        self.assertEqual(self.ov.on_key("Return"), OV.ACTION_IGNORE, "1×1 的垃圾选区不认")
        self.ov.rect = (10, 10, 100, 100)
        self.assertEqual(self.ov.on_key("Return"), OV.ACTION_CONFIRM)
        self.assertEqual(self.ov.result, (10, 10, 100, 100))
        self.assertEqual(self.fired, [(10, 10, 100, 100)])

    def test_cancel_reports_none(self):
        self.ov.rect = (10, 10, 100, 100)
        self.assertEqual(self.ov.on_key("Escape"), OV.ACTION_CANCEL)
        self.assertIsNone(self.ov.result)
        self.assertEqual(self.fired, [None])

    def test_confirm_quits_the_loop_without_a_settle_dance(self):
        """确认后**立刻**交坐标：现在从冻结图裁剪，屏幕一眼都不看（旧版的 settle 已删）。"""
        quit_calls = []
        self.ov._quit = lambda: quit_calls.append(True)
        self.ov.rect = (10, 10, 100, 100)
        self.ov.on_key("Return")
        self.assertEqual(quit_calls, [True])
        self.assertFalse(hasattr(OV.Overlay, "_settle_then_fire"),
                         "settle 那套逻辑应当已经删掉（它是为「躲自己的遮罩」存在的）")


class TestNoAnimation(unittest.TestCase):
    """明江换掉 Snipaste 的原因就是「截图会闪一下」：源码里不许出现动画。"""

    def test_no_animation_api_in_source(self):
        body = _overlay_body()
        for bad in ("ease", "transition", "set_opacity", "animate", "add_transition"):
            self.assertIsNone(re.search(rf"\b{re.escape(bad)}", body),
                              f"遮罩里出现了动画相关调用：{bad}（明江要的是「绝不闪」）")

    def test_windows_are_shown_not_faded(self):
        self.assertIn("win.show_all()", _overlay_body())


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要造帧与棋盘格")
class TestFrameIsMappedToTheArea(unittest.TestCase):
    """帧的像素尺寸**不一定**等于绘制区域 —— 必须显式映射。

    实测（2026-10-19 同一台机同一天）：三屏联合（逻辑 4800×1080）回来的图是 6400×1440（1.3333×），
    单屏 (0,0,1920,1080) 回来的是 1920×1080（1×）；窗口 scale_factor 还可能是 2。
    旧写法 `set_source_surface(frame, 0, 0); paint()` 假设 1:1 ⇒ 大帧只画出一角 / 拉错位。
    """

    @staticmethod
    def checkerboard(w, h, cells=2):
        """已知图案：左半蓝、右半绿，并且上下两条不同深浅（判断纵向映射）。"""
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surface)
        cr.set_source_rgb(*BLUE)
        cr.rectangle(0, 0, w / 2, h / 2)
        cr.fill()
        cr.set_source_rgb(0.05, 0.15, 0.75)
        cr.rectangle(0, h / 2, w / 2, h / 2)
        cr.fill()
        cr.set_source_rgb(*GREEN)
        cr.rectangle(w / 2, 0, w / 2, h / 2)
        cr.fill()
        cr.set_source_rgb(0.05, 0.75, 0.2)
        cr.rectangle(w / 2, h / 2, w / 2, h / 2)
        cr.fill()
        return surface

    def _render(self, area, frame, rect=None):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, area[2], area[3])
        cr = cairo.Context(surface)
        OV.paint(cr, area, rect, frame=frame, hint=False)
        return surface

    def test_oversized_frame_is_scaled_down_to_fill_the_area(self):
        """1.3333× 的大帧：四个象限的内容都要落在正确位置（不是只看"非黑"）。"""
        area = (0, 0, 600, 300)
        frame = self.checkerboard(800, 400)          # 1.3333×
        out = self._render(area, frame)
        left_up = px(out, 150, 75)
        left_down = px(out, 150, 225)
        right_up = px(out, 450, 75)
        right_down = px(out, 450, 225)
        self.assertGreater(left_up[2], left_up[1], f"左上该是帧的蓝：{left_up}")
        self.assertGreater(left_down[2], left_down[1], f"左下该是深蓝：{left_down}")
        self.assertLess(left_down[2], left_up[2], "上下两条深浅不同 ⇒ 纵向映射真的生效了")
        self.assertGreater(right_up[1], right_up[2], f"右上该是绿：{right_up}")
        self.assertLess(right_down[1], right_up[1], "下半该更深")
        self.assertGreater(left_up[2], left_down[2] - 1)

    @staticmethod
    def gradient(w, h):
        """横向线性渐变当**映射标尺**：第 x 列亮度 = x/w。

        比"看象限颜色"精确得多 —— 象限判据对 1.3333× 这种"稍微大一点"的帧区分不出来
        （反例具型 AT 第一次就只在 2× 那档红）。渐变帧下，映射错一点亮度就错一点，跑不掉。
        """
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surface)
        gradient = cairo.LinearGradient(0, 0, w, 0)
        gradient.add_color_stop_rgb(0, 0, 0, 0)
        gradient.add_color_stop_rgb(1, 1, 1, 1)
        cr.set_source(gradient)
        cr.paint()
        return surface

    def test_gradient_frame_maps_exactly_for_1x_1_33x_and_2x(self):
        """1× / 1.3333× / 2× 三种帧：区域内每一列的亮度都必须等于 x/区域宽。"""
        area = (0, 0, 480, 240)
        for factor in (1, 1.3333, 2):
            with self.subTest(factor=factor):
                frame = self.gradient(int(480 * factor), int(240 * factor))
                # 选区盖满整块区域 ⇒ 没有压暗带（否则亮度被乘 0.65，标尺就不准了）
                out = self._render(area, frame, rect=(0, 0, 480, 240))
                for x in (60, 200, 400):
                    got = px(out, x, 120)[0] / 255
                    expect = x / area[2]
                    self.assertAlmostEqual(got, expect, delta=0.04,
                                           msg=f"{factor}× 帧在第 {x} 列映射错了："
                                               f"亮度 {got:.3f}，应当 {expect:.3f}")

    def test_quadrant_orientation_survives_scaling(self):
        area = (0, 0, 480, 240)
        for factor in (1, 1.3333, 2):
            with self.subTest(factor=factor):
                frame = self.checkerboard(int(480 * factor), int(240 * factor))
                out = self._render(area, frame)
                self.assertGreater(px(out, 120, 60)[2], px(out, 120, 60)[1],
                                   f"{factor}× 帧的左上应仍是蓝")
                self.assertGreater(px(out, 360, 60)[1], px(out, 360, 60)[2],
                                   f"{factor}× 帧的右上应仍是绿")

    def test_selection_rect_still_uses_logical_coordinates(self):
        """选区矩形是**逻辑坐标**（窗口那一层），帧的缩放不该把它带偏。

        用**单色**帧来比压暗：不同象限的深浅本来就不同，拿两种颜色比"谁亮"没有意义（第一版就写错了）。
        """
        area = (0, 0, 400, 200)
        frame = cairo.ImageSurface(cairo.FORMAT_ARGB32, 533, 266)   # 1.3333×
        cr = cairo.Context(frame)
        cr.set_source_rgb(*BLUE)
        cr.paint()
        out = self._render(area, frame, rect=(100, 50, 200, 100))
        inside = px(out, 200, 100)
        outside = px(out, 20, 20)
        self.assertAlmostEqual(inside[2] / 255, BLUE[2], delta=0.02,
                               msg=f"选区内该是帧的原色（不压暗）：{inside}")
        self.assertLess(outside[2] / 255, BLUE[2] - 0.1, f"选区外该压暗：{outside}")


class TestWindowGeometryIsSetExplicitly(unittest.TestCase):
    """窗口几何：**必须显式设尺寸再全屏**。这条正是今天的病根。

    实测（xvfb 复现了 lead 的真机数字）：`realize()` → `fullscreen_on_monitor()` → `show_all()`
    得到 device **200×200**（GTK 默认尺寸，被 mutter 拉伸铺满 ⇒ 全黑/一块矩形）；
    `set_default_size(mon_w, mon_h)` → 全屏 → show 得到 1600×900 ✓（期望值）。
    """

    class FakeWin:
        def __init__(self):
            self.calls = []

        def set_default_size(self, w, h):
            self.calls.append(("set_default_size", w, h))

        def fullscreen_on_monitor(self, screen, idx):
            self.calls.append(("fullscreen_on_monitor", screen, idx))

    def test_size_is_set_before_fullscreen(self):
        win = self.FakeWin()
        OV.overlay_geometry(win, "SCREEN", 1, (1920, 0, 1920, 1080))
        self.assertEqual(win.calls,
                         [("set_default_size", 1920, 1080), ("fullscreen_on_monitor", "SCREEN", 1)],
                         "必须先给尺寸再全屏（反了就是 200×200 的 stale 缓冲）")

    def test_uses_the_monitor_size_not_a_hardcoded_one(self):
        for monitor in ((0, 0, 1920, 1080), (3840, 540, 960, 540)):
            win = self.FakeWin()
            OV.overlay_geometry(win, "S", 0, monitor)
            self.assertEqual((win.calls[0][1], win.calls[0][2]), (monitor[2], monitor[3]))

    def test_build_calls_overlay_geometry_for_every_monitor(self):
        import inspect
        source = inspect.getsource(OV.Overlay.build)
        self.assertIn("overlay_geometry(win, screen, idx, mon)", source,
                      "build() 必须走 overlay_geometry（不然又会退回 200×200）")
        self.assertNotIn("win.realize()", source,
                         "提前 realize() 会让随后的全屏不生效（实测）")

    def test_expectation_accounts_for_the_monitor_scale(self):
        self.assertEqual(OV.geometry_expectation((0, 0, 960, 540), 2), (1920, 1080))
        self.assertEqual(OV.geometry_expectation((0, 0, 1920, 1080), 1), (1920, 1080))


class TestGeometrySelfCheck(unittest.TestCase):
    """map 之后自检：**不许静默**（不对就关窗 + 说清期望/实得）。"""

    def test_detects_the_exact_bug_from_today(self):
        problems = OV.geometry_problems([(0, 0, 1920, 1080)], [(200, 200)], [1])
        self.assertEqual(len(problems), 1)
        self.assertIn("200", problems[0])
        self.assertIn("1920", problems[0])

    def test_silent_when_correct(self):
        self.assertEqual(OV.geometry_problems([(0, 0, 1920, 1080)], [(1920, 1080)], [1]), [])

    def test_scale_two_monitor_accepts_both_interpretations(self):
        """2× 屏：设备像素（1920×1080）与逻辑像素（960×540）**都放行**。

        为什么不做严格判定：Wayland 上 GDK 报的是设备还是逻辑像素，我在无头环境里没能定论；
        只认一种的话，认错就是"每次自检失败、遮罩每次自己关掉" —— 比原来的 bug 更糟。
        而 200×200 两种解释都不合法 ⇒ 今天这个 bug 照样被抓住（见上一条）。
        """
        self.assertEqual(OV.geometry_problems([(0, 0, 960, 540)], [(1920, 1080)], [2]), [])
        self.assertEqual(OV.geometry_problems([(0, 0, 960, 540)], [(960, 540)], [2]), [])
        self.assertEqual(len(OV.geometry_problems([(0, 0, 960, 540)], [(200, 200)], [2])), 1,
                         "200×200 无论怎么解释都不合法")

    def test_small_rounding_is_tolerated(self):
        self.assertEqual(OV.geometry_problems([(0, 0, 1920, 1080)], [(1919, 1078)], [1]), [],
                         "差一两像素别判失败（有些合成器会少给一点）")

    def test_message_says_both_numbers_and_what_happened(self):
        msg = OV.geometry_message(OV.geometry_problems([(0, 0, 1920, 1080)], [(200, 200)], [1]))
        self.assertIn("1920", msg)
        self.assertIn("200", msg)
        self.assertIn("已关掉遮罩", msg, "要告诉用户遮罩没留在屏幕上")
        self.assertIn("没有拿这张图去截", msg)

    def test_overlay_starts_unknown_and_select_region_reports_the_failure(self):
        ov = OV.Overlay(monitors=[(0, 0, 10, 10)])
        self.assertIsNone(ov.geometry_ok, "还没量过就是 None（不许默认 True）")
        self.assertEqual(ov.failure, "")
        import inspect
        source = inspect.getsource(OV.select_region)
        self.assertIn("overlay.failure", source, "自检失败的话要交回调用方")

    @unittest.skipUnless(HAVE_CAIRO, "这条要造一张假冻结图（cairo）")
    def test_region_flow_refuses_when_the_overlay_reports_failure(self):
        """自检没过 ⇒ 不写文件、退出码非零（宁可说"这次没成"，也不给一张全黑的图）。"""
        import contextlib
        import io
        import tempfile
        from capsplus import shot_capture as SC
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-geom-test-"))
        out = tmp / "should-not-exist.png"
        monitors = [(0, 0, 800, 600)]

        def capture(x, y, w, h, path):
            pattern_surface(max(1, w), max(1, h)).write_to_png(str(path))
            return True, "✓"

        deps = {
            "monitors": monitors,
            "capture": capture,
            "freeze": lambda mons: SFR.freeze(mons, capture, out_dir=tmp),
            "select": lambda frames, mons: (None, "✗ 遮罩窗口尺寸没对上（期望 1920×1080，实得 200×200）"),
            "annotate": lambda *a, **k: ("close", None, ""),
            "no_pin": True, "no_text": True,
        }
        args = SC.parse_args(["--region", "--out", str(out)])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = SC.command_region(args, deps)
        self.assertEqual(code, 1, "几何失败必须非零退出")
        self.assertIn("200×200", err.getvalue(), "要把期望/实得说给他听")
        self.assertFalse(out.exists(), "几何失败时什么都不许写")

@unittest.skipUnless(not GUI_SKIP, "跳过界面用例")
class TestWindowsBackgroundNotOverpaint(unittest.TestCase):
    """**真 `Gtk.Window`** 的背景覆盖：自定义绘制必须 `return True`。

    2026-10-19 真机事故（lead 的证据）：窗口尺寸修对之后，屏幕上变成**整块纯白**（抓屏实测
    均匀 250.0、标准差 0.0 = Adwaita 的 `#fafafa`）。原因：`GtkWidget::draw` 的**默认处理器跑在
    自定义处理器之后**，把控件自己的主题背景刷在我们画的内容上面 ⇒ 必须 `return True`
    停止信号发射。修后同一块量到"平均 163.3 = 250×0.65、标准差 19.3"——正是"真实桌面 × 压暗"。

    ⚠ **不许用 `Gtk.DrawingArea` 代替 `Gtk.Window`**：DrawingArea 自己没有背景，
    拿它测这条会**假绿**（上一批的 `TestRealWidgetDrawPath` 就是这么漏掉整屏白的）。
    这里用 `Gtk.OffscreenWindow`（它就是 `Gtk.Window` 子类，有真实的主题背景绘制路径）。
    """

    def _render_through_a_real_window(self, monitors, frames, rect=None):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk
        ov = OV.Overlay(monitors=monitors)
        ov.frames = frames
        ov.rect = rect
        width, height = monitors[0][2], monitors[0][3]
        win = Gtk.OffscreenWindow()                 # ← 真 GtkWindow（有主题背景）
        win.set_default_size(width, height)
        win.connect("draw", lambda widget, cr: ov._on_draw(widget, cr, 0))
        win.show_all()
        for _ in range(60):
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
        window = win.get_window()
        pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, width, height)
        data, stride, n = pixbuf.get_pixels(), pixbuf.get_rowstride(), pixbuf.get_n_channels()

        def sample(x, y):
            off = y * stride + x * n
            return tuple(data[off + i] / 255 for i in range(3))

        samples = [sample(x, y) for x in range(5, width - 5, 17) for y in range(5, height - 5, 11)]
        mean = sum(sum(s) / 3 for s in samples) / len(samples)
        var = sum((sum(s) / 3 - mean) ** 2 for s in samples) / len(samples)
        win.destroy()
        return {"sample": sample, "mean": mean, "std": var ** 0.5,
                "size": (pixbuf.get_width(), pixbuf.get_height())}

    @staticmethod
    def _frame(w, h):
        """左半蓝、右半绿：用来断言"看到的是帧的内容"，而不是主题底色。"""
        return pattern_surface(w, h)

    def test_not_the_theme_background(self):
        """整块不能是主题底色（Adwaita ≈ 250 均匀灰）—— 这正是真机上那一片纯白。"""
        monitors = [(0, 0, 400, 200)]
        frames = [SFR.Frame((0, 0, 400, 200), self._frame(400, 200))]
        out = self._render_through_a_real_window(monitors, frames)
        self.assertEqual(out["size"], (400, 200))
        self.assertGreater(out["std"], 0.02,
                           f"整块是均匀色 ⇒ 主题背景/纯色盖住了内容：mean={out['mean']:.3f} "
                           f"std={out['std']:.3f}")
        self.assertGreater(abs(out["mean"] - 250 / 255), 0.03,
                           "均值正好是 Adwaita #fafafa（≈250）⇒ 就是这次那个整屏白（写反过一次，注意）")

    def test_content_and_dimming_survive_the_window_background(self):
        """帧的内容必须**在窗口这一层**看得见，而且选区外还是被压暗的（不是被白底换掉）。"""
        monitors = [(0, 0, 400, 200)]
        frames = [SFR.Frame((0, 0, 400, 200), self._frame(400, 200))]
        out = self._render_through_a_real_window(monitors, frames, rect=(200, 0, 200, 200))
        inside = out["sample"](350, 100)          # 右半 = 帧的绿，选区内 → 原色
        outside = out["sample"](50, 100)          # 左半 = 帧的蓝，选区外 → 压暗
        self.assertGreater(inside[1], inside[2], f"选区内该是帧的绿：{inside}")
        self.assertGreater(outside[2], outside[1], f"选区外该是帧的蓝：{outside}")
        self.assertAlmostEqual(outside[2], BLUE[2] * (1 - OV.DIM_ALPHA), delta=0.05,
                               msg=f"选区外该是 帧色×0.65（真机修后就是这个数）：{outside}")
        self.assertAlmostEqual(inside[1], GREEN[1], delta=0.05,
                               msg=f"选区内该是帧的原色（不压暗）：{inside}")

    def test_the_dimmed_mean_matches_the_real_machine_reading(self):
        """整块均值应当是"帧的平均色 × 0.65"量级 —— 真机修后量到 163.3/255 ≈ 0.64。"""
        monitors = [(0, 0, 400, 200)]
        frame_surface = self._frame(400, 200)
        frames = [SFR.Frame((0, 0, 400, 200), frame_surface)]
        out = self._render_through_a_real_window(monitors, frames)
        self.assertGreater(out["mean"], 0.2, f"不该是纯白/纯黑：{out['mean']:.3f}")
        self.assertLess(out["mean"], 0.9, f"不该是主题白：{out['mean']:.3f}")

    def test_draw_handler_returns_true(self):
        """静态那半：源码里 `_on_draw` 必须以 `return True` 收尾（停掉默认背景绘制）。"""
        import inspect
        source = inspect.getsource(OV.Overlay._on_draw)
        body = [line.strip() for line in source.splitlines() if line.strip()
                and not line.strip().startswith("#")]
        self.assertEqual(body[-1], "return True",
                         "`_on_draw` 必须以 return True 收尾：默认处理器会在之后刷主题背景（整屏白）")

    def test_judgment_uses_a_real_window_not_a_drawing_area(self):
        """这条判据自己也要钉住"渲染路径用的是真 Gtk.Window" —— 用 DrawingArea 测会假绿。

        只扫**渲染那段的代码**（类文档里正好写着"不许用 DrawingArea"的告示，扫全文会自伤 —— 第一版就撞了）。
        """
        import inspect
        code = inspect.getsource(self._render_through_a_real_window)
        code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))
        self.assertIn("Gtk.OffscreenWindow", code)
        self.assertNotIn("Gtk.DrawingArea", code,
                         "窗口背景那一层必须用真 Gtk.Window：DrawingArea 没有背景，测不出来")


@unittest.skipUnless(not GUI_SKIP, "跳过界面用例")
class TestRealWidgetDrawPath(unittest.TestCase):
    """**真控件路径**：`DrawingArea` 走真实 draw 处理器，读回像素。

    为什么非要这条（lead 2026-10-19 点出的盲区）：我原来的像素判据都是**直接调 `paint()`** 渲染到
    ImageSurface —— 完全绕开 GTK 窗口那一层，所以「窗口 200×200」「draw 没接上」这类 bug 一条都拦不住。

    ⚠ **这条是必要的，但不充分**（2026-10-19 整屏白事故的教训）：`Gtk.DrawingArea` **本来就没有
    自己的背景** ⇒ 它证明不了"窗口的主题背景有没有盖上来"。真机上 Adwaita 把 `#fafafa ≈ 250`
    刷在遮罩上、整屏纯白，而这一整类判据**全绿**。窗口背景那一层必须用**真 `Gtk.Window`** 测，
    见 `TestWindowsBackgroundNotOverpaint`（geok 结论：`draw` 里自定义绘制必须 `return True`）。
    """

    def _render_through_gtk(self, monitors, frames, rect=None):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk
        ov = OV.Overlay(monitors=monitors)
        ov.frames = frames
        ov.rect = rect
        area_w, area_h = monitors[0][2], monitors[0][3]
        offscreen = Gtk.OffscreenWindow()
        offscreen.set_default_size(area_w, area_h)
        area = Gtk.DrawingArea()
        area.set_size_request(area_w, area_h)
        area.connect("draw", lambda widget, cr: ov._on_draw(widget, cr, 0))
        offscreen.add(area)
        offscreen.show_all()
        for _ in range(50):                     # 让 GTK 真把这一帧画出来
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
        window = offscreen.get_window()
        pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, area_w, area_h)
        data = pixbuf.get_pixels()
        stride = pixbuf.get_rowstride()
        channels = pixbuf.get_n_channels()
        out = {"size": (pixbuf.get_width(), pixbuf.get_height()),
               "sample": lambda x, y: tuple(data[y * stride + x * channels + i] / 255
                                            for i in range(3))}
        offscreen.destroy()
        return out

    def test_real_draw_shows_the_frozen_frame(self):
        monitors = [(0, 0, 400, 200)]
        frames = [SFR.Frame((0, 0, 400, 200), pattern_surface(400, 200))]
        out = self._render_through_gtk(monitors, frames)
        self.assertEqual(out["size"], (400, 200), "读回来的就是这块屏那么大")
        left = out["sample"](60, 100)           # 左半 = 帧的蓝
        right = out["sample"](340, 100)         # 右半 = 帧的绿
        self.assertGreater(left[2], left[1], f"真控件路径下该看到帧的蓝色：{left}")
        self.assertGreater(right[1], right[2], f"真控件路径下该看到帧的绿色：{right}")
        dark = out["sample"](10, 190)
        self.assertGreater(sum(left) + sum(right), 0.3,
                           f"整块不该是纯黑/主题底色（真机黑屏就是这条要拦的）：{left} {right} {dark}")

    def test_real_draw_with_an_oversized_frame_still_fills_the_area(self):
        monitors = [(0, 0, 300, 150)]
        frames = [SFR.Frame((0, 0, 300, 150), pattern_surface(400, 200))]     # 帧比窗口大
        out = self._render_through_gtk(monitors, frames)
        left = out["sample"](45, 75)
        right = out["sample"](255, 75)
        self.assertGreater(left[2], left[1], f"1.3333× 的帧也要铺满：{left}")
        self.assertGreater(right[1], right[2], f"1.3333× 的帧也要铺满：{right}")

    def test_real_draw_shows_the_dimmed_outside_and_the_bright_inside(self):
        monitors = [(0, 0, 400, 200)]
        frames = [SFR.Frame((0, 0, 400, 200), pattern_surface(400, 200))]
        out = self._render_through_gtk(monitors, frames, rect=(100, 50, 200, 100))
        inside = out["sample"](200, 100)
        outside = out["sample"](10, 190)
        self.assertGreater(sum(inside), sum(outside), "选区内不压暗、外压暗（真控件路径）")


@unittest.skipUnless(not GUI_SKIP, "跳过界面用例")
class TestRealWindow(unittest.TestCase):
    """真建窗（xvfb）：窗口真的把冻结图画出来了吗 —— 只有真 realize 一次才看得出。"""

    def _run(self, monitors, frames, then):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gdk, GLib
        state = {}

        def main():
            loop = GLib.MainLoop()
            ov = OV.Overlay(monitors=monitors)
            ov.frames = frames
            ov.build(Gdk.Display.get_default(), loop.quit)
            then(ov, state)
            for win in ov.windows:
                win.destroy()
            return False

        GLib.timeout_add(0, lambda: (main(), False)[1])
        loop = GLib.MainLoop()
        GLib.timeout_add(3000, lambda: (loop.quit(), False)[1])
        loop.run()
        return state

    def test_window_draws_the_frozen_frame(self):
        if not HAVE_CAIRO:
            self.skipTest("没有 pycairo")
        monitors = [(0, 0, 400, 300)]
        frames = [SFR.Frame((0, 0, 400, 300), pattern_surface(400, 300))]

        def then(ov, state):
            ov.rect = (50, 50, 100, 100)

            class _StubWidget:               # `_on_draw` 只用到"分配尺寸"与显示器几何
                def __init__(self, w, h):
                    self._w, self._h = w, h

                def get_allocated_width(self):
                    return self._w

                def get_allocated_height(self):
                    return self._h

            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
            cr = cairo.Context(surface)
            ov._on_draw(_StubWidget(400, 300), cr, 0)
            state["inside"] = px(surface, 120, 100)
            state["outside_alpha"] = px(surface, 20, 280)[3]

        state = self._run(monitors, frames, then)
        self.assertGreater(state["inside"][2], 0.7, f"选区里该看到冻结图的图案：{state}")
        self.assertEqual(state["outside_alpha"], 255, "窗口内容必须不透明")

    def test_windows_are_fullscreen_per_monitor(self):
        monitors = [(0, 0, 320, 240), (320, 0, 320, 240)]
        frames = [SFR.Frame(m, pattern_surface(m[2], m[3])) for m in monitors]
        state = {}
        self._run(monitors, frames, lambda ov, st: st.update(windows=len(ov.windows)))
        state = self._run(monitors, frames, lambda ov, st: st.update(windows=len(ov.windows)))
        self.assertEqual(state.get("windows"), 2, "一屏一窗（跨屏选区靠多窗各自裁剪拼出来）")

    def test_select_region_requires_frames(self):
        """没有冻结帧就不该开窗（契约）：宁可报错，也不给全黑盲选。"""
        sig = inspect.signature(OV.select_region)
        self.assertEqual(list(sig.parameters)[0], "frames", "冻结帧是选区的必需参数")


if __name__ == "__main__":
    unittest.main()