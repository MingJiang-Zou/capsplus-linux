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
import inspect
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


def pattern_surface(width, height):
    """有花纹的底图：马赛克是把内容块状化，纯色底上它本来就什么都不改。"""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    for i in range(0, width, 8):
        for j in range(0, height, 8):
            shade = 0.25 + 0.5 * (((i // 8) + (j // 8)) % 3) / 2.0
            cr.set_source_rgb(shade, 0.35, 1.0 - shade)
            cr.rectangle(i, j, 8, 8)
            cr.fill()
    return surface


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
        """窗口坐标 → 图像坐标：**先减掉画布留白，再除以缩放**。

        （第三批之前画布没有留白，那时是"直接除以缩放"；现在截图居中、四周留白 14px，
         所以映射必须带上这个偏移 —— 否则整张图的标注会整体偏 28px。）
        """
        ix, iy, _w, _h = AN.canvas_layout(self.an.size, self.an.scale)["image"]
        self.assertEqual(self.an._image_xy(self._Ev(ix + 20, iy + 10)), (40, 20))
        self.assertEqual(self.an._image_xy(self._Ev(ix, iy)), (0, 0), "图像左上角就是原点")
        # **留白区不许落笔**：映射出来超出截图的坐标一律夹回边界（lead 第五刀）
        self.assertEqual(self.an._image_xy(self._Ev(ix - 30, iy - 30)), (0, 0), "左上留白夹到原点")
        self.assertEqual(self.an._image_xy(self._Ev(ix + 10_000, iy + 10_000)),
                         (self.an.size[0] - 1, self.an.size[1] - 1), "右下留白夹到右下角")

    def test_drag_commits_a_shape_in_image_coordinates(self):
        self.an.set_tool("rect")
        ix, iy, _w, _h = AN.canvas_layout(self.an.size, self.an.scale)["image"]
        self.an.press(self.an._image_xy(self._Ev(ix + 20, iy + 10)))     # 图像 (40,20)
        self.an.drag(self.an._image_xy(self._Ev(ix + 60, iy + 40)))      # 图像 (120,80)
        self.assertTrue(self.an.release())
        self.assertEqual(self.an.stack.shapes[0].rect, (40, 20, 80, 60),
                         "图形坐标是图像坐标，且被裁在截图内")

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

    def test_highlight_defaults_to_translucent_yellow(self):
        """荧光笔默认是**半透明黄**这一支（不跟随颜色选择）。

        口径变更（lead 看图）：跟随颜色会调出"深色底 + 深色笔"的橄榄色，压在上面的字读不出来。
        显式传色仍然尊重调用方（判据见下一条），但**不透明**永远不允许 —— 那就成了矩形填充。
        """
        shape = AN.Shape("highlight", (0, 0, 10, 10))
        self.assertEqual(shape.color, AN.HIGHLIGHT_COLOR)
        self.assertLess(shape.color[3], 1.0, "荧光笔必须半透明，否则等于拿色块把内容盖住")
        self.assertGreater(shape.color[0], shape.color[2], "黄 = 红绿高、蓝低")
        explicit = AN.Shape("highlight", (0, 0, 10, 10), color=(1, 0, 0, 1))
        self.assertEqual(explicit.color, (1, 0, 0, 1), "显式传色时尊重调用方")

    def test_highlight_blends_instead_of_covering(self):
        base = white_surface()
        out = AN.render_to_surface(base, [AN.Shape("highlight", (20, 20, 60, 40))])
        r, g, b, _a = px(out, 50, 40)
        self.assertLess(b, 0.9, "高亮区必须与原图不同（黄色会把蓝压下去）")
        self.assertGreater(g, 0.5, "半透明：底下的白还在，不该变成纯色块")

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

    def test_enter_means_copy_and_close_here(self):
        """回车 = 复制并关（明江要的 Snipaste 手感）。

        ⚠ 口径变更（第三批）：第一批"回车留给选区确认"说的是**遮罩**那条链路
        （`shot_overlay.key_action("Return")` 仍然是 `confirm`）；标注窗里回车改成"复制并关"，
        两条链路各自独立，判据分别钉，别互相带跑。小键盘回车同义。
        """
        self.assertEqual(AN.key_command("Return"), AN.ACTION_ENTER)
        self.assertEqual(AN.key_command("KP_Enter"), AN.ACTION_ENTER)
        from capsplus import shot_overlay
        self.assertEqual(shot_overlay.key_action("Return"), "confirm",
                         "遮罩那边的回车仍然是「确认选区」")

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
        """有主循环时必须**异步**：起线程、面板先显示在忙、结果回主线程再回填。

        ⚠ 这里**不用嵌套的 `loop.run()` + 定时器**：那套写法依赖"当前默认主上下文里没有别的
        source"，别的用例先跑过之后会偶发失败（实测踩到过）。改成自己泵主上下文到条件满足，
        进程内没有任何遗留状态。
        """
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        an = self._annotator()
        an._loop = GLib.MainLoop()               # 有主循环实例（**不 run**）⇒ 走异步分支
        self.assertTrue(an.action(AN.ACTION_OCR), "动作本身该立刻返回 True")
        self.assertTrue(an.threads, "应当起了后台线程（否则界面被冻住）")
        self.assertTrue(an.panel.busy, "跑起来时面板要显示在忙")
        self.assertFalse(an.panel.ocr_text, "按下那一刻还没算完 —— 说明真的异步")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not an.panel.ocr_text:
            GLib.MainContext.default().iteration(False)     # 泵主上下文：跑信箱轮询
            time.sleep(0.01)
        self.assertEqual(an.panel.ocr_text, "识别到", "后台线程的结果要回填到面板")
        self.assertFalse(an.panel.busy)

    def test_failure_from_the_worker_also_lands(self):
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        an = AN.Annotator(white_surface(80, 40), screen=(80, 40), directory=self.dir,
                          clip=lambda _d: (True, "s"),
                          on_ocr=lambda _s: {"ok": False, "hint": "✗ 本机模型没起：看 12087"})
        an._loop = GLib.MainLoop()
        an.action(AN.ACTION_OCR)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not an.panel.message:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
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


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真把每种工具画一遍")
class TestEveryToolActuallyDraws(unittest.TestCase):
    """**每种工具都要真的画得出东西**（这条是补漏：椭圆渲染里少 import math，运行期直接崩，
    而当时的判据只画过矩形/箭头/序号 —— 直到渲染窗口图时才炸出来）。"""

    def _ink(self, surface, base):
        surface.flush()
        data = bytes(surface.get_data())
        base_data = bytes(base.get_data())
        stride = surface.get_stride()
        changed = 0
        for y in range(surface.get_height()):
            for x in range(surface.get_width()):
                off = y * stride + x * 4
                if (data[off], data[off + 1], data[off + 2]) != (base_data[off], base_data[off + 1],
                                                                 base_data[off + 2]):
                    changed += 1
        return changed

    def test_every_tool_changes_pixels(self):
        # 底图要**有花纹**：马赛克是把内容"块状化"，纯白底上它本来就什么都不改（那不是 bug）
        base = pattern_surface(160, 120)
        for tool in ("rect", "ellipse", "arrow", "line", "highlight", "mosaic", "badge", "text"):
            with self.subTest(tool=tool):
                shape = AN.Shape(tool, (30, 25, 80, 60), text="7", color=(0.9, 0.1, 0.1, 1.0),
                                 width=3)
                out = AN.render_to_surface(base, [shape])
                self.assertGreater(self._ink(out, base), 30, f"{tool} 什么都没画出来")

    def test_freehand_renders_its_points(self):
        base = white_surface(160, 120)
        shape = AN.Shape("pen", (20, 20, 100, 60), color=(0.1, 0.2, 0.9, 1.0), width=3,
                         points=[(20, 80), (60, 30), (100, 70), (140, 40)])
        out = AN.render_to_surface(base, [shape])
        self.assertGreater(self._ink(out, base), 100, "自由笔没画线")

    def test_text_and_badge_are_readable_shapes(self):
        """文字与序号是"看得见"的：画出来的像素量不该只有一点点（防止字体没设置好画成空白）。"""
        base = white_surface(200, 120)
        text = AN.Shape("text", (20, 20, 1, 1), text="capSplus 中文", color=(0, 0, 0, 1.0))
        badge = AN.Shape("badge", (20, 60, 28, 28), text="3", color=(0.9, 0.1, 0.1, 1.0))
        out = AN.render_to_surface(base, [text, badge])
        self.assertGreater(self._ink(out, base), 200)


@unittest.skipUnless(HAVE_CAIRO, "这次的口径判据要造一张图")
class TestNewToolbarAndEditing(unittest.TestCase):
    """第三批（UI 重做）的口径判据：工具条分组、放大镜、选中/手柄、画布、工具条不压图。"""

    def setUp(self):
        self.dir = temp_dir("capsplus-ui-test-")

    def _annotator(self):
        return AN.Annotator(white_surface(200, 120), screen=(400, 300), directory=self.dir,
                            clip=lambda _d: (True, "s"))

    # ---- 工具条的形状（纯逻辑）----
    def test_every_tool_is_in_exactly_one_group(self):
        flat = [tool for group in AN.TOOL_GROUPS for tool in group]
        self.assertEqual(sorted(flat), sorted(AN.TOOLS), "分组要覆盖全部工具")
        self.assertEqual(len(flat), len(set(flat)), "同一个工具出现在两个组里了")

    def test_action_groups_are_split_left_and_right(self):
        """撤销重做在最左、复制/存盘/贴图/关闭在最右，且**没有重复**（明江："别再顶栏底栏各一套"）。"""
        self.assertEqual(tuple(AN.EDIT_ACTIONS), (AN.ACTION_UNDO, AN.ACTION_REDO))
        self.assertEqual(tuple(AN.ACTION_GROUP),
                         (AN.ACTION_COPY, AN.ACTION_SAVE, AN.ACTION_PIN, AN.ACTION_CLOSE))
        all_actions = list(AN.EDIT_ACTIONS) + list(AN.ACTION_GROUP)
        self.assertEqual(len(all_actions), len(set(all_actions)), "动作有重复")
        self.assertEqual(AN.ACTION_GROUP[0], AN.ACTION_COPY, "复制要在动作区首位（主按钮）")

    def test_context_menu_has_the_four_actions(self):
        actions = [action for _label, action in AN.context_menu_items()]
        labels = [label for label, _action in AN.context_menu_items()]
        self.assertEqual(actions, [AN.ACTION_COPY, AN.ACTION_SAVE, AN.ACTION_PIN, AN.ACTION_CLOSE])
        self.assertEqual(len(set(actions)), len(actions), "右键菜单里有重复动作")
        self.assertTrue(all(labels))

    def test_bar_never_covers_the_image_when_there_is_room(self):
        """有留白可用时工具条**必须**在图像外（明江："贴近选区但不遮挡"）。"""
        screen = (1920, 1080)
        for image in ((400, 200), (1920, 1080), (4800, 1080), (120, 90)):
            with self.subTest(image=image):
                plan = AN.window_plan(image, screen, bar_h=40)
                bar = AN.bar_layout(plan["canvas"], (760, 40), plan["window"])
                bar["w"], bar["h"] = 760, 40
                self.assertFalse(AN.bar_overlaps_image(bar, plan["canvas"]),
                                 f"{image}：工具条压到图上了 {bar}")

    def test_bar_flips_above_when_the_bottom_has_no_room(self):
        roomy = AN.canvas_layout((400, 200), 1.0, reserve_top=60)
        above = AN.bar_layout(roomy, (760, 40), roomy["size"])
        self.assertTrue(above["flipped"], f"上方有地方就该翻上去：{above}")
        self.assertFalse(above["overlaps"])
        tight = AN.canvas_layout((400, 200), 1.0)          # 上下都没预留
        no_room = AN.bar_layout(tight, (760, 40), tight["size"])
        self.assertTrue(no_room["overlaps"],
                        "实在没地方时就老实承认会压到（判据据此盯住「只在没办法时」）")

    def test_toolbar_buttons_are_images_with_tooltips(self):
        import inspect
        source = inspect.getsource(AN.Annotator._build_bar)
        self.assertIn("set_image", source, "工具按钮要画成图标")
        self.assertIn("set_tooltip_text", source, "图标要靠悬浮提示才认得出")

    # ---- 选中 / 手柄 / 拖动（纯逻辑）----
    def test_eight_handles_around_the_bounds(self):
        bounds = (100, 200, 60, 40)
        handles = AN.handles_for(bounds)
        self.assertEqual(sorted(handles), sorted(AN.HANDLES), "必须有 8 个手柄")
        self.assertEqual(handles["nw"], (100, 200))
        self.assertEqual(handles["se"], (160, 240))
        self.assertEqual(handles["n"], (130, 200))

    def test_handle_hit_testing(self):
        bounds = (100, 200, 60, 40)
        self.assertEqual(AN.handle_at(bounds, (100, 200)), "nw")
        self.assertEqual(AN.handle_at(bounds, (161, 241)), "se")
        self.assertEqual(AN.handle_at(bounds, (130, 220)), "", "图形中间不是手柄")

    def test_selection_prefers_the_topmost_object(self):
        an = self._annotator()
        an.stack.add(AN.Shape("rect", (10, 10, 60, 60)))
        an.stack.add(AN.Shape("highlight", (20, 20, 60, 60)))
        self.assertEqual(an.select_at((20, 30)), 1, "重叠时选中最后画的那个")
        self.assertIs(an.selected, an.stack.shapes[1])
        self.assertEqual(an.select_at((500, 500)), -1, "点空白要取消选中")
        self.assertIsNone(an.selected)

    def test_dragging_an_object_moves_it_and_is_undoable(self):
        an = self._annotator()
        an.stack.add(AN.Shape("rect", (10, 10, 60, 60)))
        # 点在**边线上但不是手柄**（手柄是四角+四边中点，都留给缩放）
        an.press((30, 10))
        self.assertEqual(an._drag_mode, "move")
        an.drag((60, 40))
        self.assertTrue(an.release())
        self.assertEqual(an.stack.shapes[0].rect, (40, 40, 60, 60), "拖动要真的挪了它")
        self.assertTrue(an.undo_edit(), "拖动要可撤销")
        self.assertEqual(an.stack.shapes[0].rect, (10, 10, 60, 60))

    def test_resizing_from_a_handle_changes_only_that_edge(self):
        an = self._annotator()
        an.stack.add(AN.Shape("rect", (10, 10, 60, 60)))
        an.select_at((10, 40))                       # 先选中（选中之后手柄才生效）
        an.press((70, 70))                           # (70,70) 正是右下角手柄
        self.assertEqual(an._drag_mode, "resize:se")
        an.drag((100, 100))
        an.release()
        self.assertEqual(an.stack.shapes[0].rect, (10, 10, 90, 90), "只有右下角动")

    def test_arrow_nudge_is_one_pixel_and_ten_with_shift(self):
        an = self._annotator()
        an.stack.add(AN.Shape("rect", (10, 10, 60, 60)))
        an.select_at((10, 40))
        an.nudge_selected(*AN.nudge_delta("Right"))
        self.assertEqual(an.stack.shapes[0].rect[0], 11, "方向键 = 1px")
        an.nudge_selected(*AN.nudge_delta("Down", True))
        self.assertEqual(an.stack.shapes[0].rect[1], 20, "Shift = 10px")

    def test_nudge_without_a_selection_says_what_to_do(self):
        an = self._annotator()
        self.assertFalse(an.nudge_selected(1, 0))
        self.assertIn("先点一下", an.message, "没选中要提示怎么做，不是默默不动")

    def test_eraser_deletes_what_it_clicks(self):
        an = self._annotator()
        an.stack.add(AN.Shape("rect", (10, 10, 60, 60)))
        an.set_tool("eraser")
        self.assertEqual(an.click((40, 40)), "erased", "橡皮点盒内就该删（够好点）")
        self.assertEqual(len(an.stack.shapes), 0)
        self.assertTrue(an.undo_edit(), "删除可撤销")

    def test_freehand_stores_points(self):
        an = self._annotator()
        an.set_tool("pen")
        an.press((10, 10))
        for pt in ((20, 20), (30, 25), (40, 40)):
            an.drag(pt)
        self.assertTrue(an.release())
        shape = an.stack.shapes[0]
        self.assertEqual(shape.kind, "pen")
        self.assertGreaterEqual(len(shape.points), 4, "自由笔要把点都留着")
        self.assertEqual(shape.rect[:2], (10, 10), "外接矩形跟着点集走")

    def test_selection_is_drawn_with_handles_and_hover_outline(self):
        """选中/悬停必须在**画出来的像素**上看得见（明江："没有描边和手柄，看不出可编辑"）。"""
        base = white_surface(200, 120)
        shape = AN.Shape("rect", (40, 30, 60, 40), color=(0.9, 0.1, 0.1, 1.0))
        plain = AN.render_to_surface(base, [shape])
        selected = AN.render_to_surface(base, [shape], selected=shape)
        hovered = AN.render_to_surface(base, [shape], hover=shape)

        def pixels(surface):
            surface.flush()
            data = bytes(surface.get_data())
            stride = surface.get_stride()
            return {(x, y): (data[y * stride + x * 4 + 2], data[y * stride + x * 4 + 1],
                             data[y * stride + x * 4])
                    for y in range(surface.get_height()) for x in range(surface.get_width())}

        def diff(a, b):
            """两张图差了多少像素 —— 比「数非白像素」稳：白底上画白虚线，数非白是数不出来的。"""
            pa, pb = pixels(a), pixels(b)
            return sum(1 for key in pa if pa[key] != pb[key])

        self.assertGreater(diff(selected, plain), 200, "选中态没画出手柄/描边")
        # hover 是**双色虚线**（深底 + 白线）：白底截图上也不能"等于没画"（判据抓到过一次）
        self.assertGreater(diff(hovered, plain), 50, "hover 没有虚线反馈")
        self.assertGreater(diff(selected, hovered), 50, "hover 与选中的画法该不一样")

    def test_canvas_centres_the_image_with_a_margin(self):
        base = white_surface(100, 50)
        canvas = AN.canvas_layout((100, 50), 1.0)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, *canvas["size"])
        cr = cairo.Context(surface)
        AN.draw_canvas(cr, canvas, base, [])
        surface.flush()
        data = bytes(surface.get_data())
        stride = surface.get_stride()

        def px(x, y):
            off = y * stride + x * 4
            return (data[off + 2], data[off + 1], data[off])   # ARGB32 是预乘 BGRA

        ix, iy, iw, ih = canvas["image"]
        bg = tuple(int(c * 255) for c in AN.CANVAS_BG)
        self.assertGreater(ix, 0, "左边要有留白")
        self.assertEqual(px(2, 2), bg, "留白是中性底色")
        self.assertEqual(px(ix + 5, iy + 5), (255, 255, 255), "截图要画在中间")
        self.assertEqual(px(ix + iw + 6, iy + ih + 6), bg, "右下也是底色")


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真渲染/真造图")
class TestClippingToTheScreenshotRect(unittest.TestCase):
    """**落笔裁到截图范围内**（lead 第五刀）：留白区不许落笔，导出只导出截图矩形。

    明江看图时说"1 号圆圈与黄箭头跑到白色截图框外了" —— 一半是渲染脚本的 demo 画得不对，
    一半是产品行为没定死：画布留白区当时真的能落笔。
    """

    def setUp(self):
        self.dir = temp_dir("capsplus-clip-test-")

    def _annotator(self):
        return AN.Annotator(white_surface(200, 100), screen=(400, 300), directory=self.dir,
                            clip=lambda _d: (True, "s"))

    def test_coords_are_clamped_into_the_image(self):
        self.assertEqual(AN.clamp_to_image((-40, -40), (200, 100)), (0, 0))
        self.assertEqual(AN.clamp_to_image((9_999, 9_999), (200, 100)), (199, 99))
        self.assertEqual(AN.clamp_to_image((50, 50), (200, 100)), (50, 50))

    def test_rect_is_clipped_preserving_size(self):
        self.assertEqual(AN.clamp_rect_to_image((190, 95, 60, 60), (200, 100)), (140, 40, 60, 60),
                         "整块往回收，别把宽高改掉")

    def test_a_stroke_started_outside_lands_inside(self):
        """从留白区往外拖：落地的图形必须**整块在截图内**（不是部分画到框外）。"""
        an = self._annotator()
        an.set_tool("rect")
        an.press((-100, -100))
        an.drag((500, 400))
        self.assertTrue(an.release())
        x, y, w, h = an.stack.shapes[0].rect
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, 200)
        self.assertLessEqual(y + h, 100)

    def test_dragging_and_nudging_stay_inside(self):
        an = self._annotator()
        an.stack.add(AN.Shape("rect", (10, 10, 60, 40)))
        an.select_at((10, 30))
        an.move_selected(1000, 1000)
        rect = an.stack.shapes[0].rect
        self.assertLessEqual(rect[0] + rect[2], 200, "拖出截图要夹住")
        self.assertLessEqual(rect[1] + rect[3], 100)

    def test_freehand_points_are_clipped_too(self):
        an = self._annotator()
        an.set_tool("pen")
        an.press((10, 10))
        for pt in ((180, 90), (400, 300), (50, 50)):
            an.drag(pt)
        an.release()
        for (px_, py_) in an.stack.shapes[0].points:
            self.assertTrue(0 <= px_ < 200 and 0 <= py_ < 100, f"点跑到图外了：{(px_, py_)}")

    def test_export_is_exactly_the_screenshot_rect(self):
        """导出/复制**只导出截图矩形**：尺寸等于底图，留白区不会被带进去。"""
        base = white_surface(200, 100)
        shapes = [AN.Shape("rect", (190, 90, 60, 60)), AN.Shape("text", (195, 95, 1, 1), text="x")]
        out = AN.render_to_surface(base, shapes)
        self.assertEqual((out.get_width(), out.get_height()), (200, 100))
        canvas = AN.canvas_layout((200, 100), 1.0)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, *canvas["size"])
        cr = cairo.Context(surface)
        AN.draw_canvas(cr, canvas, base, shapes)
        surface.flush()
        data = bytes(surface.get_data())
        stride = surface.get_stride()
        ix, iy, iw, ih = canvas["image"]
        off = (iy + ih + 4) * stride + (ix + iw + 4) * 4          # 右下留白区
        self.assertEqual(tuple(data[off + i] for i in (2, 1, 0)),
                         tuple(int(c * 255) for c in AN.CANVAS_BG),
                         "画到图外的标注把留白区也污染了 ⇒ 说明绘制没有裁到截图矩形")

    def test_highlight_is_readable_on_light_and_dark_bases(self):
        """荧光笔要**在浅底与深底上都读得出**（lead 第三刀）。

        反例（本判据抓住过）：深底 + 深色笔 ⇒ 橄榄色，压在上面的字读不出来。
        """
        from capsplus import shot_style as S

        def measure(base_colour):
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 60, 40)
            cr = cairo.Context(surface)
            cr.set_source_rgb(*base_colour)
            cr.paint()
            base = cairo.ImageSurface.create_from_png(_png_of(surface))
            out = AN.render_to_surface(base, [AN.Shape("highlight", (5, 5, 50, 30))])
            out.flush()
            data = bytes(out.get_data())
            stride = out.get_stride()
            off = 20 * stride + 30 * 4
            bar = (data[off + 2] / 255, data[off + 1] / 255, data[off] / 255)
            distance = sum((bar[i] - base_colour[i]) ** 2 for i in range(3)) ** 0.5
            return S.luminance(bar), distance

        # 口径：高亮条**自己永远是亮的**（≥0.55，像真荧光笔那样）+ 与底色有明显色差（≥0.25）。
        # 只比"亮度差"是不够的：半透明黄压在白底上亮度几乎不变（黄本来就亮），但它明显是黄的。
        for name, base_colour in (("浅底", (1.0, 1.0, 1.0)), ("深底", (0.13, 0.14, 0.17))):
            with self.subTest(base=name):
                lum, distance = measure(base_colour)
                self.assertGreaterEqual(lum, 0.55, f"{name}上的高亮条太暗（{lum:.2f}）—— 压在上面的字读不出来")
                self.assertGreaterEqual(distance, 0.25, f"{name}上看不出高亮（色差 {distance:.2f}）")

    def _annotator_class(self):
        return AN.Annotator


