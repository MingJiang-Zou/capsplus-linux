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

from capsplus import shot_annotate as AN
from capsplus import shot_overlay as OV          # noqa: E402
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

    # ---- ② 窄选区：窗口宽度必须装得下工具条 ----
    def test_narrow_selection_window_is_wide_enough_for_the_toolbar(self):
        """② 窄图（260×420）时，窗口宽度必须 ≥ 工具条固有宽度 + 两边。

        2026-10-20 真机：`--annotate` 一张 260×420 的图，窗口 288×510、而工具条固有宽度 **1012px**
        ⇒ `bar_layout` 把条夹到 `x=2` 之后右端伸到窗口外面 **被窗口黑边切掉**（颜色圆点那排看不见）。
        根因：窗口尺寸只由截图算，而工具条的宽度与截图无关。
        """
        screen = (1840, 920)                      # `screen_size()` 的形状（主屏减去边距）
        # 本机实测的工具条宽度：950（33 个子控件）+ 64（间距）+ 12（.cp-bar 的 padding 5×2 + border 1×2）
        bar_w = 1026
        plan = AN.window_plan((260, 420), screen, bar_h=46,
                              min_width=AN.bar_min_window_width(bar_w))
        self.assertGreaterEqual(plan["window"][0], bar_w,
                                f"窗口 {plan['window']} 装不下 {bar_w}px 的工具条")
        bar = AN.bar_layout(plan["canvas"], (bar_w, 46), plan["window"])
        self.assertGreaterEqual(bar["x"], 0, "工具条左端出界了")
        self.assertLessEqual(bar["x"] + bar_w, plan["window"][0],
                             f"工具条右端越出窗口（就是明江看到的那条切边）：{bar}")
        self.assertFalse(AN.bar_overlaps_image({**bar, "w": bar_w, "h": 46}, plan["canvas"]),
                         "变宽之后工具条还得在图像外面")

    def test_wide_screen_keeps_the_old_behaviour(self):
        """宽度下限只在"真装不下"时起作用：图本来就比工具条宽时，窗口尺寸与旧口径一致。"""
        plan = AN.window_plan((1200, 600), (1840, 920), bar_h=46, min_width=1016)
        self.assertEqual(plan["window"][0], 1200 + AN.CANVAS_MARGIN * 2,
                         "图够宽时不该被 min_width 撑大")
        self.assertEqual(plan["canvas"]["image"][0], AN.CANVAS_MARGIN,
                         "图够宽时左边距还是 CANVAS_MARGIN")

    def test_min_width_centres_the_image_and_bar_min_width_adds_both_edges(self):
        plan = AN.window_plan((260, 420), (1840, 920), bar_h=46,
                              min_width=AN.bar_min_window_width(760))
        left, _top, w, _h = plan["canvas"]["image"]
        self.assertEqual(w, 260)
        self.assertGreater(left, AN.CANVAS_MARGIN, "变宽的画布里图像要居中，不是靠左")
        self.assertEqual(left * 2 + w, plan["window"][0], "左右留白应当相等")
        self.assertEqual(AN.bar_min_window_width(760), 760 + AN.BAR_EDGE * 2)

    # ---- ③④ 每个按钮都有"是什么 + 快捷键"的提示；状态栏跟着显示 ----
    def test_every_tool_tip_carries_name_key_and_meaning(self):
        for tool in AN.TOOLS:
            with self.subTest(tool=tool):
                tip = AN.tool_tip(tool)
                self.assertIn(AN.TOOL_LABELS[tool], tip, "提示里要有名字")
                self.assertIn(AN.TOOL_KEYS[tool], tip, "提示里要有快捷键")
                self.assertGreater(len(tip), len(AN.TOOL_LABELS[tool]) + 4,
                                   f"只写了名字等于没说这是什么：{tip}")

    def test_colour_and_width_tips_carry_their_keys(self):
        for name, key in AN.COLOR_KEYS.items():
            self.assertIn(key, AN.colour_tip(name), f"颜色 {name} 的提示里没有键")
            self.assertIn(name, AN.colour_tip(name))
        for width, key in AN.WIDTH_KEYS.items():
            self.assertIn(key, AN.width_tip(width))
            self.assertIn(str(width), AN.width_tip(width))

    def test_eyedropper_is_discoverable_not_missing(self):
        """③ 吸管**本来就在工具表里**（`shot_annotate.TOOLS`），缺的是"认得出"。

        所以这条钉的是**发现性**：名字（吸管）+ 快捷键（I）+ 说明（取色、不改画面）三样都在提示里。
        """
        self.assertIn("eyedropper", AN.TOOLS)
        tip = AN.tool_tip("eyedropper")
        self.assertIn("吸管", tip)
        self.assertIn("I", tip)
        self.assertIn("颜色", tip)
        from capsplus import shot_icons as SI
        self.assertIn("eyedropper", SI.TOOL_ICONS, "图标表里也得有它（不然按钮是空的）")

    def test_intrinsic_width_counts_the_css_padding_and_border(self):
        """工具条宽度必须**含 CSS 的 padding/border**（少了那 12px 就会切边 —— 真窗口判据抓到过）。

        纯逻辑那半：拿一个假 bar 走 `_bar_intrinsic_width()` 的两条路（有 natural 用它、
        没有就用子控件之和兜底），断言不会算得比按钮本身还小。
        """
        class _FakeBtn:
            def get_preferred_width(self):
                return (10, 40)

        class _FakeBar:
            def __init__(self, natural):
                self._natural = natural

            def get_preferred_width(self):
                if self._natural is None:
                    raise RuntimeError("没有显示服务器")
                return (self._natural, self._natural)

            def get_spacing(self):
                return 2

            def get_children(self):
                return [_FakeBtn(), _FakeBtn(), _FakeBtn()]

        an = AN.Annotator.__new__(AN.Annotator)
        an._bar = _FakeBar(1026)
        self.assertEqual(an._bar_intrinsic_width(), 1026, "该用 GTK 报的 natural（含 CSS）")
        an._bar = _FakeBar(None)                  # 拿不到时退回子控件之和：3×40 + 2×2 = 124
        self.assertEqual(an._bar_intrinsic_width(), 124, "兜底要把按钮本身算全")

    def test_key_table_has_no_duplicates_and_covers_everything(self):
        """键位表**唯一一份**：工具/颜色/线宽的键互不重复，且每个按钮都能查到键。"""
        keys = list(AN.TOOL_KEYS.values()) + list(AN.COLOR_KEYS.values()) + list(AN.WIDTH_KEYS.values())
        self.assertEqual(len(keys), len(set(keys)), f"有键撞了：{keys}")
        self.assertEqual(sorted(AN.TOOL_KEYS), sorted(AN.TOOLS), "每个工具都要有键")
        self.assertEqual(len(AN.COLOR_KEYS), len(AN.COLORS))
        self.assertEqual(len(AN.WIDTH_KEYS), len(AN.WIDTHS))
        for tool, key in AN.TOOL_KEYS.items():
            with self.subTest(tool=tool):
                self.assertEqual(AN.tool_for_key(key), tool, "按提示里写的键要真能选中这个工具")
                self.assertEqual(AN.tool_for_key(key.lower()), tool)

    def test_key_hint_line_is_derived_from_the_table(self):
        """键位提示那行必须**从表里现算**（别写成第二份真相）：表里改了，提示跟着改。"""
        hint = AN.tool_key_hint()
        for tool, key in AN.TOOL_KEYS.items():
            self.assertIn(f"{key}={AN.TOOL_LABELS[tool]}", hint)
        for name, key in AN.COLOR_KEYS.items():
            self.assertIn(f"{key}={name}", hint)
        self.assertIn(AN.tool_key_hint(), AN.hint_text(True),
                      "一整条提示里也要带上工具键位（两处都读同一个函数）")

    def test_colour_and_width_keys_resolve(self):
        for name, key in AN.COLOR_KEYS.items():
            self.assertEqual(AN.colour_for_key(key), dict(AN.COLORS)[name])
        for width, key in AN.WIDTH_KEYS.items():
            self.assertEqual(AN.width_for_key(key), width)
        self.assertIsNone(AN.tool_for_key("q"))
        self.assertIsNone(AN.colour_for_key("9z"))
        self.assertIsNone(AN.width_for_key("z"))

    def test_action_tips_keep_their_shortcut_in_the_text(self):
        """动作类按钮（撤销/存盘/贴图…）的提示里也要有快捷键 —— 它们读的是同一份 `TIPS`。"""
        from capsplus.shot_icons import TIPS
        for icon, key in (("undo", "Ctrl+Z"), ("redo", "Ctrl+Y"), ("copy", "Enter"),
                          ("save", "Ctrl+S"), ("pin", "F3"), ("close", "Esc")):
            with self.subTest(icon=icon):
                self.assertIn(key, TIPS[icon], f"{icon} 的提示里没有快捷键")

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



