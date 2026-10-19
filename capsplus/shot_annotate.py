# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""标注窗（第一批第 3 步）：cairo 画布 + 矩形/箭头/自由文字/马赛克 + 撤销重做 + 复制/存盘/钉屏。

顶层**不 import gi / cairo**（CI 不装它们）：`_gtk()` / `_cairo()` 里懒加载。
所以这一层的**画法**能在没有显示服务器的机器上判：`render()` 只吃一个 cairo 上下文，
判据把底图与图形渲染到 `ImageSurface` 上逐像素断言（`tests/test_shot_annotate.py`）。

四条定死的口径：

  1. **底图是 cairo 自己加载的 PNG**（`create_from_png`），不经过 numpy/PIL 胶水：
     cairo 的 ARGB32 是预乘 BGRA，自己拼字节十有八九会把颜色通道顺序搞错。
  2. **马赛克用"缩下去再放大"**：先按块降采样到小 surface，再用 `FILTER_NEAREST` 放大回来。
     这是唯一不引 numpy 也能得到"块状"效果的做法（判据断言**块内像素相同**）。
  3. **"自由文字"是点一下弹一个单行输入框**（`Gtk.Popover` + `Gtk.Entry`），Enter 落地。
     在画布上内联编辑光标是另一个量级的活，不在第一批里；这一点如实写进文档，不假装有。
  4. **窗口里显示的可能是缩放后的图**（整屏截图在本机是 4800×1080 逻辑像素，一比一放不下）：
     图形坐标一律存**图像坐标**，鼠标坐标按 `fit_scale` 换算，判据钉这个换算。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

COLOR_ANNOT = (0.90, 0.15, 0.15, 1.0)     # 标注默认红（Snipaste 也是这个习惯）
COLOR_SHADOW = (0.0, 0.0, 0.0, 0.55)
LINE_WIDTH = 3
ARROW_HEAD = 14
FONT_SIZE = 22
MOSAIC_BLOCK = 12
TEXT_LIMIT = 500

TOOLS = ("rect", "arrow", "text", "mosaic")
TOOL_LABELS = {"rect": "矩形", "arrow": "箭头", "text": "文字", "mosaic": "马赛克"}

ACTION_UNDO = "undo"
ACTION_REDO = "redo"
ACTION_COPY = "copy"
ACTION_SAVE = "save"
ACTION_PIN = "pin"
ACTION_CLOSE = "close"

CLIPBOARD_CMD = ("wl-copy", "--type", "image/png")


class Shape:
    """一个标注图形。`kind` ∈ TOOLS；`rect` 是**图像坐标**下的 `(x, y, w, h)`；`text` 只有文字用。"""

    def __init__(self, kind: str, rect, text: str = "", color=COLOR_ANNOT, width: int = LINE_WIDTH):
        assert kind in TOOLS, f"未知图形 {kind}"
        self.kind = kind
        self.rect = (int(rect[0]), int(rect[1]), max(1, int(rect[2])), max(1, int(rect[3])))
        self.text = text
        self.color = color
        self.width = width

    def __eq__(self, other):
        return (isinstance(other, Shape) and self.kind == other.kind and self.rect == other.rect
                and self.text == other.text)

    def __repr__(self):
        return f"Shape({self.kind}, {self.rect}, {self.text!r})"


class ShapeStack:
    """撤销/重做栈。**新画一笔就清掉"重做"那一段**（与所有画图软件一致）。"""

    def __init__(self):
        self._items = []
        self._index = 0        # 已完成（=要画）的个数

    def add(self, shape: Shape) -> None:
        del self._items[self._index:]
        self._items.append(shape)
        self._index = len(self._items)

    def undo(self) -> bool:
        if self._index == 0:
            return False
        self._index -= 1
        return True

    def redo(self) -> bool:
        if self._index >= len(self._items):
            return False
        self._index += 1
        return True

    def clear(self) -> None:
        self._items = []
        self._index = 0

    @property
    def shapes(self) -> list:
        return self._items[:self._index]

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return self._index < len(self._items)

    def __len__(self):
        return self._index