def _png_of(surface):
    import tempfile
    path = tempfile.mktemp(suffix=".png", prefix="capsplus-lum-")
    surface.write_to_png(path)
    return path


@unittest.skipUnless(not GUI_SKIP, f"跳过界面用例：{GUI_SKIP}")
class TestDrawerAndStatusPresentation(unittest.TestCase):
    """抽屉高度跟内容走（lead 第二刀）+ 状态栏分两档（lead 第四刀）。"""

    def _build(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        an = AN.Annotator(white_surface(300, 200), screen=(800, 600),
                          directory=temp_dir("capsplus-drawer-test-"), clip=lambda _d: (True, "s"))
        state = {}

        def build():
            an._loop = GLib.MainLoop()
            an._win = an._build()
            an._win.show_all()
            an.panel.set_ocr("两行文字\n就这样", "local", 500, "rapidocr")
            an._refresh_panel()
            an._win.show_all()
            _min_h, natural_small = an._panel_view.widget.get_preferred_height()
            state["small_h"] = natural_small
            state["window_h"] = an._win.get_preferred_height()[1]
            an.panel.set_ocr("\n".join(f"第 {i} 行" for i in range(40)), "local", 900, "rapidocr")
            an._refresh_panel()
            an._win.show_all()
            _min_h2, natural_big = an._panel_view.widget.get_preferred_height()
            state["big_h"] = natural_big
            state["status_main"] = an._status.get_text()
            state["status_more"] = an._status_more.get_text()
            state["main_class"] = an._status.get_style_context().has_class("cp-status-main")
            state["more_class"] = an._status_more.get_style_context().has_class("cp-status-more")
            an._win.destroy()
            loop.quit()                      # 别等兜底定时器（不然每条用例白等 3 秒）
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(0, lambda: (build(), False)[1])
        fired = []
        fallback = GLib.timeout_add(3000, lambda: (fired.append(1), loop.quit(), False)[2])
        loop.run()
        GLib.source_remove(fallback)
        return state

    def test_drawer_height_follows_the_content(self):
        """两行文字时抽屉**不许留一大片空白**（不超过窗口高度的 35%）；内容多了给上限 + 滚动。"""
        state = self._build()
        window_h = state.get("window_h") or 1
        self.assertLess(state["small_h"], window_h * 0.35,
                        f"两行文字的抽屉高 {state['small_h']}px / 窗口 {window_h}px —— 太空了")
        self.assertLessEqual(state["big_h"], 260, "内容多了要有上限（超出滚动），不能无限长")

    def test_status_bar_has_two_tiers(self):
        """常用键一档、其余收成次要那档（lead："太密、层次没出来"）。"""
        state = self._build()
        main, more = state["status_main"], state["status_more"]
        for key in ("Enter", "Esc", "Ctrl+C", "Ctrl+S", "Ctrl+Z"):
            self.assertIn(key, main, f"常用键 {key} 该在主行")
        for key in ("F2", "F3", "F4", "Del", "方向键"):
            self.assertIn(key, more, f"次要键 {key} 该在次要行")
            self.assertNotIn(key, main, f"{key} 不该同时挤在主行")
        self.assertTrue(state["main_class"] and state["more_class"], "两档要用各自的样式类")


@unittest.skipUnless(not GUI_SKIP, f"跳过界面用例：{GUI_SKIP}")
class TestActiveStateIsAppliedInARealWindow(unittest.TestCase):
    """选中态在**真窗口里真的生效**：工具/颜色/线宽三者都必须同时有**底色 + 描边**。

    为什么非要在真窗口里量：源码层面的"取了同一个类"不等于"样式画上去了"。实测踩过的坑是
    **CSS 顺序** —— `.cp-swatch`/`.cp-dot` 里写了 `background-color: transparent`，同权重时后写的赢，
    `.cp-active` 排在前面就会被盖掉（渲染图上看着像"只有描边没有底色"）。所以这里量**解析后的样式**：
    `get_background_color().alpha > 0` 且 `get_border_color().alpha > 0`。
    """

    def test_all_three_kinds_get_a_tint_and_an_outline(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib, Gtk
        an = AN.Annotator(white_surface(200, 100), screen=(400, 300),
                          directory=temp_dir(), clip=lambda _d: (True, "s"))
        state = {}

        def build():
            an._loop = GLib.MainLoop()
            an._win = an._build()
            an._win.show_all()
            for label, widget in (("tool", an._buttons["rect"]), ("swatch", an._swatches["红"]),
                                  ("dot", an._widths[3])):
                ctx = widget.get_style_context()
                state[label] = (ctx.get_background_color(Gtk.StateFlags.NORMAL).alpha,
                                ctx.get_border_color(Gtk.StateFlags.NORMAL).alpha)
            an._win.destroy()
            loop.quit()
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(0, lambda: (build(), False)[1])
        fired = []
        fallback = GLib.timeout_add(2000, lambda: (fired.append(1), loop.quit(), False)[2])
        loop.run()
        if not fired:
            try:
                GLib.source_remove(fallback)
            except Exception:
                pass
        self.assertEqual(sorted(state), ["dot", "swatch", "tool"])
        for label, (bg_alpha, border_alpha) in state.items():
            self.assertGreater(bg_alpha, 0.1, f"{label} 的选中态没有底色（CSS 顺序被盖掉了？）")
            self.assertGreater(border_alpha, 0.1, f"{label} 的选中态没有描边")


@unittest.skipUnless(not GUI_SKIP, f"跳过界面用例：{GUI_SKIP}")
class TestIconColourIsReadable(unittest.TestCase):
    """图标颜色必须与**它自己的底色**拉得开 —— 否则就是个"看不出来的按钮"。

    2026-10-19 真事：抽屉右上角的复制按钮在主按钮底色上画白图标，渲染出来是个**空方块**
    （窗口图上一眼看到）。当时赌的是 `@theme_fg_color` / `get_color(SELECTED)` 两个主题变量，
    某些主题下前景色与底色同亮 ⇒ 白压白。现在改成"量背景亮度再反着挑"，这条判据盯住结果。
    """

    def test_primary_and_drawer_icons_contrast_with_their_background(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        from capsplus import shot_style
        from capsplus.shot_panel import PanelView
        an = AN.Annotator(white_surface(200, 100), screen=(400, 300),
                          directory=temp_dir(), clip=lambda _d: (True, "s"))
        state = {}

        def build():
            an._loop = GLib.MainLoop()
            an._win = an._build()
            an._win.show_all()
            view = PanelView()
            drawer = view.build()
            an._win.add(drawer)          # 让抽屉也拿到主题上下文
            drawer.show_all()
            pairs = {}
            for name, btn in [("toolbar-copy", an._actions["copy"]),
                              ("toolbar-close", an._actions["close"]),
                              ("tool-save", an._actions["save"]),
                              ("drawer-copy", view._copy)]:
                colour = shot_style.readable_colour(btn)
                pairs[name] = (shot_style.luminance(colour),
                               shot_style.background_luminance(btn))
            state["pairs"] = pairs
            an._win.destroy()
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(0, lambda: (build(), False)[1])
        fired = []
        fallback = GLib.timeout_add(2000, lambda: (fired.append(1), loop.quit(), False)[2])
        loop.run()
        if not fired:                         # 只在兜底**没**触发时移除
            try:
                GLib.source_remove(fallback)
            except Exception:
                pass
        pairs = state.get("pairs") or {}
        self.assertTrue(pairs)
        for name, (icon_lum, bg_lum) in pairs.items():
            self.assertGreater(abs(icon_lum - bg_lum), 0.25,
                               f"{name} 的图标颜色与底色差太小（{icon_lum:.2f} vs {bg_lum:.2f}）"
                               f" —— 那就是「看不见的按钮」")


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
        fired = []
        fallback = GLib.timeout_add(3000, lambda: (fired.append(1), loop.quit(), False)[2])
        loop.run()
        if not fired:                         # 只在兜底**没**触发时移除
            try:
                GLib.source_remove(fallback)
            except Exception:
                pass
        self.assertTrue(state["hidden_before"], "没结果时面板要先藏着")
        self.assertTrue(state["visible_after"], "有结果了面板还不显示 = 人看不到识别结果")
        self.assertIn("本地", state["headline"])
        self.assertEqual(state["text"], "识别到的文字")

    def test_window_has_the_new_toolbar_layout(self):
        """真窗口里核对新布局（明江："太丑、易用性有待提高" 那一批的**结构**判据）。

        ⚠ 两条踩过的坑，别再犯：
          1. **所有碰控件的检查必须在 `destroy()` 之前做完** —— 控件销毁后再 `get_image()` /
             `get_style_context()` 是**段错误**（不是抛异常），判据整轮崩掉过；
          2. 别读按钮里那个 surface 的像素（GTK 持有的内存，`get_data()` 同样会崩）——
             用**同样的画法现画一个**来数墨，等价且安全。
        """
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        from capsplus import shot_style
        from capsplus.shot_icons import render_icon
        an = AN.Annotator(white_surface(200, 100), screen=(400, 300),
                          directory=temp_dir(), clip=lambda _d: (True, "s"))
        state = {}

        def ink_of(name):
            surface = render_icon(name, AN.TOOL_BTN - 8,
                                  shot_style.readable_colour(an._actions[name]))
            if surface is None:
                return 0
            surface.flush()
            data = bytes(surface.get_data())
            stride = surface.get_stride()
            return sum(1 for y in range(surface.get_height()) for x in range(surface.get_width())
                       if data[y * stride + x * 4 + 3] > 40)

        def build():
            an._loop = GLib.MainLoop()
            an._win = an._build()
            an._win.show_all()

            def ctx(widget):
                return widget.get_style_context()

            state["mapped"] = an._win.get_visible() and an.area.get_visible()
            state["tools"] = sorted(an._buttons)
            state["tool_images"] = all(b.get_image() for b in an._buttons.values())
            state["tool_tips"] = all(b.get_tooltip_text() for b in an._buttons.values())
            state["active_tool_class"] = ctx(an._buttons["rect"]).has_class(shot_style.ACTIVE_CLASS)
            state["swatches"] = sorted(an._swatches)
            state["swatch_class"] = ctx(an._swatches["红"]).has_class("cp-swatch")
            state["swatch_active"] = ctx(an._swatches["红"]).has_class(shot_style.ACTIVE_CLASS)
            state["widths"] = sorted(an._widths)
            state["dot_class"] = ctx(an._widths[3]).has_class("cp-dot")
            state["width_active"] = ctx(an._widths[3]).has_class(shot_style.ACTIVE_CLASS)
            state["actions"] = sorted(an._actions)
            state["primary_class"] = ctx(an._actions["copy"]).has_class("cp-btn-primary")
            state["status"] = an._status.get_text()
            state["status_more"] = an._status_more.get_text()
            state["bar_classes"] = ctx(an._bar).has_class("cp-bar")
            state["drawer_hidden"] = not an._panel_view.widget.get_visible()
            state["drawer_not_in_bar"] = an._panel_view.widget.get_parent() is not an._bar
            state["overlaps"] = AN.bar_overlaps_image(
                {"x": an._bar_layout["x"], "y": an._bar_layout["y"],
                 "w": an._bar_width(), "h": 40}, an.plan["canvas"])
            # 与"橙色方框是不是空方块"两头对上：按钮有图标 + 图标像素上有墨
            state["missing_icons"] = [n for n, b in an._actions.items() if not b.get_image()]
            state["blank_icons"] = {n: ink_of(n) for n in ("copy", "save", "pin", "close",
                                                          "undo", "redo")}
            an._buttons["arrow"].set_active(True)
            state["tool"] = an.tool
            state["arrow_active"] = ctx(an._buttons["arrow"]).has_class(shot_style.ACTIVE_CLASS)
            state["rect_active"] = ctx(an._buttons["rect"]).has_class(shot_style.ACTIVE_CLASS)
            an._buttons["highlight"].set_active(True)
            state["tool_hl"] = an.tool
            an._swatches["蓝"].set_active(True)
            state["color"] = an.color
            an._widths[6].set_active(True)
            state["width"] = an.width
            an.stack.add(AN.Shape("rect", (10, 10, 60, 40)))
            an.select_at((10, 30))
            an.refresh_status()
            state["status_selected"] = an._status.get_text()
            an._win.destroy()
            loop.quit()
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(0, lambda: (build(), False)[1])
        fired = []
        fallback = GLib.timeout_add(2000, lambda: (fired.append(1), loop.quit(), False)[2])
        loop.run()
        if not fired:                          # 只在兜底**没**触发时移除（已触发过再移除会报警告）
            try:
                GLib.source_remove(fallback)
            except Exception:
                pass

        self.assertTrue(state.get("mapped"), "窗口/画布没显示出来")
        self.assertEqual(state["tools"], sorted(AN.TOOLS), "工具条上的工具必须齐全且**无重复**")
        self.assertTrue(state["tool_images"], "每个工具都该是**图标**（不是文字按钮）")
        self.assertTrue(state["tool_tips"], "每个工具都该有悬浮提示")
        self.assertTrue(state["active_tool_class"], "当前工具没有高亮（cairo 里那点灰不算高亮）")
        self.assertEqual(len(state["swatches"]), len(AN.COLORS))
        self.assertGreaterEqual(len(state["swatches"]), 6, "颜色至少 6 个")
        self.assertTrue(state["swatch_class"] and state["swatch_active"], "颜色圆点缺样式/选中描边")
        self.assertEqual(state["widths"], sorted(AN.WIDTHS), "线宽三档")
        self.assertTrue(state["dot_class"] and state["width_active"],
                        "线宽圆点缺样式，或选中态没走同一个 cp-active")
        self.assertEqual(state["actions"], sorted(AN.ACTION_GROUP + AN.EDIT_ACTIONS),
                         "动作区必须含撤销重做 + 复制/存盘/贴图/关闭，且各一次")
        self.assertTrue(state["primary_class"], "复制要是主按钮（明江要的「主次分明」）")
        self.assertEqual(state["missing_icons"], [], "这些动作按钮没有图标")
        blank = {n: ink for n, ink in state["blank_icons"].items() if ink < 20}
        self.assertEqual(blank, {}, f"这些动作按钮的图标是空的（渲染出来是个空方块）：{blank}")
        self.assertIn("Enter", state["status"], "快捷键提示要在状态栏里")
        self.assertIn("未选中", state["status"], "没选中时状态栏要说明白")
        self.assertIn("已选", state["status_selected"])
        self.assertIn("矩形", state["status_selected"], "状态栏要写清选中了什么")
        self.assertIn("F2", state["status_more"], "次要那档要收着其余的键")
        self.assertTrue(state["bar_classes"], "工具条没上样式类")
        self.assertTrue(state["drawer_hidden"], "没结果时抽屉先藏着")
        self.assertTrue(state["drawer_not_in_bar"], "结果抽屉**不许**和工具条挤在同一行")
        self.assertFalse(state["overlaps"], f"工具条压到截图上了：{an._bar_layout}")
        self.assertEqual(state["tool"], "arrow")
        self.assertTrue(state["arrow_active"] and not state["rect_active"],
                        "换工具后高亮要跟着走（旧的要摘掉）")
        self.assertEqual(state["tool_hl"], "highlight")
        self.assertEqual(state["color"], AN.COLORS[3][1], "颜色选择没生效")
        self.assertEqual(state["width"], 6, "线宽选择没生效")


if __name__ == "__main__":
    unittest.main()


class TestDaemonStyleSubprocesses(unittest.TestCase):
    """**守护型命令不许用 `capture_output` 等管道 EOF**（2026-10-19 真机事故）。

    明江："我多按几次 Ctrl+C 程序就会异常"。根因：`wl-copy` 会 fork 一个长期存活的"留守者"，
    它 **stderr 原样继承** ⇒ `capture_output=True` 等于等一条永不 EOF 的管道 ⇒ 每次复制卡满
    5 秒 timeout（主线程被冻住），连按几次就堆成"程序异常"。

    判据用**真的**守护型形状（`sh -c 'sleep N & exit 0'`：父进程立刻退、留守者抱着管道），
    不靠"函数被调用了"这种假绿。
    """

    def test_default_runner_returns_without_waiting_for_the_lingering_child(self):
        daemon_argv = ["sh", "-c", "sleep 3 & exit 0"]
        t0 = time.monotonic()
        proc = AN._default_clipboard_runner(daemon_argv, b"x")
        elapsed = time.monotonic() - t0
        self.assertEqual(proc.returncode, 0)
        self.assertLess(elapsed, 1.0,
                        f"默认 runner 等了 {elapsed:.2f}s —— 它又在等管道 EOF 了")

    def test_capture_output_would_block(self):
        """把反例摆在判据里：接管道的那版**确实**会被留守者拖住（说明上面那条测的是真东西）。"""
        t0 = time.monotonic()
        subprocess.run(["sh", "-c", "sleep 1 & exit 0"], capture_output=True, timeout=10)
        bad = time.monotonic() - t0
        t0 = time.monotonic()
        subprocess.run(["sh", "-c", "sleep 1 & exit 0"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
        good = time.monotonic() - t0
        self.assertGreater(bad, 0.8, f"反例没被拖住（{bad:.2f}s）—— 判据失去意义")
        self.assertLess(good, 0.5, f"DEVNULL 那版也慢了（{good:.2f}s）")

    def test_copy_to_clipboard_is_fast_with_a_daemon_shaped_command(self):
        """端到端：`copy_to_clipboard` 在守护型命令上必须**远小于 timeout** 地返回 ok=True。"""
        t0 = time.monotonic()
        ok, why = AN.copy_to_clipboard(b"hello", runner=AN._default_clipboard_runner,
                                       which=lambda _n: "/usr/bin/wl-copy")
        elapsed = time.monotonic() - t0
        self.assertTrue(ok, why)
        self.assertLess(elapsed, 1.0, f"复制用了 {elapsed:.2f}s")

    def test_default_runner_does_not_use_capture_output(self):
        """静态：默认 runner 的**代码**里不许出现 `capture_output`（反例一改就红）。

        ⚠ 只看代码，不看文档字符串 —— 那段注释里正写着 `capture_output=True` 这个反面教材，
        按文本扫会误伤（第一版就误伤了，判据自己红）。
        """
        node = ast.parse(inspect.getsource(AN._default_clipboard_runner)).body[0]
        code = ast.dump(ast.Module(body=[n for n in node.body
                                         if not (isinstance(n, ast.Expr)
                                                 and isinstance(n.value, ast.Constant)
                                                 and isinstance(n.value.value, str))],
                                   type_ignores=[]))
        self.assertNotIn("capture_output", code)
        self.assertIn("DEVNULL", code)

    def test_no_daemon_command_waits_on_a_pipe_anywhere(self):
        """全仓扫：**提 wl-copy 的调用点不许出现 `capture_output`**（含注释里的反面教材除外）。

        已逐条确认过的例外写在下面（都是普通命令，父进程写完即退）：
          * `capsplus/extcheck.py`：探索用外部命令（一次性）
          * `capsplus/shot_ocr.py`：本地 OCR 的 `python -c`（跑完即退）
          * `capsplus/shot_capture.py`：`journalctl`（读完即退）
          * `capsplus/apply.py` / `capsplus/mapping_overrides.py`：keyd/工具命令（一次性）
          * `capsplus/voice_inject.py`：**有意为之** —— 那边自己分了"守护步 vs 普通步"两类等法
        """
        allow_files = {"voice_inject.py"}
        offenders = []

        def code_strings_and_kwargs(fn):
            """一个函数里**代码**用到的字符串常量与关键字参数（不含文档字符串）。"""
            body = [n for n in fn.body
                    if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                            and isinstance(n.value.value, str))]
            strings, kwargs = [], []
            for sub in ast.walk(ast.Module(body=body, type_ignores=[])):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    strings.append(sub.value)
                if isinstance(sub, ast.keyword) and sub.arg:
                    kwargs.append(sub.arg)
            return strings, kwargs

        for path in sorted((REPO / "capsplus").glob("*.py")):
            if path.name in allow_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                strings, kwargs = code_strings_and_kwargs(node)
                if "capture_output" not in kwargs:
                    continue
                if any("wl-copy" in s or "wl-paste" in s for s in strings):
                    offenders.append(f"{path.name}:{node.lineno}({node.name})")
        self.assertEqual(offenders, [],
                         f"这些点可能在等一条没人关的管子：{offenders}")


if __name__ == "__main__":
    unittest.main()
