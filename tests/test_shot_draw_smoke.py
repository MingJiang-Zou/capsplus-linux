# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：**绘制面**（拿真 cairo 上下文真跑一遍）+ 全仓"调了不存在的方法"体检。

为什么单开这个文件（2026-10-20 真事故）：
    `capsplus/shot_overlay.py` 的 `draw_colour_readout()` 里有一行 `cr.flush()` —— pycairo 的
    `cairo.Context` **没有** flush（只有 `cairo.Surface.flush()`）。后果不是"这次没刷出去"，
    而是 **GTK 的 draw 回调每帧抛 AttributeError**：遮罩画不出十字线/取色读数，框选看着"完全没反应"
    （用户两次 Caps+F1 后只能去开 Snipaste；热修 `613cd5d`）。
    当时 1224 条判据**全绿** —— 因为没有任何一条走过"带颜色、带指针"的真实绘制路径：
    取色读数只在 `crosshair is not None and colour is not None` 时才画，而既有像素用例
    （`test_shot_overlay.TestFrozenBackgroundIsTheContent` 等）只喂 `frame`/`rect`，`colour` 一律 None。
    取色的**取样**那一半当时有判据（`test_shot_annotate.TestEyedropperPicksExactPixels`），
    漏的是**画出来**那一半 —— 本文件补的就是它。

四件事：
  ① 真判据（像素）：`draw_colour_readout()` / `paint()` 在离屏 `ImageSurface` 上喂**非 None 的颜色
     与指针**，断言不抛异常、色样方块 == 取样色、`#RRGGBB` 文本区有墨；
  ② 真实 draw 处理器：`Overlay._on_draw()`（GTK 每帧真正调的那个函数）用**桩控件**走一遍 ——
     「指针 + 冻结图 + 放大镜」全开，断言它**真的走到了**取色读数（出事那次就是这一级没被走过）；
     两个窗口的 `_on_draw` 都登记在册，新增窗口漏登记会红；
  ③ 绘制面冒烟：模块里每个"第一个参数是 cairo 上下文"的函数都拿真上下文调一遍
     （方法不存在 / 参数不兼容当场红）；**新绘制函数不登记就红**（不许静默漏掉）；
  ④ 全仓体检：源码里对 cairo 上下文 / gi 模块调的每个方法名，逐个拿**真对象** `hasattr` 核
     —— 不靠人眼 grep，也不靠一张写死的方法名白名单（那等于把"哪些方法存在"抄成第二份真相）。
  ⑤ 同一形状的第二个实例（2026-10-20 晚）：结果面板的"复制"按钮用了 **GTK4** 的
     `Gdk.Display.get_clipboard()`，而本仓是 GTK3 —— 真机上 `AttributeError: 'GdkWaylandDisplay'
     object has no attribute 'get_clipboard'`，异常抛在信号处理器里没人看见，现象就是"点了没反应"。
     ④ 只核了 `Gtk./Gdk.` 这类**模块级**引用，漏了**实例方法**。⑤ 补上：
     `display = Gdk.Display.get_default()` 之后所有 `display.xxx` 都拿真 `Gdk.Display` 类 `hasattr` 核。