# ------------------------------------------------------------------------ 坐标与几何

def fit_scale(image_size, screen_size) -> float:
    """把图像塞进屏幕的缩放比（只缩不放：小图一比一显示，模糊比缩小更讨厌）。"""
    iw, ih = image_size
    sw, sh = screen_size
    if iw <= 0 or ih <= 0:
        return 1.0
    return min(1.0, sw / iw, sh / ih)


def to_image_coords(display_xy, scale: float) -> tuple:
    """窗口坐标 → 图像坐标（判据钉这个换算：错了标注就会整体偏）。"""
    if scale <= 0:
        return (0, 0)
    return (int(round(display_xy[0] / scale)), int(round(display_xy[1] / scale)))


def arrow_head(start, end, size: int = ARROW_HEAD) -> list:
    """箭头那三条边的三个顶点（纯几何，判据算得出来）。"""
    import math
    x0, y0 = start
    x1, y1 = end
    angle = math.atan2(y1 - y0, x1 - x0)
    left = (x1 - size * math.cos(angle - math.pi / 7), y1 - size * math.sin(angle - math.pi / 7))
    right = (x1 - size * math.cos(angle + math.pi / 7), y1 - size * math.sin(angle + math.pi / 7))
    return [(x1, y1), left, right]


# ---------------------------------------------------------------------------- 渲染

def _cairo():
    import cairo
    return cairo


def render(cr, base_surface, shapes, size=None) -> None:
    """把底图 + 全部图形画到 cairo 上下文上（**不碰 gi**，判据渲染到 ImageSurface 上断言像素）。"""
    cairo = _cairo()
    if size is None:
        size = (base_surface.get_width(), base_surface.get_height())
    cr.set_source_surface(base_surface, 0, 0)
    cr.paint()

    for shape in shapes:
        cr.set_source_rgba(*shape.color)
        cr.set_line_width(shape.width)
        x, y, w, h = shape.rect
        if shape.kind == "rect":
            cr.rectangle(x + 0.5, y + 0.5, max(w - 1, 1), max(h - 1, 1))
            cr.stroke()
        elif shape.kind == "arrow":
            cr.move_to(x, y + h)                     # 从选区左下 → 右上（拖出来的对角线方向）
            cr.line_to(x + w, y)
            cr.stroke()
            tri = arrow_head((x, y + h), (x + w, y))
            cr.move_to(*tri[0])
            for pt in tri[1:]:
                cr.line_to(*pt)
            cr.close_path()
            cr.fill()
        elif shape.kind == "text":
            cr.select_font_face("Sans")
            cr.set_font_size(FONT_SIZE)
            cr.move_to(x, y + FONT_SIZE)
            cr.show_text(shape.text or "")
        elif shape.kind == "mosaic":
            _draw_mosaic(cr, base_surface, shape.rect, size)


