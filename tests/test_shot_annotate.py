# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：标注窗（第 3 步）—— 撤销/重做、坐标换算、四种图元的**像素**结果、存盘与剪贴板。

为什么连像素也判：四种图元里"马赛克像不像马赛克"、"箭头有没有箭头"是**画出来的事实**，
不是配置项。判据把它们渲染到 `ImageSurface` 上逐像素断言（mosaic 那条断言"块内像素相同"），
这样换一个实现（比如以后改用 PIL）也得先满足同一条口径。

两条不许踩的红线（判据直接扫源码）：
  * 界面判据不许动明江的剪贴板 ⇒ `Annotator` 的复制走注入的 `clip`，测试里给 stub；
  * 标注窗不许动画（`ease`/`set_opacity`/`transition`），与遮罩同一条口径。
"""

import ast
import pathlib
import re
import subprocess
import shutil
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_annotate as AN          # noqa: E402
from capsplus import shot_frame as SF             # noqa: E402

try:
    import cairo
    HAVE_CAIRO = True
except Exception:                                  # pragma: no cover
    cairo = None
    HAVE_CAIRO = False

PNG_1PX = bytes([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1,
                 8, 2, 0, 0, 0, 144, 119, 83, 222, 0, 0, 0, 12, 73, 68, 65, 84, 8, 215, 99, 248,
                 207, 192, 0, 0, 3, 1, 1, 0, 24, 221, 141, 176, 0, 0, 0, 0, 73, 69, 78, 68, 174,
                 66, 96, 130])

RUN_GUI = __import__("os").environ.get("CAPSPLUS_GUI") == "1"
ALLOW_WAYLAND_DESKTOP = __import__("os").environ.get("CAPSPLUS_WAYLAND") == "1"


def white_surface(w=120, h=100):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(s)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    return s


def grey_gradient_surface(w=120, h=100):
    """左黑右白的横向渐变：马赛克那条判据用它看"块内是否被抹平"。"""
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(s)
    grad = cairo.LinearGradient(0, 0, w, 0)
    grad.add_color_stop_rgb(0, 0, 0, 0)
    grad.add_color_stop_rgb(1, 1, 1, 1)
    cr.set_source(grad)
    cr.paint()
    return s


def px(surface, x, y):
    surface.flush()
    stride = surface.get_stride()
    buf = bytes(surface.get_data())
    off = y * stride + x * 4
    b, g, r, a = buf[off], buf[off + 1], buf[off + 2], buf[off + 3]
    return (r / 255, g / 255, b / 255, a / 255)



# 临时目录统一入口：判据**不许往 /tmp 里堆垃圾**（跑一轮留一堆目录 = 事后审计的负担，
# 2026-10-19 实测一次全量跑就留下两百多个）。tearDownModule 保证一个测试模块跑完全清。
_TEMP_DIRS = []


def temp_dir(prefix: str = "capsplus-shot-test-"):
    d = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append(d)
    return d


def tearDownModule():
    for d in _TEMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    _TEMP_DIRS.clear()


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

class TestShapeStack(unittest.TestCase):
    def test_add_undo_redo(self):
        st = AN.ShapeStack()
        self.assertFalse(st.can_undo)
        st.add(AN.Shape("rect", (0, 0, 10, 10)))
        st.add(AN.Shape("arrow", (5, 5, 20, 20)))
        self.assertEqual(len(st), 2)
        self.assertTrue(st.undo())
        self.assertEqual(len(st), 1)
        self.assertTrue(st.can_redo)
        self.assertTrue(st.redo())
        self.assertEqual(len(st), 2)
        self.assertFalse(st.redo())

    def test_new_shape_clears_the_redo_branch(self):
        st = AN.ShapeStack()
        st.add(AN.Shape("rect", (0, 0, 10, 10)))
        st.add(AN.Shape("rect", (0, 0, 20, 20)))
        st.undo()
        st.add(AN.Shape("arrow", (1, 1, 5, 5)))
        self.assertFalse(st.can_redo, "画了新的一笔之后，原来的『重做』必须作废（所有画图软件都这样）")
        self.assertEqual([s.kind for s in st.shapes], ["rect", "arrow"])

    def test_undo_redo_at_edges_are_noops(self):
        st = AN.ShapeStack()
        self.assertFalse(st.undo())
        self.assertFalse(st.redo())
        self.assertEqual(st.shapes, [])

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(AssertionError):
            AN.Shape("blur", (0, 0, 1, 1))


class TestGeometry(unittest.TestCase):
    def test_fit_scale_only_shrinks(self):
        self.assertEqual(AN.fit_scale((100, 100), (1000, 1000)), 1.0, "小图不许被放大（糊）")
        self.assertAlmostEqual(AN.fit_scale((4800, 1080), (1920, 1080)), 0.4)

    def test_image_coords_are_divided_by_scale(self):
        self.assertEqual(AN.to_image_coords((200, 100), 0.5), (400, 200),
                         "窗口显示的是缩放后的图，鼠标坐标必须除回去，否则标注位置整体偏")
        self.assertEqual(AN.to_image_coords((10, 10), 0), (0, 0))

    def test_arrow_head_points_back_towards_the_start(self):
        tip, left, right = AN.arrow_head((0, 0), (100, 0), size=10)
        self.assertEqual(tip, (100, 0))
        self.assertLess(left[0], 100)
        self.assertLess(right[0], 100)
        self.assertNotEqual(left[1], right[1], "两翼要在箭头两侧，不能重合")


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：像素判据跳过")
class TestRenderPixels(unittest.TestCase):
    def _render(self, base, shapes):
        out = AN.render_to_surface(base, shapes)
        return out

    def test_rect_draws_a_border_but_not_a_fill(self):
        out = self._render(white_surface(), [AN.Shape("rect", (20, 20, 60, 40))])
        r, g, b, _a = px(out, 50, 20)
        self.assertGreater(r, 0.7)
        self.assertLess(g, 0.4, "边框应是红的")
        self.assertGreater(px(out, 50, 40)[0], 0.95, "框内不该被涂满（标注不能挡住内容）")
        self.assertGreater(px(out, 5, 5)[0], 0.95)

    def test_arrow_leaves_a_line_between_the_two_ends(self):
        shapes = [AN.Shape("arrow", (10, 10, 60, 40))]      # 左下 → 右上
        out = self._render(white_surface(), shapes)
        hits = sum(1 for t in range(0, 40)
                   if px(out, 10 + int(60 * t / 40), 50 - int(40 * t / 40))[1] < 0.5)
        self.assertGreater(hits, 15, "箭身那条线得画出来")
        tip_area_red = any(px(out, x, y)[1] < 0.5 for x in range(60, 72) for y in range(6, 20))
        self.assertTrue(tip_area_red, "箭头得有个箭头")

    def test_text_puts_ink_on_the_canvas(self):
        out = self._render(white_surface(), [AN.Shape("text", (10, 10, 1, 1), text="丢")])
        ink = any(px(out, x, y)[1] < 0.5 for x in range(10, 45) for y in range(10, 40))
        self.assertTrue(ink, "文字得有笔画")

    def test_mosaic_equalises_pixels_inside_each_block(self):
        base = grey_gradient_surface()
        out = self._render(base, [AN.Shape("mosaic", (20, 20, 60, 40))])
        block = AN.MOSAIC_BLOCK
        for bx in range(20, 80, block):
            for by in range(20, 60, block):
                pts = {px(out, x, y)
                       for x in range(bx + 1, min(bx + block, 79), 3)
                       for y in range(by + 1, min(by + block, 59), 3)}
                self.assertEqual(len(pts), 1,
                                 f"块 ({bx},{by}) 内像素必须一致（马赛克就是把块抹平），"
                                 f"实际拿到 {len(pts)} 种：{sorted(pts)[:3]}")
        self.assertGreater(len({px(out, 20 + k * block, 30) for k in range(5)}), 1,
                           "不同块之间还得有区别，否则整块变成一个颜色 = 糊死了")

    def test_mosaic_really_changes_the_original(self):
        base = grey_gradient_surface()
        out = self._render(base, [AN.Shape("mosaic", (20, 20, 60, 40))])
        changed = sum(1 for x in range(21, 79) if abs(px(out, x, 30)[0] - px(base, x, 30)[0]) > 0.002)
        self.assertGreater(changed, 20, "马赛克区域必须与原图不同（否则等于没画）")

    def test_undone_shapes_are_not_rendered(self):
        st = AN.ShapeStack()
        st.add(AN.Shape("rect", (20, 20, 60, 40)))
        st.undo()
        out = self._render(white_surface(), st.shapes)
        self.assertGreater(px(out, 50, 20)[0], 0.95, "撤销掉的那笔不许还留在画面上")


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：存盘判据跳过")
class TestSaveAndClipboard(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir("capsplus-annot-test-")

    def test_save_writes_a_complete_png_with_a_timestamped_name(self):
        path = AN.save_named(white_surface(), [AN.Shape("rect", (5, 5, 20, 20))], self.dir)
        self.assertTrue(path.exists())
        self.assertRegex(path.name, r"^shot-\d{4}-\d{2}-\d{2}-\d{6}\.png$")
        self.assertTrue(SF.is_complete_png(path)[0], "存下来的必须是完整 PNG")
        self.assertEqual(path.parent, self.dir)

    def test_saved_file_is_readable_and_keeps_the_shapes(self):
        path = AN.save_named(white_surface(), [AN.Shape("rect", (20, 20, 60, 40))], self.dir)
        back = cairo.ImageSurface.create_from_png(str(path))
        self.assertEqual((back.get_width(), back.get_height()), (120, 100))
        self.assertLess(px(back, 50, 20)[1], 0.5, "存下来的图里必须带着刚画的标注")

    def test_clipboard_command_is_wl_copy_with_png_type(self):
        self.assertEqual(AN.clipboard_command(), ["wl-copy", "--type", "image/png"])

    def test_copy_pipes_png_bytes_to_the_command(self):
        seen = {}

        class P:
            returncode = 0
            stderr = b""

        def runner(argv, data):
            seen["argv"] = argv
            seen["data"] = data
            return P()

        ok, msg = AN.copy_to_clipboard(b"x", runner=runner, which=lambda _n: "/usr/bin/wl-copy")
        self.assertTrue(ok, msg)
        self.assertEqual(seen["argv"], ["wl-copy", "--type", "image/png"])

    def test_copy_reports_honestly_when_wl_copy_is_missing(self):
        ok, msg = AN.copy_to_clipboard(b"x", which=lambda _n: None)
        self.assertFalse(ok)
        self.assertIn("wl-copy", msg, "没有工具时不许假装成功（诚实纪律）")

    def test_png_bytes_round_trip(self):
        data = AN.png_bytes(white_surface(10, 10))
        self.assertTrue(data.startswith(SF.PNG_MAGIC))
        self.assertIn(SF.PNG_IEND, data)


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：状态机判据跳过")
class TestAnnotatorStateMachine(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir("capsplus-annot-test-")
        self.copied = []
        self.pinned = []

        def clip(data):
            self.copied.append(data)
            return True, "✓ stub"

        def pin(surface):
            self.pinned.append(surface)
            return True, "✓ stub 钉上了"

        self.an = AN.Annotator(white_surface(200, 100), on_pin=pin, screen=(100, 50),
                              directory=self.dir, clip=clip)

    def test_window_shows_the_image_scaled(self):
        self.assertAlmostEqual(self.an.scale, 0.5, msg="200×100 塞进 100×50 的屏 → 0.5")

    class _Ev:
        """假事件：`_image_xy` 只读 .x/.y 两个属性，判据不用真鼠标。"""

        def __init__(self, x, y):
            self.x, self.y = x, y

    def test_window_coords_are_converted_to_image_coords(self):
        """缩放 0.5 时，窗口 (20,10) 就是图像 (40,20) —— 这条错了标注位置会整体偏。"""
        self.assertEqual(self.an._image_xy(self._Ev(20, 10)), (40, 20))
        self.assertEqual(self.an._image_xy(self._Ev(120, 60)), (240, 120))

    def test_drag_commits_a_shape_in_image_coordinates(self):
        self.an.set_tool("rect")
        self.an.press(self.an._image_xy(self._Ev(20, 10)))       # 图像 (40,20)
        self.an.drag(self.an._image_xy(self._Ev(120, 60)))       # 图像 (240,120)
        self.assertTrue(self.an.release())
        self.assertEqual(self.an.stack.shapes[0].rect, (40, 20, 200, 100))

    def test_tiny_drag_is_dropped(self):
        self.an.press((10, 10))
        self.an.drag((12, 12))
        self.assertFalse(self.an.release(), "手抖点一下不该留下一个 1×1 的框")
        self.assertEqual(len(self.an.stack), 0)

    def test_text_tool_asks_for_input_and_commits(self):
        self.an.set_tool("text")
        self.assertEqual(self.an.click((30, 30)), "text-input")
        self.assertTrue(self.an.commit_text((30, 30), "  hello  "))
        self.assertEqual(self.an.stack.shapes[0].text, "hello")
        self.assertFalse(self.an.commit_text((30, 30), "   "), "空文字不许落地")

    def test_actions_drive_the_stack_and_the_clipboard_stub(self):
        self.an.press((0, 0))
        self.an.drag((100, 50))
        self.an.release()
        self.an.action(AN.ACTION_UNDO)
        self.assertEqual(len(self.an.stack), 0)
        self.an.action(AN.ACTION_REDO)
        self.assertEqual(len(self.an.stack), 1)
        self.an.action(AN.ACTION_COPY)
        self.assertEqual(self.an.result, "copy")
        self.assertTrue(self.copied and self.copied[0].startswith(SF.PNG_MAGIC),
                        "复制喂给剪贴板的必须是 PNG 字节")

    def test_save_action_reports_the_path(self):
        self.an.press((0, 0))
        self.an.drag((100, 50))
        self.an.release()
        self.an.action(AN.ACTION_SAVE)
        self.assertTrue(self.an.saved_path.exists())
        self.assertEqual(self.an.result, "save")

    def test_pin_action_calls_the_injected_hook(self):
        self.an.press((0, 0))
        self.an.drag((100, 50))
        self.an.release()
        self.an.action(AN.ACTION_PIN)
        self.assertEqual(self.an.result, "pin")
        self.assertEqual(len(self.pinned), 1, "钉屏要拿到渲染好的图（带标注）")

    def test_pin_without_hook_is_reported_not_faked(self):
        an = AN.Annotator(white_surface(), clip=lambda _d: (True, "stub"))
        an.action(AN.ACTION_PIN)
        self.assertIsNone(an.result)
        self.assertIn("没有钉屏回调", an.message)

    def test_pending_shape_is_drawn_while_dragging(self):
        self.an.press((0, 0))
        self.an.drag((50, 25))
        self.assertEqual(len(self.an.shapes_to_draw()), 1, "拖动过程中要能看见橡皮筋（未提交也算画）")
        self.assertEqual(len(self.an.stack), 0, "松手之前不进撤销栈")


class TestNoAnimation(unittest.TestCase):
    """与遮罩同一条口径：标注窗也不许有动画机制。"""

    SRC = (REPO / "capsplus" / "shot_annotate.py").read_text(encoding="utf-8")

    @staticmethod
    def code_only(text: str) -> str:
        text = re.sub(r'"""[\s\S]*?"""', "", text)
        return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())

    def test_no_animation_primitives(self):
        code = self.code_only(self.SRC)
        for banned in ("ease(", "set_opacity", "transition", "Clutter", "animate", "fade"):
            self.assertIsNone(re.search(r"\b" + re.escape(banned), code),
                              f"{banned}：标注窗也不许有动画（不抢焦点的静态窗口最省事）")

    def test_no_input_grab_and_no_keep_above(self):
        code = self.code_only(self.SRC)
        for banned in ("grab(", "set_keep_above", "set_focus(", "present_with_time"):
            self.assertNotIn(banned, code,
                             f"{banned} 在本机 Wayland 上要么无效、要么会去动别人的窗口"
                             "（仓里 voice_osd 的既有口径）")

    def test_top_level_imports_stay_pure(self):
        tree = ast.parse(self.SRC)
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        # 允许 cairo/gi 之外的一切标准库与**仓内纯模块**（`capsplus.*` —— shot_panel 本身也纯）；
        # 禁的是"CI 上装不出来的那几样"：gi / cairo / PIL。
        for banned in ("gi", "cairo", "PIL", "numpy"):
            self.assertNotIn(banned, imported, f"顶层不许 import {banned}，实际：{sorted(imported)}")
        self.assertIn("capsplus", imported,
                      "面板模型是仓内纯模块，顶层 import 它是可以的（判据别把这条误伤）")


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：第二批像素判据跳过")
class TestSecondBatchTools(unittest.TestCase):
    """第二批新工具：荧光笔（半透明）、序号圆标、颜色与线宽可选。"""

    def test_tool_roster_and_labels_cover_the_new_tools(self):
        for tool in ("highlight", "badge"):
            self.assertIn(tool, AN.TOOLS)
            self.assertIn(tool, AN.TOOL_LABELS)

    def test_highlight_forces_a_translucent_colour(self):
        shape = AN.Shape("highlight", (0, 0, 10, 10), color=(1, 0, 0, 1))     # 故意传不透明的
        self.assertAlmostEqual(shape.color[3], AN.HIGHLIGHT_ALPHA,
                               msg="荧光笔必须半透明，否则等于拿色块把内容盖住")

    def test_highlight_blends_instead_of_covering(self):
        base = white_surface()
        out = AN.render_to_surface(base, [AN.Shape("highlight", (20, 20, 60, 40))])
        r, g, b, _a = px(out, 50, 40)
        self.assertLess(r, 0.99, "高亮区必须与原图不同")
        self.assertGreater(g, 0.5, "半透明：底下的白还在，不该变成纯红（那就成了矩形填充）")

    def test_badge_numbers_increment_and_redo_reuses_the_number(self):
        an = AN.Annotator(white_surface(200, 100), clip=lambda _d: (True, "stub"))
        self.assertEqual(an.next_badge_number(), 1)
        an.set_tool("badge")
        an.click((30, 30))
        self.assertEqual(an.stack.shapes[-1].text, "1")
        an.click((80, 60))
        self.assertEqual(an.stack.shapes[-1].text, "2")
        an.stack.undo()
        self.assertEqual(an.next_badge_number(), 2, "撤销一个之后，下一个接着用那个号")

    def test_badge_is_a_filled_circle_with_white_ink(self):
        base = white_surface(200, 100)
        out = AN.render_to_surface(base, [AN.Shape("badge", (20, 20, 28, 28), text="3")])
        ring = px(out, 20 + AN.BADGE_RADIUS, 20 + 2)                 # 圆的顶部
        self.assertGreater(ring[0], 0.8)
        self.assertLess(ring[1], 0.4, "圆标要用当前颜色画实心圆")
        centre = px(out, 20 + AN.BADGE_RADIUS, 20 + AN.BADGE_RADIUS)
        self.assertGreater(centre[1], 0.5, "圆心是白字（不透明），不是纯色圆点")

    def test_colour_and_width_are_selectable_and_apply_to_new_shapes(self):
        an = AN.Annotator(white_surface(200, 100), clip=lambda _d: (True, "stub"))
        blue = [c for name, c in AN.COLORS if name == "蓝"][0]
        an.set_color(blue)
        an.set_width(6)
        an.press((10, 10))
        an.drag((60, 40))
        an.release()
        shape = an.stack.shapes[-1]
        self.assertEqual(shape.color, blue)
        self.assertEqual(shape.width, 6)

    def test_unknown_colour_or_width_is_refused(self):
        an = AN.Annotator(white_surface(200, 100), clip=lambda _d: (True, "stub"))
        before = (an.color, an.width)
        an.set_color((0.1, 0.2, 0.3, 1.0))        # 不在调色板里
        an.set_width(99)
        self.assertEqual((an.color, an.width), before, "调色板之外的取值必须被挡住")


class TestKeyRoster(unittest.TestCase):
    """窗内键位：**唯一一份定义**在 `key_command()`，提示条与事件处理都读它。"""

    def test_function_keys_map_to_the_three_features(self):
        self.assertEqual(AN.key_command("F2"), AN.ACTION_TRANSLATE)
        self.assertEqual(AN.key_command("F3"), AN.ACTION_PIN)
        self.assertEqual(AN.key_command("F4"), AN.ACTION_OCR)

    def test_first_batch_keys_still_work_and_do_not_collide(self):
        self.assertEqual(AN.key_command("Escape"), AN.ACTION_CLOSE)
        self.assertEqual(AN.key_command("c", ctrl=True), AN.ACTION_COPY)
        self.assertEqual(AN.key_command("S", ctrl=True), AN.ACTION_SAVE)
        self.assertEqual(AN.key_command("z", ctrl=True), AN.ACTION_UNDO)
        self.assertEqual(AN.key_command("z", ctrl=True, shift=True), AN.ACTION_REDO)
        self.assertEqual(AN.key_command("y", ctrl=True), AN.ACTION_REDO)

    def test_enter_is_left_to_the_first_batch(self):
        self.assertIsNone(AN.key_command("Return"),
                          "回车在第一批是『确认选区』；标注窗不许抢它")
        self.assertIsNone(AN.key_command("KP_Enter"))

    def test_unknown_keys_are_not_swallowed(self):
        for name in ("a", "F5", "F1", "space", ""):
            self.assertIsNone(AN.key_command(name),
                              f"{name} 没被接手就该让 GTK 照常处理（F1 是全局热键，窗内不管）")

    def test_every_hotkey_has_exactly_one_action(self):
        actions = list(AN.KEY_ACTIONS.values())
        self.assertEqual(len(actions), len(set(actions)), "两个 F 键映射到同一个动作了")
        for action in actions:
            self.assertTrue(action, "空动作 = 按了没反应")


class TestHintBar(unittest.TestCase):
    def test_focused_hint_lists_the_keys(self):
        text = AN.hint_text(True)
        for token in ("F2", "F3", "F4", "Ctrl+C", "Ctrl+S", "Esc"):
            self.assertIn(token, text)

    def test_unfocused_hint_says_keys_do_not_work_and_how_to_fix(self):
        text = AN.hint_text(False)
        self.assertIn("没有焦点", text)
        self.assertIn("不会", text, "失焦时必须说清『按了不会生效』，不能假装按下去了")
        self.assertIn("点一下窗口", text, "要给下一步动作")

    def test_hints_differ(self):
        self.assertNotEqual(AN.hint_text(True), AN.hint_text(False))


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：面板集成判据要真渲染一次")
class TestPanelIntegration(unittest.TestCase):
    """F4/F2 的接线：provider 注入 stub，**不碰真模型、不碰真网络**。"""

    def setUp(self):
        self.dir = temp_dir("capsplus-annot-test-")
        self.ocr_calls = []
        self.tr_calls = []

        def on_ocr(surface):
            self.ocr_calls.append(surface.get_width())
            return {"ok": True, "text": "识别到的原文", "backend": "local", "ms": 42,
                    "note": "rapidocr"}

        def on_translate(text):
            self.tr_calls.append(text)
            return {"ok": True, "text": "translated", "src": text, "backend": "local", "ms": 900,
                    "note": "qwen3.8-27b"}

        self.an = AN.Annotator(white_surface(200, 100), screen=(100, 50), directory=self.dir,
                               clip=lambda _d: (True, "stub"), on_ocr=on_ocr,
                               on_translate=on_translate)

    def test_f4_fills_the_panel_with_the_ocr_result(self):
        self.an.action(AN.ACTION_OCR)
        self.assertEqual(self.an.panel.ocr_text, "识别到的原文")
        self.assertIn("本地", self.an.panel.headline())
        self.assertEqual(self.ocr_calls, [200], "送进 OCR 的是**渲染后**的画面（带标注）")

    def test_f2_without_ocr_text_asks_for_f4_first(self):
        ok = self.an.action(AN.ACTION_TRANSLATE)
        self.assertFalse(ok)
        self.assertEqual(self.tr_calls, [], "没有原文就别去惊动模型")
        self.assertIn("F4", self.an.panel.message, "话术要给出下一步：先按 F4")

    def test_f2_after_f4_translates_the_ocr_text(self):
        self.an.action(AN.ACTION_OCR)
        self.an.action(AN.ACTION_TRANSLATE)
        self.assertEqual(self.tr_calls, ["识别到的原文"], "翻译的输入必须是 OCR 原文")
        self.assertEqual(self.an.panel.dst_text, "translated")
        self.assertEqual(self.an.panel.src_text, "识别到的原文")

    def test_provider_failure_surfaces_the_hint(self):
        an = AN.Annotator(white_surface(), screen=(100, 50), clip=lambda _d: (True, "stub"),
                          on_ocr=lambda _s: {"ok": False, "hint": "✗ 本机模型没起：看 12087 端口"},
                          on_translate=lambda _t: None)
        an.action(AN.ACTION_OCR)
        self.assertIn("12087", an.panel.message)
        an2 = AN.Annotator(white_surface(), screen=(100, 50), clip=lambda _d: (True, "stub"),
                           on_ocr=lambda _s: None)
        an2.action(AN.ACTION_OCR)
        self.assertIn("没带原因", an2.panel.message, "provider 返回 None 也要说清楚，不许静默")

    def test_missing_provider_is_reported_not_crashed(self):
        an = AN.Annotator(white_surface(), screen=(100, 50), clip=lambda _d: (True, "stub"))
        an.action(AN.ACTION_OCR)
        self.assertIn("没接 OCR", an.panel.message)
        an.action(AN.ACTION_TRANSLATE)
        self.assertIn("没接", an.panel.message)

    def test_switch_and_view_toggle_are_pure_state(self):
        self.an.action(AN.ACTION_OCR)
        self.an.panel.backend = "local"
        self.assertEqual(self.an._switch_panel_backend(), "cloud")
        self.assertEqual(self.an._switch_panel_backend(), "local")
        self.an.action(AN.ACTION_TRANSLATE)
        first = self.an.panel.view
        self.an._toggle_panel_view()
        self.assertNotEqual(self.an.panel.view, first)


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：异步判据要真渲染一次")
class TestTextActionsDoNotFreezeTheWindow(unittest.TestCase):
    """F2/F4 不能冻住界面：有主循环时必须走后台线程，结果回主线程再碰界面。

    为什么要这条：本机 27B 翻译实测 1 秒起、百度 OCR 三百多毫秒 —— 同步跑等于窗子卡死，
    而"卡住"在截图工具里是最容易被骂的体验（明江换掉 Snipaste 就是因为闪）。
    """

    def setUp(self):
        self.dir = temp_dir("capsplus-async-test-")
        self.done = []

    def _annotator(self, delay=0.05):
        def on_ocr(_surface):
            time.sleep(delay)                    # 假装在算
            self.done.append("ocr")
            return {"ok": True, "text": "识别到", "backend": "local", "ms": 50}

        return AN.Annotator(white_surface(80, 40), screen=(80, 40), directory=self.dir,
                            clip=lambda _d: (True, "s"), on_ocr=on_ocr)

    def test_sync_path_when_there_is_no_main_loop(self):
        an = self._annotator(delay=0)
        self.assertIsNone(an._loop)
        self.assertTrue(an.action(AN.ACTION_OCR), "没有主循环时就是同步（判据/无窗场景）")
        self.assertEqual(an.panel.ocr_text, "识别到")

    def test_threaded_path_when_a_loop_exists(self):
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        an = self._annotator()
        loop = GLib.MainLoop()
        an._loop = loop                          # 有主循环 ⇒ 必须异步
        state = {}

        def kick():
            # ⚠ GLib 回调里抛 AssertionError **不会让用例失败**（只在 journal 留一行）——
            # 2026-10-19 反例具型 AM（把异步改回同步）就因此漏过。所以这里只记状态，断言放在
            # loop.run() 之后。
            an.action(AN.ACTION_OCR)
            state["threads"] = len(an.threads)
            state["busy"] = an.panel.busy
            state["panel_done_at_kick"] = bool(an.panel.ocr_text)
            return False

        def check():
            if an.panel.ocr_text:
                self.done.append("ui-updated")
                loop.quit()
                return False
            return True

        GLib.timeout_add(0, kick)
        GLib.timeout_add(50, check)
        GLib.timeout_add(3000, lambda: (self.done.append("timeout"), loop.quit(), False)[2])
        loop.run()
        self.assertEqual(an.panel.ocr_text, "识别到", "后台线程的结果要回填到面板")
        self.assertIn("ui-updated", self.done, "结果没回到主线程")
        self.assertFalse(an.panel.busy)
        self.assertEqual(state.get("threads"), 1, "有主循环时必须起后台线程（否则界面被冻住）")
        self.assertTrue(state.get("busy"), "跑起来时面板要显示在忙")
        self.assertFalse(state.get("panel_done_at_kick"),
                         "按下那一刻还没算完 —— 说明真的异步（同步会当场就有结果）")

    def test_failure_from_the_worker_also_lands(self):
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        an = AN.Annotator(white_surface(80, 40), screen=(80, 40), directory=self.dir,
                          clip=lambda _d: (True, "s"),
                          on_ocr=lambda _s: {"ok": False, "hint": "✗ 本机模型没起：看 12087"})
        loop = GLib.MainLoop()
        an._loop = loop
        GLib.timeout_add(0, lambda: (an.action(AN.ACTION_OCR), False)[1])

        def wait_message():
            if an.panel.message:
                loop.quit()
                return False
            return True          # 没到就继续等（返回 True = 保留这个定时器）

        GLib.timeout_add(50, wait_message)
        GLib.timeout_add(3000, lambda: (loop.quit(), False)[1])
        loop.run()
        self.assertIn("12087", an.panel.message, "后台失败也要把话术送到界面上")

    def test_cloud_can_skip_ocr_and_read_the_image(self):
        """云直读图：还没有 OCR 原文时按 F2，直接把画面发过去（本机模型没这能力）。"""
        seen = {}

        def on_translate_image(surface):
            seen["w"] = surface.get_width()
            return {"ok": True, "src": "图里的话", "text": "译好了", "backend": "cloud", "ms": 700,
                    "note": "deepseek-v4.1-flash（云·直读图）"}

        an = AN.Annotator(white_surface(80, 40), screen=(80, 40), directory=self.dir,
                          clip=lambda _d: (True, "s"))
        an.on_translate = lambda _t: {"ok": False, "hint": "不该走到这条"}
        an.on_translate_image = on_translate_image
        self.assertTrue(an.action(AN.ACTION_TRANSLATE))
        self.assertEqual(seen["w"], 80, "发过去的是当前画面")
        self.assertEqual(an.panel.dst_text, "译好了")
        self.assertIn("云", an.panel.headline())

    def test_local_without_ocr_text_asks_for_f4(self):
        an = AN.Annotator(white_surface(80, 40), screen=(80, 40), directory=self.dir,
                          clip=lambda _d: (True, "s"),
                          on_translate=lambda _t: {"ok": True, "text": "x"})
        an.on_translate_image = None             # 本机那条路：得先 OCR
        self.assertFalse(an.action(AN.ACTION_TRANSLATE))
        self.assertIn("F4", an.panel.message)

    def test_worker_never_touches_glib_from_the_other_thread(self):
        """工作线程只写一个 Python 变量，**不许**从线程里调 GLib（会把堆搞坏）。

        2026-10-19 实测：worker 里 `GLib.idle_add(...)` 回主线程，判据连跑三轮撞到两次
        `corrupted size vs. prev_size` / `realloc(): invalid next size` —— 那是**崩溃**，不是报错。
        改成信箱（主循环 50ms 轮询）之后 5/5 稳定。
        """
        import inspect
        import re
        source = inspect.getsource(AN.Annotator._run_text_action)
        worker = source[source.index("def worker():"):source.index("GLib = _gtk()[2]")]
        # 不看具体写法（`GLib.` / `GLib2.` / 别名都算），worker 里**一处 GLib 都不许出现**：
        # 反例具型 AO 第一次就是用别名 `GLib2.idle_add` 从这条判据底下溜过去的。
        for banned in ("GLib", "idle_add", "timeout_add"):
            self.assertNotIn(banned, worker, f"worker 线程里不许出现 {banned}：{worker}")
        self.assertIn("self._mailbox =", worker, "worker 应当只往信箱里写")
        drain = inspect.getsource(AN.Annotator._drain_mailbox)
        self.assertIn("GLib", inspect.getsource(AN.Annotator._run_text_action),
                      "轮询要在主线程里排")
        self.assertIn("return True", drain, "没收到结果时要继续轮询")

    def test_mailbox_is_drained_and_cleared(self):
        an = AN.Annotator(white_surface(80, 40), screen=(80, 40), directory=self.dir,
                          clip=lambda _d: (True, "s"))
        an._mailbox = ("ocr", {"ok": True, "text": "来自后台", "backend": "local", "ms": 5,
                               "src": "", "note": "", "hint": ""})
        self.assertFalse(an._drain_mailbox(), "收到之后不该再排下一次")
        self.assertEqual(an.panel.ocr_text, "来自后台")
        self.assertIsNone(an._mailbox, "信箱要清空")

    def test_switch_notifies_the_wiring_layer(self):
        seen = []
        an = AN.Annotator(white_surface(80, 40), screen=(80, 40), directory=self.dir,
                          clip=lambda _d: (True, "s"))
        an.on_switch_backend = seen.append
        an.panel.backend = "local"
        an._switch_panel_backend()
        self.assertEqual(seen, ["cloud"], "面板切后端要通知接线层翻偏好")


@unittest.skipUnless(not GUI_SKIP, f"跳过界面用例：{GUI_SKIP}")
@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo")
class TestRealWindow(unittest.TestCase):
    """真建窗（只在 CAPSPLUS_GUI=1 且不是真实 Wayland 桌面时跑）。"""

    def test_panel_becomes_visible_with_content_after_a_result(self):
        """有结果时面板必须**真的看得见**（含它的子控件）。

        2026-10-19 截屏判据抓到过：`no-show-all` 留着旗子时 `show_all()` 连子控件一起跳过 ——
        面板框在、内容是空白。这条钉住"旗子要在显示时放下"。
        """
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        an = AN.Annotator(white_surface(200, 100), screen=(200, 100),
                          directory=temp_dir("capsplus-annot-test-"),
                          clip=lambda _d: (True, "s"),
                          on_ocr=lambda _s: {"ok": True, "text": "识别到的文字",
                                             "backend": "local", "ms": 11})
        state = {}

        def build():
            an._loop = GLib.MainLoop()
            an._win = an._build()
            an._win.show_all()
            state["hidden_before"] = not an._panel_view.widget.get_visible()
            an.action(AN.ACTION_OCR)
            return False

        def read_result():
            # ⚠ 有主循环时识别是**异步**的（后台线程 + 50ms 信箱）—— 必须等结果回到主循环再读控件，
            # 否则读到的是空面板（这条判据 2026-10-19 在 xvfb 全量里就是这么红的：异步改动之前
            # 它是同步的，所以一直没露出来）。
            if not an.panel.ocr_text:
                return True                      # 还没回来，继续等
            an._refresh_panel()
            state["visible_after"] = an._panel_view.widget.get_visible()
            state["headline"] = an._panel_view._headline.get_text()
            buf = an._panel_view._text.get_buffer()
            state["text"] = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            an._win.destroy()
            loop.quit()
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(0, lambda: (build(), False)[1])
        GLib.timeout_add(30, read_result)
        GLib.timeout_add(3000, lambda: (loop.quit(), False)[1])
        loop.run()
        self.assertTrue(state["hidden_before"], "没结果时面板要先藏着")
        self.assertTrue(state["visible_after"], "有结果了面板还不显示 = 人看不到识别结果")
        self.assertIn("本地", state["headline"])
        self.assertEqual(state["text"], "识别到的文字")

    def test_window_builds_with_all_tools_and_actions(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        errors = []
        an = AN.Annotator(white_surface(200, 100), screen=(200, 100),
                          directory=temp_dir(), clip=lambda _d: (True, "s"))
        state = {}

        def build():
            try:
                an._loop = GLib.MainLoop()
                an._win = an._build()
                an._win.show_all()
                state["mapped"] = an._win.get_visible() and an.area.get_visible()
                state["buttons"] = sorted(an._buttons)
                state["colors"] = sorted(an._color_buttons)
                state["widths"] = sorted(an._width_buttons)
                state["hint"] = an._hint.get_text()
                state["panel_hidden"] = not an._panel_view.widget.get_visible()
                state["actions"] = sorted(an._action_buttons)
                an._buttons["arrow"].set_active(True)
                state["tool"] = an.tool
                an._buttons["highlight"].set_active(True)
                state["tool_hl"] = an.tool
                an._color_buttons["蓝"].set_active(True)
                state["color_blue"] = an.color
                an._on_focus(an._win, None, False)
                state["hint_unfocused"] = an._hint.get_text()
                an._win.destroy()
            except Exception as exc:                  # pragma: no cover
                errors.append(repr(exc))
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(0, lambda: (build(), False)[1])
        GLib.timeout_add(1200, lambda: (loop.quit(), False)[1])
        loop.run()
        self.assertEqual(errors, [], f"建窗抛异常：{errors}")
        self.assertTrue(state.get("mapped"), "窗口/画布没显示出来")
        self.assertEqual(state.get("buttons"),
                         ["arrow", "badge", "highlight", "mosaic", "rect", "text"],
                         "第二批的六件工具都要在工具栏上")
        self.assertEqual(state.get("colors"), ["red" if False else c[0] for c in AN.COLORS] and
                         sorted([c[0] for c in AN.COLORS]))
        self.assertEqual(state.get("widths"), sorted(AN.WIDTHS))
        for action in ("OCR", "翻译", "钉到屏幕上", "复制", "存盘", "关闭"):
            self.assertIn(action, state.get("actions"), f"工具栏缺 {action}")
        self.assertEqual(state.get("tool"), "arrow", "点工具按钮要切换当前工具")
        self.assertEqual(state.get("tool_hl"), "highlight", "荧光笔也要能选中")
        self.assertIn("蓝", state.get("color_blue") and [c[0] for c in AN.COLORS
                                                        if c[1] == state["color_blue"]] or [])
        self.assertIn("F2", state.get("hint"), "提示条要写明窗内键位")
        self.assertTrue(state.get("panel_hidden"), "没结果时面板整块藏着，不干扰标注")
        self.assertIn("没有焦点", state.get("hint_unfocused"),
                      "失焦时提示条要换成『按了不会生效』那句话")


if __name__ == "__main__":
    unittest.main()