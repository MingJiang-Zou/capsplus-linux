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
        self.assertEqual(imported, {"__future__", "os", "subprocess", "tempfile", "pathlib"},
                         f"顶层不许 import gi / cairo / PIL，实际：{sorted(imported)}")


@unittest.skipUnless(RUN_GUI, "没设 CAPSPLUS_GUI=1：界面用例跳过")
@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo")
class TestRealWindow(unittest.TestCase):
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
                state["toolbar"] = len([w for w in an._win.get_child().get_children()])
                an._buttons["arrow"].set_active(True)
                state["tool"] = an.tool
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
        self.assertEqual(state.get("buttons"), ["arrow", "mosaic", "rect", "text"])
        self.assertEqual(state.get("tool"), "arrow", "点工具按钮要切换当前工具")


if __name__ == "__main__":
    unittest.main()