def _draw_mosaic(cr, base_surface, rect, size) -> None:
    """马赛克：把该区域按块降采样到小 surface，再用 `FILTER_NEAREST` 放大回去。

    放大用最近邻 ⇒ 块状；降采样用 cairo 默认滤波 ⇒ 每块是该区域的平均色（近似）。
    """
    cairo = _cairo()
    x, y, w, h = rect
    bw = max(1, (w + MOSAIC_BLOCK - 1) // MOSAIC_BLOCK)
    bh = max(1, (h + MOSAIC_BLOCK - 1) // MOSAIC_BLOCK)
    small = cairo.ImageSurface(cairo.FORMAT_ARGB32, bw, bh)
    crs = cairo.Context(small)
    crs.scale(bw / w, bh / h)
    crs.set_source_surface(base_surface, -x, -y)
    crs.paint()
    pattern = cairo.SurfacePattern(small)
    pattern.set_filter(cairo.FILTER_NEAREST)
    # ⚠ 光 `set_source(pattern)` 不够：SurfacePattern 的默认矩阵是**恒等**，小图会被 1:1 画在原点，
    # 于是什么马赛克都不会出现（像素判据第一次就把这条抓出来了）。必须显式做"小图 → 目标矩形"：
    # 平移到 (x,y) 再放大 (w/bw, h/bh)；放大用最近邻 ⇒ 块状。
    cr.save()
    cr.translate(x, y)
    cr.scale(w / bw, h / bh)
    cr.set_source(pattern)
    cr.rectangle(0, 0, bw, bh)
    cr.fill()
    cr.restore()


def render_to_surface(base_surface, shapes):
    """渲染成一张新 surface（存盘与复制都走它，保证"存下来的 == 屏幕上看到的"）。"""
    cairo = _cairo()
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, base_surface.get_width(),
                             base_surface.get_height())
    render(cairo.Context(out), base_surface, shapes)
    return out


def load_base(png_path):
    """底图由 cairo 自己加载（不引 PIL，也就不会踩通道顺序的坑）。"""
    return _cairo().ImageSurface.create_from_png(str(png_path))


# ------------------------------------------------------------------ 存盘 / 剪贴板 / 钉屏

def png_bytes(surface) -> bytes:
    """surface → PNG 字节（先落临时文件再读回来：pycairo 没有"写进内存/管道"的口子）。"""
    handle, name = tempfile.mkstemp(prefix="capsplus-annot-", suffix=".png")
    os.close(handle)
    tmp = Path(name)
    try:
        surface.write_to_png(str(tmp))
        return tmp.read_bytes()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def clipboard_command() -> list:
    """喂 PNG 给 `wl-copy`（Wayland）。命令本身是数据，判据直接断言它。"""
    return list(CLIPBOARD_CMD)


def copy_to_clipboard(data: bytes, runner=None, which=None) -> tuple:
    """复制到剪贴板；返回 `(ok, 说明)`。没有 wl-copy 时**如实说**，不假装成功。"""
    import shutil
    which = which or shutil.which
    if which("wl-copy") is None:
        return False, "✗ 这台机没有 wl-copy（Wayland 剪贴板工具没装）"
    runner = runner or (lambda argv, data: subprocess.run(argv, input=data, timeout=5,
                                                          capture_output=True))
    try:
        proc = runner(clipboard_command(), data)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"✗ wl-copy 失败：{exc}"
    if getattr(proc, "returncode", 0) != 0:
        err = getattr(proc, "stderr", b"") or b""
        return False, f"✗ wl-copy 退出码 {proc.returncode}：{err[:200].decode(errors='replace')}"
    return True, "✓ 已复制到剪贴板"


def save_named(base_surface, shapes, directory=None):
    """存盘到 `~/Pictures/Screenshots/shot-<时间戳>.png`；返回落点。"""
    from capsplus import shot_frame as SF
    directory = Path(directory) if directory else SF.save_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = SF.unique_path(directory, SF.shot_filename())
    render_to_surface(base_surface, shapes).write_to_png(str(path))
    return path


# ------------------------------------------------------------------------------ GTK 部分

def _gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    return Gtk, Gdk, GLib


def screen_size():
    """当前屏的逻辑尺寸（决定窗口里把图缩到多大）。"""
    _Gtk, Gdk, _GLib = _gtk()
    display = Gdk.Display.get_default()
    if display is None:
        return (1920, 1080)
    mon = display.get_primary_monitor() or display.get_monitor(0)
    g = mon.get_geometry()
    return (max(g.width - 80, 320), max(g.height - 160, 240))


class Annotator:
    """标注窗。除 `run()`/`_build()` 外的方法都不碰 gi，判据能直接调（见测试的 stub 版）。"""

    def __init__(self, base_surface, on_pin=None, screen=(1920, 1080), directory=None, clip=None):
        self.base = base_surface
        self._clip = clip or copy_to_clipboard      # 判据注入 stub：不许在测试里动明江的剪贴板
        self._loop = None
        self._win = None
        self.size = (base_surface.get_width(), base_surface.get_height())
        self.scale = fit_scale(self.size, screen)
        self.stack = ShapeStack()
        self.tool = "rect"
        self.on_pin = on_pin
        self.directory = directory
        self.drag_from = None
        self.pending = None            # 正在拖的那个图形（未提交）
        self.result = None             # "copy" / "save" / "pin" / "close"
        self.saved_path = None
        self.message = ""

    # ---- 纯逻辑：事件 → 动作 ----
    def shapes_to_draw(self) -> list:
        out = list(self.stack.shapes)
        if self.pending is not None:
            out.append(self.pending)
        return out

    def press(self, image_xy) -> None:
        self.drag_from = image_xy

    def drag(self, image_xy) -> None:
        if self.drag_from is None:
            return
        from capsplus.shot_frame import normalize_rect
        rect = normalize_rect(self.drag_from[0], self.drag_from[1], image_xy[0], image_xy[1])
        self.pending = Shape(self.tool, rect)

    def release(self) -> bool:
        """松手落地：太小的一笔直接丢掉（手抖点一下不该留一个 1×1 的框）。"""
        from capsplus.shot_frame import accept_rect
        self.drag_from = None
        shape, self.pending = self.pending, None
        if shape is None or (shape.kind != "text" and not accept_rect(shape.rect)):
            return False
        if shape.kind == "text":
            return False            # 文字等输入框里敲完再落地（见 commit_text）
        self.stack.add(shape)
        return True

    def commit_text(self, image_xy, text: str) -> bool:
        text = (text or "").strip()[:TEXT_LIMIT]
        if not text:
            return False
        self.stack.add(Shape("text", (image_xy[0], image_xy[1], 1, 1), text=text))
        return True

    def click(self, image_xy) -> str:
        """单击：文字工具要弹输入框，其余工具什么也不做。"""
        return "text-input" if self.tool == "text" else "none"

    def set_tool(self, tool: str) -> None:
        if tool in TOOLS:
            self.tool = tool

    def action(self, name: str) -> str:
        """工具栏/快捷键动作（判据直接调它，不需要窗口）。"""
        if name == ACTION_UNDO:
            self.stack.undo()
        elif name == ACTION_REDO:
            self.stack.redo()
        elif name == ACTION_COPY:
            ok, msg = self._clip(png_bytes(render_to_surface(self.base, self.stack.shapes)))
            self.message = msg
            if ok:
                self.result = "copy"
        elif name == ACTION_SAVE:
            self.saved_path = save_named(self.base, self.stack.shapes, self.directory)
            self.message = f"✓ 已存 {self.saved_path}"
            self.result = "save"
        elif name == ACTION_PIN:
            if self.on_pin:
                ok, msg = self.on_pin(render_to_surface(self.base, self.stack.shapes))
                self.message = msg
                if ok:
                    self.result = "pin"
            else:
                self.message = "✗ 没有钉屏回调（这条链路没接上）"
        elif name == ACTION_CLOSE:
            self.result = "close"
        return self.result or name

    # ---- GTK ----
    def _build(self):
        Gtk, Gdk, _GLib = _gtk()
        win = Gtk.Window(title=f"capsplus 标注 — {self.size[0]}×{self.size[1]}")
        win.set_default_size(max(int(self.size[0] * self.scale), 200),
                             max(int(self.size[1] * self.scale), 150) + 48)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.set_margin_start(4)
        bar.set_margin_end(4)

        self._buttons = {}
        first = None
        for tool in TOOLS:
            # GTK3 里"一组互斥的开关按钮"就是 RadioButton（`set_join_group` 是 GTK4 的 API，
            # GTK3 上调它会 `AttributeError` —— xvfb 界面判据当场抓到过）。
            # `set_mode(False)` 关掉圆点，看起来与普通开关按钮一样。
            btn = Gtk.RadioButton.new_with_label_from_widget(first, TOOL_LABELS[tool])
            btn.set_mode(False)
            if first is None:
                first = btn
            btn.set_active(tool == self.tool)
            btn.connect("toggled", self._on_tool_toggled, tool)
            bar.pack_start(btn, False, False, 0)
            self._buttons[tool] = btn
        for label, action in (("撤销", ACTION_UNDO), ("重做", ACTION_REDO),
                              ("复制", ACTION_COPY), ("存盘", ACTION_SAVE),
                              ("钉到屏幕上", ACTION_PIN), ("关闭", ACTION_CLOSE)):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self._on_action, action)
            bar.pack_end(btn, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(int(self.size[0] * self.scale), int(self.size[1] * self.scale))
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("button-release-event", self._on_release)
        box.pack_start(self.area, True, True, 0)
        win.add(box)
        win.connect("key-press-event", self._on_key)
        win.connect("destroy", lambda *_a: self._quit_if_needed())
        return win

    def _image_xy(self, event):
        return to_image_coords((event.x, event.y), self.scale)

    def _on_draw(self, _widget, cr):
        cr.scale(self.scale, self.scale)
        render(cr, self.base, self.shapes_to_draw())

    def _on_press(self, _widget, event):
        if event.button != 1:
            return False
        xy = self._image_xy(event)
        if self.click(xy) == "text-input":
            self._open_text_entry(xy)
            return True
        self.press(xy)
        return True

    def _on_motion(self, _widget, event):
        self.drag(self._image_xy(event))
        self.area.queue_draw()
        return True

    def _on_release(self, _widget, _event):
        if self.release():
            self.area.queue_draw()
        return True

    def _open_text_entry(self, image_xy):
        Gtk, _Gdk, _GLib = _gtk()
        entry = Gtk.Entry()
        entry.set_placeholder_text("输入文字后回车")
        pop = Gtk.Popover()
        pop.add(entry)
        pop.set_relative_to(self.area)
        pop.set_pointing_to(self.area.get_allocation())
        entry.connect("activate", lambda *_a: (self.commit_text(image_xy, entry.get_text()),
                                              pop.popdown(), self.area.queue_draw()))
        pop.show_all()
        pop.popup()

    def _on_tool_toggled(self, btn, tool):
        if btn.get_active():
            self.set_tool(tool)

    def _on_action(self, _btn, action):
        self.action(action)
        if self.result in ("save", "close"):
            self._quit()
        elif self.result == "pin":
            self._quit()
        else:
            self.area.queue_draw()

    def _on_key(self, _win, event):
        _Gtk, Gdk, _GLib = _gtk()
        name = Gdk.keyval_name(event.keyval) or ""
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if name == "Escape":
            self.action(ACTION_CLOSE)
            self._quit()
        elif ctrl and name in ("z", "Z"):
            self.action(ACTION_REDO if event.state & Gdk.ModifierType.SHIFT_MASK else ACTION_UNDO)
            self.area.queue_draw()
        elif ctrl and name in ("y", "Y"):
            self.action(ACTION_REDO)
            self.area.queue_draw()
        elif ctrl and name in ("c", "C"):
            self.action(ACTION_COPY)
        elif ctrl and name in ("s", "S"):
            self.action(ACTION_SAVE)
            self._quit()
        elif ctrl and name in ("p", "P"):
            self.action(ACTION_PIN)
            if self.result == "pin":
                self._quit()
        return True

    def _quit_if_needed(self):
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()

    def _quit(self):
        for win in (self._win,):
            if win is not None:
                win.destroy()

    def run(self) -> str:
        Gtk, _Gdk, GLib = _gtk()
        self._loop = GLib.MainLoop()
        self._win = self._build()
        self._win.show_all()
        self._win.present()
        self._loop.run()
        return self.result or "close"


def annotate(png_path, on_pin=None, screen=None, directory=None) -> tuple:
    """打开标注窗；返回 `(result, saved_path, message)`。"""
    base = load_base(png_path)
    an = Annotator(base, on_pin=on_pin, screen=screen or screen_size(), directory=directory)
    result = an.run()
    return result, an.saved_path, an.message