反例具型：把 `cr.flush()` 加回 `draw_colour_readout()` ⇒ 本文件必红（`TestTheIncidentShapeIsCaught`
在进程内复现同一条错误；真改文件跑一遍的现场见交付报告）。
跑法：`python3 -m unittest tests.test_shot_draw_smoke`（装了 cairo 就跑；`PYTHONPATH=tools/ci/nogi`
那个口径没有 pycairo，自动 skip）。
"""

import ast
import inspect
import pathlib
import sys
import unittest
import warnings

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_annotate as SA        # noqa: E402
from capsplus import shot_frames as SFR         # noqa: E402
from capsplus import shot_icons as SI           # noqa: E402
from capsplus import shot_overlay as OV         # noqa: E402

try:
    import cairo
    HAVE_CAIRO = True
except Exception:                               # pragma: no cover - nogi 口径
    cairo = None
    HAVE_CAIRO = False

try:                                            # 顶层不许裸 import gi（test_no_gi_smoke 守着）
    import gi
    for _name, _ver in (("Gtk", "3.0"), ("Gdk", "3.0"), ("Pango", "1.0"),
                        ("GLib", "2.0"), ("Gio", "2.0")):
        gi.require_version(_name, _ver)
    from gi.repository import Gdk, Gio, GLib, Gtk, Pango
    GI_MODULES = {"Gtk": Gtk, "Gdk": Gdk, "Pango": Pango, "GLib": GLib, "Gio": Gio}
    HAVE_GI = True
except Exception:                               # pragma: no cover - 无头 CI / nogi 口径
    GI_MODULES = {}
    HAVE_GI = False

AREA = (0, 0, 400, 300)
RECT = (20, 20, 80, 60)                 # 离指针远一点：读数区不带选区边框/手柄的杂色
POINTER = (150, 150)
SAMPLE_RGB = (18, 52, 86)               # 冻结图左半：取样色（与任何主题色/常量都不同）
OTHER_RGB = (180, 40, 30)               # 冻结图右半：换个位置取样就应该是这个（证明按指针取）

# 本仓对 cairo 上下文的命名约定（体检按名字找对象，见 TestNoCallsToMethodsThatDoNotExist）。
# 裸名 `ctx` 在本仓一律是 GTK 的 `StyleContext`、`context` 是 `Gdk.AppLaunchContext` ——
# 不是 cairo 上下文，所以不入这张表；命名约定本身由一条判据守着（不许有人偷偷另起名字）。
CAIRO_CONTEXT_NAMES = ("cr", "crs")

# 本仓**所有用到 cairo 的 capsplus 模块**（grep 实测 2026-10-20；少一个就有绘制面漏在冒烟之外，
# 由 test_every_capsplus_module_that_draws_is_scanned 守着）。shot_frames 只切图不画图，
# 所以它在册但没有登记调用 —— 它新长出一个绘制函数时，"没登记就红"那条会当场抓住。
DRAW_MODULES = {"shot_overlay": OV, "shot_annotate": SA, "shot_icons": SI, "shot_frames": SFR}


# ─────────────────────────────────────────────────────────────── 工具

def frozen_surface(size=(400, 300)):
    """假冻结图：**左半 `SAMPLE_RGB`、右半 `OTHER_RGB`**（按位置取样才分得出对错）。"""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size[0], size[1])
    cr = cairo.Context(surface)
    for x0, rgb in ((0, SAMPLE_RGB), (size[0] // 2, OTHER_RGB)):
        cr.set_source_rgb(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
        cr.rectangle(x0, 0, size[0] - x0, size[1])
        cr.fill()
    surface.flush()
    return surface


def blank(size=(400, 300)):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size[0], size[1])
    return surface, cairo.Context(surface)


def surface_pixels(surface):
    """`(bytes, stride, w, h)` —— 一次读全图，逐像素断言时别再 `get_data()` N 次。"""
    surface.flush()
    return bytes(surface.get_data()), surface.get_stride(), surface.get_width(), surface.get_height()


def rgb_at(pixels, x, y):
    data, stride, w, h = pixels
    x = int(min(max(x, 0), w - 1))
    y = int(min(max(y, 0), h - 1))
    off = y * stride + x * 4
    return (data[off + 2], data[off + 1], data[off])       # cairo ARGB32 在小端是 BGRA


def luma(rgb):
    return sum(rgb) / 3.0


def close_rgb(a, b, tol=1):
    return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))


def ink_count(pixels, box, threshold=170.0):
    """一块矩形里"比底色亮"的像素数（文字/线条的墨）。"""
    x, y, w, h = box
    n = 0
    for yy in range(int(y), int(y + h)):
        for xx in range(int(x), int(x + w)):
            if luma(rgb_at(pixels, xx, yy)) >= threshold:
                n += 1
    return n


def opaque_pixels(pixels):
    """整张图里 α>0 的像素数（"到底画没画出东西"）。"""
    data, stride, w, h = pixels
    n = 0
    for y in range(h):
        row = y * stride
        for x in range(w):
            if data[row + x * 4 + 3]:
                n += 1
    return n


def font_usable(cr, probe="#0123456789ABCDEF"):
    """这台机器上有没有可用字体（cairo 的 toy API 拿不到字体时画出来是空的）。

    判据要的是"文本区有墨"，而"没装字体"不是这条判据要判红的东西 —— 那种情况显式 skip 并说清原因。
    """
    cr.select_font_face("Sans")
    cr.set_font_size(12)
    try:
        return cr.text_extents(probe).width > 0
    except Exception:
        return False


def readout_box(xy, size=(104, 26)):
    """`draw_colour_readout` 的落位规则（**与实现同一套避让**：右/下放不下就翻面 + 夹进区域）。

    判据要在画完的像素里找到"色样方块"与"文本区"，就得知道它落在哪。这里只复算**位置**，
    像素一律从真画出来的 surface 上量。
    """
    w, h = size
    ax, ay, aw, ah = AREA
    x, y = xy[0] + 8, xy[1] + 8
    if x + w > ax + aw:
        x = xy[0] - w - 8
    if y + h > ay + ah:
        y = xy[1] - h - 8
    x = max(ax, min(x, ax + aw - w))
    y = max(ay, min(y, ay + ah - h))
    return (x, y, w, h)


class _StubWidget:
    """`Overlay._on_draw` 只用到这两个方法（源码刻意不查 `self.windows`，判据才能这样走真处理器）。"""

    def __init__(self, w=AREA[2], h=AREA[3]):
        self._w, self._h = w, h

    def get_allocated_width(self):
        return self._w

    def get_allocated_height(self):
        return self._h


def overlay_for_draw(pointer=POINTER, rect=RECT, magnifier=True, readout=True):
    """搭一个只够跑 `_on_draw` 的 Overlay（一屏、一块冻结图、指针就位）。"""
    ov = OV.Overlay.__new__(OV.Overlay)
    ov.monitors = [AREA]
    ov.frames = [SFR.Frame(AREA, frozen_surface(), 1.0)]
    ov.rect = rect
    ov.pointer = (0, pointer)
    ov.magnifier = magnifier
    ov.colour_readout = readout
    return ov


def annotator_for_draw():
    """搭一个只够跑 `_on_draw` 的 Annotator（没建窗：`_on_draw` 会自己现算 canvas）。"""
    an = SA.Annotator(frozen_surface(), screen=(AREA[2], AREA[3]))
    an.plan = None
    return an


class _Spy:
    """把绘制函数换成"记一笔 + 照常调用真函数"的壳（**不 stub 掉绘制**，真路径照跑）。"""

    def __init__(self, module, name):
        self.module, self.name = module, name
        self.calls = []

    def __enter__(self):
        self.real = getattr(self.module, self.name)

        def wrapper(*args, **kwargs):
            result = self.real(*args, **kwargs)
            self.calls.append({"args": args, "kwargs": kwargs, "result": result})
            return result
        setattr(self.module, self.name, wrapper)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.real)
        return False


# ─────────────────────────────────────────────────────────────── ① 真判据：像素

@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真建 surface")
class TestColourReadoutOnRealSurface(unittest.TestCase):
    """`draw_colour_readout()` / `paint(colour=…)` 的**像素**判据（非 None 的颜色 + 指针）。

    这一组就是"取色读数每帧抛异常"的直接防线：绘制函数在真 cairo 上下文上真跑。
    """

    def test_draw_colour_readout_does_not_raise(self):
        _surface, cr = blank()
        box = OV.draw_colour_readout(cr, POINTER, SAMPLE_RGB, AREA)
        self.assertEqual(len(box), 4, "返回值应该是 (x, y, w, h)")

    def test_the_swatch_is_exactly_the_sampled_colour(self):
        surface, cr = blank()
        x, y, w, h = OV.draw_colour_readout(cr, POINTER, SAMPLE_RGB, AREA)
        got = rgb_at(surface_pixels(surface), x + 8, y + h // 2)
        self.assertTrue(close_rgb(got, SAMPLE_RGB, tol=0),
                        f"色样方块应该是取样色 {SAMPLE_RGB}，实际读到 {got}")

    def test_the_hex_text_area_has_ink(self):
        surface, cr = blank()
        if not font_usable(cr):
            self.skipTest("这台机器上 cairo 拿不到字体：文本区判不了墨（不是判红）")
        x, y, w, h = OV.draw_colour_readout(cr, POINTER, SAMPLE_RGB, AREA)
        text_box = (x + h + 4, y + 2, w - h - 6, h - 4)      # 色样右边那一段 = `#RRGGBB`
        ink = ink_count(surface_pixels(surface), text_box)
        self.assertGreaterEqual(ink, 12, f"`#RRGGBB` 文本区没墨（读到 {ink} 个亮点）—— 文字没画出来")

    def test_paint_with_a_colour_and_a_pointer_does_not_raise(self):
        """事故现场形态：`paint(..., crosshair=指针, magnifier=指针, colour=颜色)`。

        反例具型：`draw_colour_readout` 里加回 `cr.flush()` ⇒ 这一行直接抛 AttributeError。
        """
        surface, cr = blank()
        OV.paint(cr, AREA, RECT, frame=frozen_surface(), crosshair=POINTER,
                 magnifier=POINTER, colour=SAMPLE_RGB)
        x, y, w, h = readout_box(POINTER)
        got = rgb_at(surface_pixels(surface), x + 8, y + h // 2)
        self.assertTrue(close_rgb(got, SAMPLE_RGB, tol=0),
                        f"paint 里画出的色样应该是 {SAMPLE_RGB}，实际读到 {got}")

    def test_paint_without_a_colour_still_works(self):
        """反向：不喂颜色时不许画出读数（否则遮罩上永远挂着一块色值）。"""
        surface, cr = blank()
        OV.paint(cr, AREA, RECT, frame=frozen_surface(), crosshair=POINTER)
        x, y, w, h = readout_box(POINTER)
        got = rgb_at(surface_pixels(surface), x + 8, y + h // 2)
        self.assertFalse(close_rgb(got, SAMPLE_RGB, tol=0), "没喂颜色却画出了色样")


# ─────────────────────────────────────────────────────────────── ② 真判据：真 draw 处理器

@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真建 surface")
class TestTheRealDrawHandlerDrawsTheReadout(unittest.TestCase):
    """`Overlay._on_draw()` —— GTK 每帧真正调的那个函数，用桩控件整体走一遍。

    为什么非要走它、而不是只调 `paint()`：出事那次 **就是这一级没被走过**。
    `colour` 是 `_on_draw` 自己从"指针 + 冻结帧"算出来的（`pixel_colour`）；只调 `paint()`
    的用例喂 `colour=None`，于是 `draw_colour_readout` 一次都没被调用，错误一路绿着上线。
    """

    def _draw(self, **kwargs):
        ov = overlay_for_draw(**kwargs)
        surface, cr = blank()
        with _Spy(OV, "draw_colour_readout") as readout, _Spy(OV, "draw_magnifier") as mag:
            ret = ov._on_draw(_StubWidget(), cr, 0)
        return surface, ret, readout.calls, mag.calls

    def test_the_handler_reaches_the_colour_readout(self):
        """颜色读数**真的被画了** —— 这条 + 下一条就是本次事故的防线。"""
        _surface, ret, readout, mag = self._draw(magnifier=True)
        self.assertIs(ret, True, "draw 处理器必须 return True（否则主题背景会盖掉我们的画）")
        self.assertEqual(len(readout), 1, "真实 draw 处理器没走到取色读数（= 事故前的那条绿）")
        self.assertTrue(mag, "放大镜也没被走到")

    def test_the_readout_samples_the_colour_under_the_pointer(self):
        for pointer, expect in ((POINTER, SAMPLE_RGB), ((300, 150), OTHER_RGB)):
            with self.subTest(pointer=pointer):
                surface, _ret, readout, _mag = self._draw(pointer=pointer, magnifier=False)
                self.assertEqual(len(readout), 1)
                rgb = readout[0]["args"][2]
                self.assertTrue(close_rgb(rgb, expect, tol=1),
                                f"指针在 {pointer} 时的取样色应该是 {expect}，实际 {rgb}")
                x, y, w, h = readout[0]["result"]
                got = rgb_at(surface_pixels(surface), x + 8, y + h // 2)
                self.assertTrue(close_rgb(got, rgb, tol=0),
                                f"画出来的色样 {got} 与取样色 {rgb} 不一致")

    def test_the_hex_text_area_has_ink_in_the_real_path(self):
        surface, _ret, readout, _mag = self._draw(magnifier=False)
        self.assertEqual(len(readout), 1)
        pixels = surface_pixels(surface)
        x, y, w, h = readout[0]["result"]
        ink = ink_count(pixels, (x + h + 4, y + 2, w - h - 6, h - 4))
        if ink == 0:
            self.skipTest("这台机器上 cairo 拿不到字体：文本区判不了墨（不是判红）")
        self.assertGreaterEqual(ink, 12, f"真实路径里 `#RRGGBB` 文本区没墨（{ink} 个亮点）")

    def test_the_crosshair_is_drawn_at_the_pointer(self):
        """十字线那条线：指针那一行必须比旁边亮（压暗的背景 + 一条 α=0.35 的白线）。"""
        surface, _ret, _readout, _mag = self._draw(magnifier=False)
        pixels = surface_pixels(surface)
        on_line = max(luma(rgb_at(pixels, 250, POINTER[1])), luma(rgb_at(pixels, 250, POINTER[1] + 1)))
        off_line = luma(rgb_at(pixels, 250, POINTER[1] + 20))
        self.assertGreater(on_line, off_line + 40,
                           f"指针那一行没画出十字线（线上 {on_line:.0f} vs 线下 {off_line:.0f}）")

    def test_readout_can_be_turned_off(self):
        _surface, _ret, readout, _mag = self._draw(readout=False)
        self.assertEqual(readout, [], "colour_readout 关掉后不该再画色值读数")


# ─────────────────────────────────────────────────────────────── ③ 绘制面冒烟

class _Call:
    """一次冒烟调用：`args`/`kwargs` 原样喂给函数，`expect_ink` 说"这次应该画出东西"。"""

    def __init__(self, args, kwargs=None, expect_ink=True, note=""):
        self.args = args
        self.kwargs = kwargs or {}
        self.expect_ink = expect_ink
        self.note = note


def all_kinds_shapes():
    """每种图元各一个（`render` 的分支一个不漏：矩形/椭圆/箭头/直线/荧光笔/自由笔/序号/文字/马赛克）。"""
    return [
        SA.Shape("rect", (10, 10, 60, 40)),
        SA.Shape("ellipse", (80, 10, 60, 40)),
        SA.Shape("arrow", (10, 60, 60, 40), points=[(10, 100), (70, 60)]),
        SA.Shape("line", (80, 60, 60, 40), points=[(80, 60), (140, 100)]),
        SA.Shape("highlight", (150, 10, 60, 40)),
        SA.Shape("pen", (150, 60, 60, 40), points=[(150, 60), (170, 80), (190, 70)]),
        SA.Shape("badge", (220, 10, 30, 30), text="3"),
        SA.Shape("text", (220, 60, 60, 30), text="hi"),
        SA.Shape("mosaic", (300, 10, 60, 40)),
    ]


def _preset(cr):
    """冒烟前把 source 颜色/线宽准备好（`draw_icon`/`_draw_polyline` 用的是**当前** source）。"""
    cr.set_source_rgba(1.0, 0.9, 0.2, 1.0)
    cr.set_line_width(2.0)


# 登记表：(模块, 函数名) → 造调用的函数。**没登记就先红**（见 test_every_draw_function_is_registered）。
DRAW_CALLS = {
    ("shot_overlay", "paint"): lambda: [_Call(
        (AREA, RECT), {"frame": frozen_surface(), "crosshair": POINTER,
                       "magnifier": POINTER, "colour": SAMPLE_RGB},
        note="事故现场的组合：帧 + 指针 + 放大镜 + 颜色读数")],
    ("shot_overlay", "draw_magnifier"): lambda: [_Call((frozen_surface(), POINTER, AREA))],
    ("shot_overlay", "draw_colour_readout"): lambda: [_Call((POINTER, SAMPLE_RGB, AREA))],
    ("shot_annotate", "render"): lambda: [_Call(
        (frozen_surface(), all_kinds_shapes()),
        {"size": (400, 300), "selected": all_kinds_shapes()[0], "hover": all_kinds_shapes()[1]},
        note="九种图元全上（含选中描边/hover 虚线）")],
    ("shot_annotate", "draw_canvas"): lambda: [_Call(
        (SA.canvas_layout((400, 300)), frozen_surface(), all_kinds_shapes()),
        {"selected": all_kinds_shapes()[0], "hover": all_kinds_shapes()[1]})],
    ("shot_annotate", "_draw_polyline"): lambda: [_Call((all_kinds_shapes()[5],))],
    ("shot_annotate", "_draw_selection"): lambda: [_Call(
        (all_kinds_shapes()[0], all_kinds_shapes()[1]))],
    ("shot_annotate", "_outline"): lambda: [_Call(
        ((10, 10, 80, 60), (1, 0, 0, 1), 2), {"dashes": [4, 3], "offset": 1})],
    ("shot_annotate", "_draw_badge"): lambda: [_Call((all_kinds_shapes()[6],))],
    ("shot_annotate", "_draw_mosaic"): lambda: [_Call(
        (frozen_surface(), (300, 10, 60, 40), (400, 300)))],
    ("shot_icons", "draw_icon"): lambda: [_Call((name, 24), note=f"图标 {name}")
                                          for name in SI.ICON_NAMES],
    ("shot_icons", "_arrow_head"): lambda: [_Call(
        ((10, 10), 0.5, 5), expect_ink=False,
        note="只建路径不 stroke（调用方自己 stroke），所以这里只判不炸")],
}

# 两个窗口的 `_on_draw`（GTK 真正调的那些；签名不是"cr 打头"，所以与 DRAW_CALLS 分开登记）。
# 值 = `(接收者, 把 cr 装成调用参数)`。
DRAW_HANDLERS = {
    ("shot_overlay", "Overlay._on_draw"): lambda: (overlay_for_draw(), lambda cr: (_StubWidget(), cr, 0)),
    ("shot_annotate", "Annotator._on_draw"): lambda: (annotator_for_draw(), lambda cr: (None, cr)),
}


def discovered_draw_functions() -> set:
    """模块里所有"第一个参数是 cairo 上下文"的函数。

    只认**本模块自己定义**的函数（`__module__` 对得上），不把 import 进来的算进去。
    """
    found = set()
    for label, module in DRAW_MODULES.items():
        for name, obj in vars(module).items():
            if inspect.isfunction(obj) and getattr(obj, "__module__", "") == module.__name__:
                params = list(inspect.signature(obj).parameters)
                if params and params[0] in CAIRO_CONTEXT_NAMES:
                    found.add((label, name))
    return found


def discovered_draw_handlers() -> set:
    """各窗口类上的 `_on_draw`（签名是 `(self, widget, cr[, idx])`，与上面那一类分开找）。"""
    found = set()
    for label, module in DRAW_MODULES.items():
        for cls_name, cls in vars(module).items():
            if inspect.isclass(cls) and getattr(cls, "__module__", "") == module.__name__:
                if inspect.isfunction(vars(cls).get("_on_draw")):
                    found.add((label, f"{cls_name}._on_draw"))
    return found


def cairo_using_capsplus_modules() -> set:
    """`capsplus/` 下**用到 cairo 的模块名**（`cairo.` 属性访问或 `_cairo()` 调用）。

    这个集合必须等于 `DRAW_MODULES` —— 否则新开一个会画画的模块就绕过了整个绘制面冒烟。
    """
    out = set()
    for path in repo_python_files():
        rel = path.relative_to(REPO)
        if rel.parts[0] != "capsplus":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "cairo") or \
               (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_cairo"):
                out.add(path.stem)
                break
    return out


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真建 surface")
class TestDrawSurfaceSmoke(unittest.TestCase):
    """绘制面冒烟：每个绘制函数拿**真** cairo 上下文调一遍。

    "调了不存在的方法 / 参数不兼容"这类错误一次拦住 —— 值不值？看事实：本次事故就是
    `cr.flush()`（不存在的方法）闯过了 1224 条判据。
    """

    def test_every_registered_draw_function_runs_on_a_real_context(self):
        ran = []
        for (label, name), builder in sorted(DRAW_CALLS.items()):
            module = DRAW_MODULES[label]
            for call in builder():
                with self.subTest(draw=f"{label}.{name}", note=call.note):
                    surface, cr = blank()
                    _preset(cr)
                    fn = getattr(module, name)
                    try:
                        fn(cr, *call.args, **call.kwargs)
                    except Exception as exc:                      # noqa: BLE001 - 冒烟要报全
                        self.fail(f"{label}.{name} 在真 cairo 上下文上炸了："
                                  f"{type(exc).__name__}: {exc}"
                                  + (f"（{call.note}）" if call.note else ""))
                    if call.expect_ink:
                        ink = opaque_pixels(surface_pixels(surface))
                        self.assertGreater(ink, 0, f"{label}.{name} 什么都没画出来"
                                                   + (f"（{call.note}）" if call.note else ""))
                ran.append(name)
        self.assertGreaterEqual(len(ran), 20, f"冒烟覆盖太少（只跑了 {len(ran)} 次）—— 登记表被砍了？")

    def test_every_window_draw_handler_runs_on_a_real_context(self):
        """两个窗口的 `_on_draw`（真函数、桩控件、真上下文）：GTK 只在有显示服务器时才调它们，
        所以无头口径里**只有这类冒充才能覆盖**；真窗口那一层另有 `CAPSPLUS_GUI=1` 的用例。"""
        for (label, dotted), build in sorted(DRAW_HANDLERS.items()):
            with self.subTest(handler=f"{label}.{dotted}"):
                receiver, args_for = build()
                surface, cr = blank()
                fn = getattr(receiver, dotted.split(".")[-1])
                try:
                    fn(*args_for(cr))
                except Exception as exc:                          # noqa: BLE001
                    self.fail(f"{label}.{dotted} 在真 cairo 上下文上炸了："
                              f"{type(exc).__name__}: {exc}")
                self.assertGreater(opaque_pixels(surface_pixels(surface)), 0,
                                   f"{label}.{dotted} 什么都没画出来")

    def test_every_draw_function_is_registered(self):
        """**新绘制函数不登记就红**：否则冒烟会静默漏掉它 —— 本次事故正是这种漏。"""
        self.assertEqual(sorted(discovered_draw_functions() - set(DRAW_CALLS)), [],
                         "有绘制函数没进 DRAW_CALLS 登记表（新画的函数要顺手加一条进冒烟）")
        self.assertEqual(sorted(discovered_draw_handlers() - set(DRAW_HANDLERS)), [],
                         "有窗口的 _on_draw 没进 DRAW_HANDLERS 登记表（否则它没人守）")

    def test_every_capsplus_module_that_draws_is_scanned(self):
        """新开一个会画画的模块 ⇒ 上面的冒烟会**整个绕过它**，所以这里判红。

        （实测 2026-10-20：`capsplus/` 下用到 cairo 的只有 shot_overlay / shot_annotate /
          shot_icons / shot_frames 四个，都在 DRAW_MODULES 里。）
        """
        self.assertEqual(sorted(cairo_using_capsplus_modules() - set(DRAW_MODULES)), [],
                         "这个 capsplus 模块用到 cairo 却没进 DRAW_MODULES（绘制面冒烟扫不到它）")

    def test_the_registry_has_no_stale_entries(self):
        """反向：登记表里指的是已不存在的函数 ⇒ 也是红的（防止登记表变成骗人的清单）。"""
        for label, name in sorted(DRAW_CALLS):
            self.assertTrue(hasattr(DRAW_MODULES[label], name), f"{label}.{name} 已经不存在了")
        for label, dotted in sorted(DRAW_HANDLERS):
            cls_name = dotted.split(".")[0]
            cls = getattr(DRAW_MODULES[label], cls_name, None)
            self.assertIsNotNone(cls, f"{label}.{cls_name} 已经不存在了")
            self.assertTrue(hasattr(cls, "_on_draw"), f"{label}.{dotted} 已经不存在了")


class TestTheIncidentShapeIsCaught(unittest.TestCase):
    """判据自己咬自己一次（不是摆设）：没有反例具型的判据等于没写过。

    这里在进程内把 `cr.flush()` **加回去**（换成壳函数，绘制照旧走真实现），断言真实 draw
    处理器当场抛 AttributeError —— 说明 ② 那一组判据走的确实是会炸的那条路。
    真改文件跑一遍的现场（红 → 绿）见提交信息与交付报告。
    """

    @unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真建 surface")
    def test_pycairo_context_really_has_no_flush(self):
        self.assertFalse(hasattr(cairo.Context, "flush"),
                         "pycairo 的 Context 竟然有 flush 了？核完再改这条判据与热修注释")
        self.assertTrue(hasattr(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1), "flush"),
                        "Surface.flush 才是存在的那个（本文件另有几处 flush 都打在 surface 上）")

    @unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真建 surface")
    def test_adding_the_line_back_turns_the_real_path_red(self):
        ov = overlay_for_draw()
        _surface, cr = blank()
        real = OV.draw_colour_readout

        def buggy(cr_, xy, rgb, area, size=(104, 26)):
            box = real(cr_, xy, rgb, area, size)
            cr_.flush()                     # ← 2026-10-20 热修删掉的那一行
            return box

        OV.draw_colour_readout = buggy
        try:
            with self.assertRaises(AttributeError) as ctx:
                ov._on_draw(_StubWidget(), cr, 0)
            self.assertIn("flush", str(ctx.exception),
                          "加的这条错误没被抓住（判据走的不是真路？）")
        finally:
            OV.draw_colour_readout = real


# ─────────────────────────────────────────────────────────────── ④ 全仓体检

def repo_python_files():
    """仓内全部 .py（跳过 .git / __pycache__ / 第三方 site-packages）。"""
    out = []
    for path in sorted(REPO.rglob("*.py")):
        parts = set(path.relative_to(REPO).parts)
        if parts & {".git", "__pycache__", "site-packages", "node_modules"}:
            continue
        out.append(path)
    return out


def _walk_repo_sources():
    for path in repo_python_files():
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def cairo_context_calls():
    """源码里所有 `<cr|crs>.<方法>(...)` 调用点：`(相对路径, 行号, 变量名, 方法名)`。"""
    found = []
    for path, tree in _walk_repo_sources():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in CAIRO_CONTEXT_NAMES):
                found.append((str(path.relative_to(REPO)), node.lineno,
                              node.func.value.id, node.func.attr))
    return found


def cairo_context_bindings():
    """源码里所有 `名字 = cairo.Context(...)` 的绑定名（命名约定守卫要用）。"""
    found = []
    for path, tree in _walk_repo_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            call = node.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "Context"
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == "cairo"):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.append((str(path.relative_to(REPO)), node.lineno, target.id))
    return found


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：体检要拿真 Context 逐个 hasattr")
class TestNoCallsToMethodsThatDoNotExist(unittest.TestCase):
    """全仓"调了 pycairo 上不存在的方法"体检（**以真对象为准**，不是 grep、也不是白名单）。

    为什么不用一张写死的方法名白名单：那等于把"pycairo 有哪些方法"抄成第二份真相，
    pycairo 一升级就漂，而漂的方向恰好是"漏掉新方法 / 留着老方法"。这里问真 `cairo.Context` 自己。
    只按名字找对象，所以命名约定本身也必须被守着（见下面第三条）。
    """

    def test_the_scan_is_not_vacuous(self):
        """先证明扫描真扫到了东西（判据空转比判据缺失更坏：看着是绿的）。"""
        self.assertGreaterEqual(len(cairo_context_calls()), 100,
                                "只扫到这么几处 cr.* 调用 —— 扫描本身坏了（会假绿）")
        self.assertGreaterEqual(len(cairo_context_bindings()), 5,
                                "没扫到 `cr = cairo.Context(...)` —— 命名约定守卫会假绿")

    def test_context_method_calls_exist_on_the_real_context(self):
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 4))
        bad = []
        for path, lineno, var, attr in cairo_context_calls():
            if not hasattr(cr, attr):
                bad.append(f"{path}:{lineno}  {var}.{attr}(…)")
        self.assertEqual(sorted(set(bad)), [],
                         "这些调用在真 pycairo 的 Context 上不存在（就是本次事故的形状）：\n  "
                         + "\n  ".join(sorted(set(bad))))

    def test_cairo_contexts_are_bound_to_a_scanned_name(self):
        """命名约定守卫：新上下文起了别的名字 ⇒ 上面的体检会**静默漏掉它**，所以这里判红。"""
        bad = [f"{path}:{lineno}  {name}"
               for path, lineno, name in cairo_context_bindings()
               if name not in CAIRO_CONTEXT_NAMES]
        self.assertEqual(bad, [],
                         "cairo 上下文换了名字，体检会漏掉它：\n  " + "\n  ".join(bad)
                         + f"\n（要么改回 {CAIRO_CONTEXT_NAMES}，要么把它加进 CAIRO_CONTEXT_NAMES）")

    @unittest.skipUnless(HAVE_GI, "没有 gi：这条要在真 PyGObject 上 hasattr")
    def test_gi_module_attributes_exist(self):
        """`Gtk.xxx` / `Gdk.xxx` / … 这类模块级引用逐个 hasattr 真模块。

        （嵌套的 `Gtk.Widget.foo` 这种只能靠真跑：`CAPSPLUS_GUI=1` 的 xvfb 口径覆盖。）
        """
        seen = {}
        for path, tree in _walk_repo_sources():
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id in GI_MODULES):
                    seen.setdefault((node.value.id, node.attr), []).append(
                        f"{path.relative_to(REPO)}:{node.lineno}")
        self.assertGreaterEqual(len(seen), 50, "只扫到这么几处 gi 模块级引用 —— 扫描坏了")
        with warnings.catch_warnings():
            # hasattr 会碰到 `Gio.DesktopAppInfo` 这类**已弃用但仍存在**的属性，PyGObject 会打
            # DeprecationWarning —— 弃用不是"不存在"，别让警告把日志淹掉。
            warnings.simplefilter("ignore")
            bad = [f"{mod}.{attr}  ({' '.join(sorted(set(where)))})"
                   for (mod, attr), where in sorted(seen.items())
                   if not hasattr(GI_MODULES[mod], attr)]
        self.assertEqual(bad, [], "这些 gi 模块级属性在真 PyGObject 上不存在：\n  " + "\n  ".join(bad))



# ---------------------------------------------------------------------------
# ⑤ 实例方法也要核：`display.xxx` 必须真的在 Gdk.Display 上存在
# ---------------------------------------------------------------------------
DISPLAY_ASSIGN = "Gdk.Display.get_default()"


def display_methods_that_do_not_exist(source: str, display_cls) -> list[str]:
    """返回 `display.<方法>` 里那些**在真 `Gdk.Display` 上不存在**的（纯函数：判据与反例具型共用）。

    只认"从 `Gdk.Display.get_default()` 赋出来的那个变量名"，别的同名变量不碰 ——
    宁可漏（少抓一处）也不要误报（那会让人为了绿去改无关代码）。
    """
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not isinstance(func, ast.Attribute) or func.attr != "get_default":
            continue
        # 形状：<什么>.get_default()，且整条表达式文本里有 Gdk.Display
        if not any(isinstance(t, ast.Name) and t.id == "Gdk" for t in ast.walk(func.value)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in names and not hasattr(display_cls, node.attr):
            bad.append(f"line {node.lineno}: {node.value.id}.{node.attr}() "
                       f"在 {display_cls.__name__} 上不存在")
    return bad


@unittest.skipUnless(HAVE_GI, "没有 gi：这条要拿真 Gdk.Display 类 hasattr")
class TestNoGtk4ApisInGtk3Code(unittest.TestCase):
    """判据：GTK3 的地盘里不许出现只属于 GTK4 的实例方法。

    事故来源见文件头 ⑤：`display.get_clipboard()` 是 GTK4 的 API，GTK3 的 `Gdk.Display` 上没有，
    于是结果面板的「复制」按钮点了完全没反应（异常埋在 GTK 信号处理器里）。
    """

    def test_repo_never_calls_methods_missing_on_gdk_display(self):
        bad = []
        for name in ("capsplus", "settings", "qbar", "tray"):
            root = REPO / name
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                bad += [f"{path.relative_to(REPO)}: {msg}"
                        for msg in display_methods_that_do_not_exist(
                            path.read_text(encoding="utf-8"), Gdk.Display)]
        self.assertEqual(bad, [], "这些调用在本机 GTK3 上不存在（多半是抄了 GTK4 的写法）：\n  "
                                  + "\n  ".join(bad))

    def test_the_guard_catches_the_incident_line(self):
        """反例具型：2026-10-20 真机那行必须被抓到；合法的 GTK3 方法不许误报。"""
        self.assertFalse(hasattr(Gdk.Display, "get_clipboard"), "本机 Gdk.Display 居然有 get_clipboard？")
        src = (
            "def f():\n"
            "    display = Gdk.Display.get_default()\n"
            "    if display is None:\n"
            "        return\n"
            "    display.get_clipboard(Gdk.SELECTION_CLIPBOARD).set_text('x', -1)\n"
        )
        self.assertTrue(display_methods_that_do_not_exist(src, Gdk.Display), "判据抓不住那行事故代码")
        ok = (
            "def f():\n"
            "    display = Gdk.Display.get_default()\n"
            "    return display.get_primary_monitor() or display.get_monitor(0)\n"
        )
        self.assertEqual(display_methods_that_do_not_exist(ok, Gdk.Display), [], "合法用法被误报了")


if __name__ == "__main__":
    unittest.main()