@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：起不了标注器的底图（nogi 口径）")
class TestPanelCopyGoesThroughWlCopy(unittest.TestCase):
    """判据：结果面板那个「复制」按钮**真的把文字送出去了**，而且走的是能活过退出的那条路。

    2026-10-20 明江真机报"翻译 / OCR 的文本点复制按钮复制不出来"。根因不是剪贴板工具坏了，是那行代码用了
    **GTK4** 的 `Gdk.Display.get_clipboard()` —— GTK3 的 `Gdk.Display` 上没有这个方法（真机实测
    `AttributeError: 'GdkWaylandDisplay' object has no attribute 'get_clipboard'`），异常抛在
    GTK 信号处理器里、stderr 落在 keyd 热键那条没人看的路径上 ⇒ 现象就是"点了完全没反应"。
    而且那段代码既没刷新状态栏、也没汇报失败，"成功"和"没反应"长得一模一样。

    这一组钉四件事：
      ① 送出去的内容就是面板当前该复制的那段（有译文优先译文）；
      ② 走的是注入的文本复制器（生产实现 = `copy_text_to_clipboard` → `wl-copy`，选区由外部进程持有，
         窗口关掉文字还在；GTK 自己的剪贴板做不到）；
      ③ 成功/失败**都要在状态栏说出来**，失败要把原因原样带出去；
      ④ 面板没内容时**不许**去碰剪贴板。
    """

    def make(self, copy_text=None):
        seen = []

        def stub(text):
            seen.append(text)
            return (True, "✓ 已复制 %s" % text)

        an = AN.Annotator(white_surface(80, 40), screen=(80, 40),
                          clip=lambda _d: (True, "stub"), copy_text=copy_text or stub)
        return an, seen

    def test_button_copies_the_panel_text_through_the_injected_copier(self):
        an, seen = self.make()
        an.panel.set_ocr("发票号码 12345", "local", 12)
        ok, msg = an.copy_panel_text()
        self.assertTrue(ok, msg)
        self.assertEqual(seen, ["发票号码 12345"], "没把面板文字交出去（或交错了）")
        self.assertIn("已复制", an.message, "状态栏没告诉人复制成了")

    def test_translation_wins_over_source_text(self):
        an, seen = self.make()
        an.panel.set_ocr("原文字", "local", 9)
        an.panel.set_translation("原文字", "译文", "cloud", 20)
        an.copy_panel_text()
        self.assertEqual(seen, ["译文"], "有译文时该复制译文（primary_text 的口径）")

    def test_failure_is_reported_not_swallowed(self):
        """"复制不出来"必须当场说出来 —— 静默失败正是这次被报上来的形状本身。"""
        an, _ = self.make(copy_text=lambda _t: (False, "✗ 这台机没有 wl-copy（Wayland 剪贴板工具没装）"))
        an.panel.set_ocr("文字", "local", 5)
        ok, msg = an.copy_panel_text()
        self.assertFalse(ok)
        self.assertIn("wl-copy", msg)
        self.assertEqual(an.message, msg, "失败原因没落到状态栏")

    def test_empty_panel_never_touches_the_clipboard(self):
        an, seen = self.make()
        ok, msg = an.copy_panel_text()
        self.assertFalse(ok)
        self.assertEqual(seen, [], "面板没内容却去碰了剪贴板")
        self.assertIn("没有可复制的内容", msg)

    def test_default_copier_is_the_wl_copy_text_path(self):
        """默认实现必须是 `copy_text_to_clipboard`（wl-copy），不是 GTK 自己的剪贴板。"""
        an = AN.Annotator(white_surface(80, 40), screen=(80, 40), clip=lambda _d: (True, "stub"))
        self.assertIs(an._copy_text, AN.copy_text_to_clipboard)


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

    def test_clipboard_command_shape_is_unchanged(self):
        """复制图片的命令形状：`wl-copy --type image/png`（单独一条，红了一眼就知道是形状变了）。"""
        self.assertEqual(AN.clipboard_command(), ["wl-copy", "--type", "image/png"],
                         "复制图片的命令形状变了 —— 少了 `--type image/png`，剪贴板里会是一坨二进制")

    def test_copy_to_clipboard_is_fast_with_a_daemon_shaped_command(self):
        """端到端：`copy_to_clipboard` 在守护型命令上必须**远小于 timeout** 地返回 ok=True。

        2026-10-20 改的口径（原来这条**在无显示的 shell 里挂死**）：它真跑 `wl-copy --type image/png`，
        没有合成器时那条命令根本不肯退出 ⇒ 卡满 5 秒 timeout、报 ok=False（实测 `timeout 15 wl-copy
        --type image/png` rc=124），还把 `test_no_gi_smoke` 里"整套 discover 退出码为 0"那条级联带红。
        现在**形状与行为分开验**：
          * 形状 —— 上面那条断言真命令还是 `wl-copy --type image/png`；
          * 行为 —— 命令换成**等价形状的 stub**（`sh -c 'sleep 3 & exit 0'`：父进程立刻退、留守者抱着
            stderr），端到端跑一遍 `copy_to_clipboard`。不需要显示服务器、也不需要真装 wl-copy，
            更不会像老版那样在真桌面上把明江的剪贴板塞进一张 PNG。
        runner 要是又回到 `capture_output=True`，留守者会抱住那条永不 EOF 的管子 ⇒ 这里就慢过 1 秒 = 红。
        """
        stub = ["sh", "-c", "sleep 3 & exit 0"]
        real = AN.CLIPBOARD_CMD
        AN.CLIPBOARD_CMD = tuple(stub)
        try:
            t0 = time.monotonic()
            ok, why = AN.copy_to_clipboard(b"hello", runner=AN._default_clipboard_runner,
                                           which=lambda _n: "/usr/bin/wl-copy")
            elapsed = time.monotonic() - t0
        finally:
            AN.CLIPBOARD_CMD = real
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


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真造图/真渲染")
class TestArrowAndLineEndpoints(unittest.TestCase):
    """箭头/直线的**起点与终点必须就是按下的点与抬起的点**（2026-10-20 真机 P0）。

    明江原话：「标注箭头和直线的时候有 bug，在点击拖动添加箭头的时候，**箭头的起始点会到处跑
    （和鼠标移动方向关联）**，**箭头的终点反而不会 y 轴移动**」。

    实测根因（先复现、后定位）：`drag()` 用 `normalize_rect(A, B)` 落地成一个**正宽高**矩形，
    而渲染箭头时是**写死的"选区左下 → 右上"对角线**（`move_to(x, y+h) → line_to(x+w, y)`）——
    方向信息在 job 之前就丢了。于是：
      * 起点 = 矩形的某个角（随拖拽象限翻来翻去）⇒ "到处跑"；
      * 竖直方向上，矩形顶边 `min(y0,y1)` 与高度 `|dy|` 反向变化 ⇒ 终点 y 被钉住 ⇒ "终点不随 y 动"。
    **不是**上一批"落笔裁在截图内"引入的（把裁剪整个关掉，本判据照样红——见 class
    `TestClippingDoesNotChangeEndpoints` 与 mutation 记录）。
    """

    def _annotator(self):
        return AN.Annotator(white_surface(400, 300), screen=(400, 300),
                            directory=temp_dir(), clip=lambda _d: (True, "s"))

    def _drag(self, tool, a, b):
        an = self._annotator()
        an.set_tool(tool)
        an.press(a)
        an.drag(b)
        self.assertTrue(an.release(), f"{tool}: {a}→{b} 没落地")
        return an.stack.shapes[-1]

    def test_endpoints_are_the_pressed_and_released_points(self):
        quadrants = {"右下": ((60, 60), (300, 240)), "右上": ((60, 240), (300, 60)),
                     "左下": ((300, 60), (60, 240)), "左上": ((300, 240), (60, 60))}
        for tool in ("arrow", "line"):
            for name, (a, b) in quadrants.items():
                with self.subTest(tool=tool, quadrant=name):
                    shape = self._drag(tool, a, b)
                    self.assertEqual(AN.shape_endpoints(shape), (a, b),
                                     f"{tool} 在「{name}」方向拖出来的起点/终点不是按下/抬起的点")

    def test_cross_screen_start_still_keeps_the_direction(self):
        """跨屏起手（从一块屏拖到另一块）也要保持方向。"""
        an = AN.Annotator(white_surface(800, 300), screen=(800, 300),
                          directory=temp_dir(), clip=lambda _d: (True, "s"))
        an.set_tool("arrow")
        an.press((100, 250))
        an.drag((700, 50))
        self.assertTrue(an.release())
        self.assertEqual(AN.shape_endpoints(an.stack.shapes[-1]), ((100, 250), (700, 50)))

    def test_moving_and_resizing_keeps_both_ends_consistent(self):
        an = self._annotator()
        an.set_tool("arrow")
        an.press((60, 60))
        an.drag((300, 240))
        an.release()
        shape = an.stack.shapes[-1]
        an.select_at((180, 150), tol=30)
        an.move_selected(10, -20)
        self.assertEqual(AN.shape_endpoints(shape), ((70, 40), (310, 220)), "拖动后两端都要跟着挪")
        r = AN.shape_bounds(shape)
        an.resize_selected("se", (r[0] + r[2] + 40, r[1] + r[3] + 20))
        start, end = AN.shape_endpoints(shape)
        bx, by, bw, bh = AN.shape_bounds(shape)
        # 端点永远落在包围盒的两个对角上（缩放/移动后也是），这次盒子被拉到 280×200
        self.assertEqual((end[0] - start[0], end[1] - start[1]), (bw, bh),
                         "端点该跟着手柄走（方向不变、和包围盒对角一致）")
        self.assertGreater(end[1], start[1],
                           "这次是「左上 → 右下」拖的：起点该在上、终点在下，方向不许翻")

    def test_arrow_head_is_at_the_end_we_released_on(self):
        """像素标尺：箭头改在**终点**那一侧（画到空白底上，数哪一侧有线）。"""
        an = self._annotator()
        an.set_tool("arrow")
        an.press((60, 60))
        an.drag((340, 260))
        an.release()
        shape = an.stack.shapes[-1]
        out = AN.render_to_surface(white_surface(400, 300), [shape])
        out.flush()
        data = bytes(out.get_data())
        stride = out.get_stride()

        def ink(x, y):
            off = y * stride + x * 4
            return sum(1 for i in range(3) if data[off + i] < 200)
        near_end = sum(ink(x, y) for x in range(300, 340) for y in range(220, 260))
        near_start = sum(ink(x, y) for x in range(60, 100) for y in range(60, 100))
        self.assertGreater(near_end, near_start,
                           "箭头头部不在我们松手的那个端点（还是画在写死的对角线上）")


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真造图")
class TestEveryToolInEveryDragQuadrant(unittest.TestCase):
    """顺手查一遍**所有**图元在四个象限拖出来的几何（lead 要求"别只修箭头"）。

    判据：① 图形整块落在截图内（裁剪仍生效）；② 包围盒 == 按下点与抬起点的规范化矩形
    （四个象限各一遍）—— 有符号问题的图元会在这里露出来。
    """

    def test_bounds_match_the_drag_rect_in_all_quadrants(self):
        from capsplus.shot_frame import normalize_rect
        quadrant = {"右下": ((60, 60), (320, 260)), "右上": ((60, 260), (320, 60)),
                    "左下": ((320, 60), (60, 260)), "左上": ((320, 260), (60, 60))}
        for tool in AN.TOOLS:
            if tool in ("text", "badge", "eyedropper"):
                continue        # 这三个是"点一下"的（文字/序号/吸管），不靠拖
            for name, (a, b) in quadrant.items():
                with self.subTest(tool=tool, quadrant=name):
                    an = AN.Annotator(white_surface(400, 300), screen=(400, 300),
                                      directory=temp_dir(), clip=lambda _d: (True, "s"))
                    an.set_tool(tool)
                    an.press(a)
                    an.drag(b)
                    if tool == "pen":
                        an.drag(((a[0] + b[0]) // 2, (a[1] + b[1]) // 2))
                    if not an.release():
                        self.assertIn(tool, ("pen",), f"{tool} 在「{name}」没落地")
                        continue
                    shape = an.stack.shapes[-1]
                    x, y, w, h = AN.shape_bounds(shape)
                    self.assertGreaterEqual(x, 0, f"{tool}/{name} 越出左边界")
                    self.assertGreaterEqual(y, 0, f"{tool}/{name} 越出上边界")
                    self.assertLessEqual(x + w, 400, f"{tool}/{name} 越出右边界")
                    self.assertLessEqual(y + h, 300, f"{tool}/{name} 越出下边界")
                    if tool != "pen":
                        self.assertEqual((x, y, w, h), normalize_rect(a[0], a[1], b[0], b[1]),
                                         f"{tool}/{name} 的包围盒和拖出来的矩形不一致（符号问题）")


class TestClippingDoesNotChangeEndpoints(unittest.TestCase):
    """反证："落笔裁到截图内"**不是**箭头方向 bug 的成因。

    把裁剪完全关掉（模拟加裁剪之前的行为）跑同一条几何判据：如果仍然红，就说明病灶在
    "方向信息在落地时就丢了"，与裁剪无关。这条判据保证以后不会有人把锅甩错地方。
    """

    def test_shape_endpoints_helper_is_not_the_problem(self):
        rect_from_points = AN.shape_endpoints
        shape = AN.Shape("arrow", (60, 60, 240, 180), points=[(60, 60), (300, 240)])
        self.assertEqual(rect_from_points(shape), ((60, 60), (300, 240)),
                         "存了端点就该原样读出来（问题在落地时没存）")


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真造图")
class TestEyedropperPicksExactPixels(unittest.TestCase):
    """提取颜色（2026-10-20 新功能）：**逐像素标尺**，不许"看着差不多"。

    遮罩阶段的读数与标注窗的吸管都从**冻结图/底图**取色，判据用一张已知颜色的假图来量：
    每个测试点都断言 `#RRGGBB` **精确相等**；反例（取样坐标偏 1px）必红 —— 这张图每一列颜色都不同。
    """

    def _gradient(self, w=16, h=4):
        """每一列颜色都不同的假冻结图：(x, y) 的颜色 = (x*16, y*32, 0x40)。"""
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surface)
        for x in range(w):
            for y in range(h):
                cr.set_source_rgb(x * 16 / 255.0, y * 32 / 255.0, 0x40 / 255.0)
                cr.rectangle(x, y, 1, 1)
                cr.fill()
        surface.flush()
        return surface

    def test_overlay_reads_the_exact_hex(self):
        frame = self._gradient()
        for x, y in ((0, 0), (3, 1), (15, 3), (7, 2)):
            with self.subTest(px=(x, y)):
                rgb = OV.pixel_colour(frame, (x, y), (0, 0, 16, 4), scale=1.0)
                self.assertEqual(rgb, (x * 16, y * 32, 0x40), f"({x},{y}) 取到的不是那个像素")
                self.assertEqual(OV.hex_colour(rgb), "#%02X%02X%02X" % (x * 16, y * 32, 0x40))

    def test_one_pixel_offset_changes_the_result(self):
        """反例守则：这张图**每一列都不同** ⇒ 坐标偏 1px 一定读出差值（判据不是碰运气）。"""
        frame = self._gradient()
        a = OV.pixel_colour(frame, (5, 1), (0, 0, 16, 4), scale=1.0)
        b = OV.pixel_colour(frame, (6, 1), (0, 0, 16, 4), scale=1.0)
        self.assertNotEqual(a, b, "偏 1px 读出来一样 ⇒ 这条判据分辨不出偏移")

    def test_overlay_scales_logical_coords_to_frame_pixels(self):
        """帧是 2×（缩放屏）时，逻辑坐标要先放大再取样。"""
        frame = self._gradient(32, 8)
        rgb = OV.pixel_colour(frame, (5, 1), (0, 0, 16, 4))       # 自动推 scale=2
        self.assertEqual(rgb, ((5 * 2) * 16 % 256, (1 * 2) * 32 % 256, 0x40))

    def test_annotator_eyedropper_sets_the_colour_and_next_shape_uses_it(self):
        base = self._gradient(64, 32)
        an = AN.Annotator(base, screen=(64, 32), directory=temp_dir(), clip=lambda _d: (True, "s"))
        an.set_tool("eyedropper")
        an.press((6, 2))
        self.assertEqual(an.picked, "#%02X%02X%02X" % (6 * 16, 2 * 32, 0x40))
        self.assertEqual(an.color, (6 * 16 / 255.0, 2 * 32 / 255.0, 0x40 / 255.0, 1.0))
        self.assertEqual(an.stack.shapes, [], "取色不该产生图形")
        # 接着画一个矩形：必须用刚取的颜色
        an.set_tool("rect")
        an.press((8, 4))
        an.drag((40, 24))
        an.release()
        self.assertEqual(an.stack.shapes[-1].color, an.color, "新图元没用吸管的颜色")
        # 状态栏是 GTK 控件（没建窗口时没有）—— 直接用纯函数断言状态文本
        from capsplus import shot_style as _S
        self.assertIn(an.picked, _S.status_text(True, "", an.message, picked=an.picked),
                      "状态栏文本要显示取到的 hex")

    def test_eyedropper_outside_the_image_is_a_no_op(self):
        an = AN.Annotator(self._gradient(), screen=(16, 4), directory=temp_dir(),
                          clip=lambda _d: (True, "s"))
        before = an.color
        an.set_tool("eyedropper")
        an.press((-5, -5))
        self.assertEqual(an.color, before, "图外取色不该改颜色")
        self.assertEqual(an.picked, "")


@unittest.skipUnless(not GUI_SKIP, f"跳过界面用例：{GUI_SKIP}")
class TestNarrowWindowInARealWindow(unittest.TestCase):
    """② 的**真窗口**判据：260×420 的窄图，窗口与工具条的**实际分配**都要装得下。

    真机复现命令（明江给的，留在这里好复算）：
        python3 -c "from PIL import Image; Image.new('RGB',(260,420),(240,240,245)).save('/tmp/narrow.png')"
        capsplus-shot --annotate /tmp/narrow.png
    修前：窗口 288×510、工具条 1012px ⇒ 右端（颜色圆点那排）越出窗口被切掉。
    """

    def test_window_and_bar_allocation_agree_for_a_narrow_image(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        an = AN.Annotator(white_surface(260, 420), screen=(1840, 920),
                          directory=temp_dir(), clip=lambda _d: (True, "s"))
        state = {}

        def build():
            an._loop = GLib.MainLoop()
            an._win = an._build()
            an._win.show_all()
            GLib.timeout_add(150, measure)
            return False

        def measure():
            win_w = an._win.get_size()[0]
            alloc = an._bar.get_allocation()
            state["win"] = an._win.get_size()
            state["bar_w"] = an._bar_intrinsic_width()
            state["alloc"] = (alloc.x, alloc.y, alloc.width, alloc.height)
            state["plan"] = an.plan["window"]
            state["layout"] = dict(an._bar_layout)
            an._win.destroy()
            loop.quit()
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(0, build)
        fired = []
        fallback = GLib.timeout_add(3000, lambda: (fired.append(1), loop.quit(), False)[2])
        loop.run()
        if not fired:
            try:
                GLib.source_remove(fallback)
            except Exception:
                pass
        # 宽度必须与规划一致（这条是 ② 的全部）；高度会**多出状态栏那两行**（它单独占一层，
        # 规划里算的是画布那条带），所以只要求 ≥
        self.assertEqual(state["win"][0], state["plan"][0], "窗口实际宽度与规划不一致")
        self.assertGreaterEqual(state["win"][1], state["plan"][1],
                                "窗口实际高度比画布还小 —— 画布被裁了")
        self.assertGreaterEqual(state["win"][0], state["bar_w"],
                                f"窗口 {state['win']} 装不下 {state['bar_w']}px 的工具条")
        x, _y, width, _h = state["alloc"]
        self.assertGreaterEqual(x, 0, "工具条左端出界")
        self.assertLessEqual(x + width, state["win"][0],
                             f"工具条右端越出窗口 —— 正是明江看到的那条切边：{state['alloc']}")
        self.assertGreaterEqual(width, state["bar_w"] - 4,
                                f"工具条被压窄了：{state['alloc']} vs 固有 {state['bar_w']}px")
        bar = {**state["layout"], "w": state["bar_w"], "h": 46}
        self.assertFalse(AN.bar_overlaps_image(bar, an.plan["canvas"]), f"工具条压到图上：{bar}")


@unittest.skipUnless(not GUI_SKIP, f"跳过界面用例：{GUI_SKIP}")
class TestToolbarTipsAndStatusInARealWindow(unittest.TestCase):
    """④ 在**真窗口**里逐按钮核对"是什么 + 快捷键"，并核对状态栏跟着走（2026-10-20）。

    明江："工具栏有几个按钮不知道什么意思" —— 纯函数那几条只证明表是对的，这条证明**按钮上真挂上了**、
    悬停/按键之后**状态栏真变了**。
    """

    def test_every_button_has_a_tip_with_its_key_and_the_status_follows(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gdk, GLib
        an = AN.Annotator(white_surface(200, 100), screen=(400, 300),
                          directory=temp_dir(), clip=lambda _d: (True, "s"))
        state = {}

        def build():
            an._loop = GLib.MainLoop()
            an._win = an._build()
            an._win.show_all()
            groups = {"工具": an._buttons, "颜色": an._swatches,
                      "线宽": an._widths, "动作": an._actions}
            state["empty"] = [f"{g}/{n}" for g, btns in groups.items()
                              for n, b in btns.items() if not (b.get_tooltip_text() or "").strip()]
            state["missing_key"] = [f"{g}/{n}" for g, btns in groups.items()
                                    for n, b in btns.items()
                                    if (n in AN.TOOL_KEYS and AN.TOOL_KEYS[n]
                                        not in b.get_tooltip_text())]
            state["status"] = an._status.get_text()
            state["more"] = an._status_more.get_text()
            # 悬停一个"不认识"的按钮（吸管）⇒ 状态栏立刻说清它是什么、按什么键
            an._hover_hint = an._buttons["eyedropper"].get_tooltip_text()
            an.refresh_status()
            state["hover_status"] = an._status.get_text()
            an._hover_hint = ""
            an.refresh_status()
            # 按快捷键：工具 / 颜色 / 线宽三样都要跟着变，按钮也要亮成新的那个
            class _Ev:
                pass
            for name, keyval in (("工具", Gdk.KEY_i), ("颜色", Gdk.KEY_4), ("线宽", Gdk.KEY_0)):
                ev = _Ev()
                ev.keyval = keyval
                ev.state = 0
                state[f"consumed_{name}"] = an._on_key(None, ev)
            state["tool"] = an.tool
            state["colour"] = an.color
            state["width"] = an.width
            state["eyedropper_active"] = an._buttons["eyedropper"].get_active()
            state["blue_active"] = an._swatches["蓝"].get_active()
            state["six_active"] = an._widths[6].get_active()
            state["status_after_key"] = an._status.get_text()
            state["more_has_keys"] = AN.tool_key_hint() in an._status_more.get_text()
            an._win.destroy()
            loop.quit()
            return False

        loop = GLib.MainLoop()
        GLib.timeout_add(0, lambda: (build(), False)[1])
        fired = []
        fallback = GLib.timeout_add(3000, lambda: (fired.append(1), loop.quit(), False)[2])
        loop.run()
        if not fired:
            try:
                GLib.source_remove(fallback)
            except Exception:
                pass
        self.assertEqual(state.get("empty"), [], "这些按钮没有悬浮提示（等于让人猜）")
        self.assertEqual(state.get("missing_key"), [], "提示里没写快捷键")
        self.assertIn("工具：矩形（R）", state.get("status", ""),
                      f"状态栏要写清当前工具与快捷键：{state.get('status')}")
        self.assertIn("悬停：吸管", state.get("hover_status", ""),
                      f"悬停要能当场看明白那个按钮是什么：{state.get('hover_status')}")
        self.assertIn("I", state.get("hover_status", ""))
        for kind in ("工具", "颜色", "线宽"):
            self.assertTrue(state.get(f"consumed_{kind}"), f"{kind} 的快捷键没被接手")
        self.assertEqual((state.get("tool"), state.get("width")), ("eyedropper", 6))
        self.assertEqual(state.get("colour"), dict(AN.COLORS)["蓝"])
        self.assertTrue(state.get("eyedropper_active"), "按 I 之后吸管按钮没亮")
        self.assertTrue(state.get("blue_active"), "按 4 之后蓝色圆点没亮")
        self.assertTrue(state.get("six_active"), "按 0 之后线宽 6px 没亮")
        self.assertIn("吸管（I）", state.get("status_after_key", ""))
        self.assertTrue(state.get("more_has_keys"), "次要那行没带上工具/颜色/线宽的键位")


if __name__ == "__main__":
    unittest.main()
