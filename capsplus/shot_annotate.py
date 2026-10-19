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

from capsplus.shot_panel import KIND_OCR, KIND_TRANSLATE, PanelModel

COLOR_ANNOT = (0.90, 0.15, 0.15, 1.0)     # 标注默认红（Snipaste 也是这个习惯）
COLOR_SHADOW = (0.0, 0.0, 0.0, 0.55)
LINE_WIDTH = 3
ARROW_HEAD = 14
FONT_SIZE = 22
MOSAIC_BLOCK = 12
TEXT_LIMIT = 500
POLL_MS = 50                        # 后台结果回主循环的轮询间隔（见 _drain_mailbox：不用 idle_add）
HIGHLIGHT_ALPHA = 0.35                    # 荧光笔：半透明，压得住文字又能看见底下的内容
BADGE_RADIUS = 14
BADGE_FONT_SIZE = 16

TOOLS = ("rect", "arrow", "highlight", "badge", "text", "mosaic")
TOOL_LABELS = {"rect": "矩形", "arrow": "箭头", "highlight": "荧光笔", "badge": "序号",
               "text": "文字", "mosaic": "马赛克"}
# 颜色与线宽可选（第二批"和一般截图工具一样"那条）。默认第一项 —— 与第一批的行为一致。
COLORS = (("红", (0.90, 0.15, 0.15, 1.0)),
          ("黄", (1.00, 0.82, 0.10, 1.0)),
          ("绿", (0.20, 0.75, 0.35, 1.0)),
          ("蓝", (0.20, 0.55, 0.95, 1.0)),
          ("黑", (0.05, 0.05, 0.05, 1.0)))
WIDTHS = (2, 3, 6)

ACTION_UNDO = "undo"
ACTION_REDO = "redo"
ACTION_COPY = "copy"
ACTION_SAVE = "save"
ACTION_PIN = "pin"
ACTION_OCR = "ocr"
ACTION_TRANSLATE = "translate"
ACTION_CLOSE = "close"

# 窗内键位（**只在这一份里定义**，提示条与事件处理都读它 —— 免得两头对不上）
KEY_ACTIONS = {"F2": ACTION_TRANSLATE, "F3": ACTION_PIN, "F4": ACTION_OCR}
HINT_FOCUSED = ("F2 翻译 · F3 贴图 · F4 OCR · Ctrl+C 复制 · Ctrl+S 存盘 · "
                "Ctrl+Z 撤销 · Esc 关")
HINT_UNFOCUSED = ("窗口没有焦点：F2/F3/F4 现在**不会**生效（这是提示，不是按了没反应）"
                  "—— 点一下窗口再按")

CLIPBOARD_CMD = ("wl-copy", "--type", "image/png")


class Shape:
    """一个标注图形。`kind` ∈ TOOLS；`rect` 是**图像坐标**下的 `(x, y, w, h)`；`text` 只有文字用。"""

    def __init__(self, kind: str, rect, text: str = "", color=COLOR_ANNOT, width: int = LINE_WIDTH):
        assert kind in TOOLS, f"未知图形 {kind}"
        if kind == "highlight":                     # 荧光笔自带半透明，别指望调用方记得传 alpha
            color = (color[0], color[1], color[2], HIGHLIGHT_ALPHA)
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

def key_command(name: str, ctrl: bool = False, shift: bool = False):
    """键 → 动作。**窗内键位只在这一个地方定义**（提示条与事件处理都读它，免得两头对不上）。

    与第一批的键位不冲突：Esc 关、Ctrl+C 复制、Ctrl+S 存盘、Ctrl+Z / Ctrl+Shift+Z 撤销重做；
    第二批新增 F2 翻译 / F3 贴图 / F4 OCR（只在窗口有焦点时到得了 —— 见 `hint_text`）。
    `Enter` 在第一批是"选区确认"，标注窗里**不占用**（本函数对它返回 None）。
    """
    if ctrl:
        return {"c": ACTION_COPY, "s": ACTION_SAVE,
                "z": ACTION_REDO if shift else ACTION_UNDO,
                "y": ACTION_REDO, "p": ACTION_PIN}.get(name.lower())
    if name in KEY_ACTIONS:
        return KEY_ACTIONS[name]
    if name == "Escape":
        return ACTION_CLOSE
    return None


def hint_text(focused: bool = True) -> str:
    """底部提示条的文字。**失焦要有可读提示**：不能让人以为"按了没反应"。"""
    return HINT_FOCUSED if focused else HINT_UNFOCUSED


def normalize_result(res) -> dict:
    """把 provider 的返回值归一化 —— 少字段、None、异常对象都不许让面板崩。

    约定（step B 的 provider 照这个返回）：
      `{"ok": bool, "text": str, "src": str, "backend": "local"|"cloud", "ms": int,
        "note": str, "hint": str}`
    面板只读这几个键；缺的一律给安全默认（`ok=False` + 一句可操作的话）。
    """
    out = {"ok": False, "text": "", "src": "", "backend": "", "ms": 0, "note": "", "hint": ""}
    if isinstance(res, dict):
        for key in out:
            if key in res and res[key] is not None:
                out[key] = res[key]
    out["ok"] = bool(out["ok"])
    try:
        out["ms"] = int(out["ms"])
    except (TypeError, ValueError):
        out["ms"] = 0
    if not out["ok"] and not out["hint"]:
        out["hint"] = "✗ 这一步失败了，但没带原因（provider 没写 hint）"
    return out


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
        elif shape.kind == "highlight":
            # 半透明色块：压一层就完事（不描边 —— 描边就不像荧光笔了）
            cr.set_source_rgba(*shape.color)
            cr.rectangle(x, y, w, h)
            cr.fill()
        elif shape.kind == "badge":
            _draw_badge(cr, shape)
        elif shape.kind == "mosaic":
            _draw_mosaic(cr, base_surface, shape.rect, size)


def _draw_badge(cr, shape) -> None:
    """序号圆标：实心圆 + 居中白字（"1、2、3…"那种，讲步骤时最常用）。"""
    cx = shape.rect[0] + BADGE_RADIUS
    cy = shape.rect[1] + BADGE_RADIUS
    cr.set_source_rgba(*shape.color)
    cr.arc(cx, cy, BADGE_RADIUS, 0, 2 * 3.141592653589793)
    cr.fill()
    cr.set_source_rgba(1, 1, 1, 1)
    cr.select_font_face("Sans")
    cr.set_font_size(BADGE_FONT_SIZE)
    text = shape.text or "1"
    extents = cr.text_extents(text)
    cr.move_to(cx - extents.width / 2 - extents.x_bearing,
               cy + extents.height / 2)
    cr.show_text(text)


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

    def __init__(self, base_surface, on_pin=None, screen=(1920, 1080), directory=None, clip=None,
                 on_ocr=None, on_translate=None):
        self.base = base_surface
        self._clip = clip or copy_to_clipboard      # 判据注入 stub：不许在测试里动明江的剪贴板
        self._loop = None
        self._win = None
        self.size = (base_surface.get_width(), base_surface.get_height())
        self.scale = fit_scale(self.size, screen)
        self.stack = ShapeStack()
        self.tool = "rect"
        self.color = COLORS[0][1]      # 当前颜色（默认红，与第一批一致）
        self.width = LINE_WIDTH        # 当前线宽
        self.focused = True            # 窗口有没有焦点（F 键只在有焦点时到得了）
        self.on_pin = on_pin
        self.on_ocr = on_ocr           # 第二步接上真实现；判据里注入 stub
        self.on_translate = on_translate
        self.on_translate_image = None     # 云端直读图（只有云有）；没有就退回"先 F4 再 F2"
        self.on_switch_backend = None      # 面板「切到云/本地」时通知接线层翻偏好
        self.threads = []                  # 后台线程（识别/翻译不该冻住界面）
        self.panel = PanelModel()
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
        self.pending = Shape(self.tool, rect, color=self.color, width=self.width)

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
        self.stack.add(Shape("text", (image_xy[0], image_xy[1], 1, 1), text=text,
                             color=self.color, width=self.width))
        return True

    def next_badge_number(self) -> int:
        """下一个序号 = 已画出的序号数 + 1（撤销掉一个之后接着用那个号，符合"重画这一笔"的直觉）。"""
        return 1 + sum(1 for s in self.stack.shapes if s.kind == "badge")

    def place_badge(self, image_xy) -> bool:
        number = self.next_badge_number()
        self.stack.add(Shape("badge", (image_xy[0] - BADGE_RADIUS, image_xy[1] - BADGE_RADIUS,
                                       BADGE_RADIUS * 2, BADGE_RADIUS * 2),
                             text=str(number), color=self.color, width=self.width))
        return True

    def click(self, image_xy) -> str:
        """单击：文字工具要弹输入框、序号工具当场落一个圆标，其余工具什么也不做。"""
        if self.tool == "text":
            return "text-input"
        if self.tool == "badge":
            self.place_badge(image_xy)
            return "badge-placed"
        return "none"

    def set_tool(self, tool: str) -> None:
        if tool in TOOLS:
            self.tool = tool

    def set_color(self, rgba) -> None:
        if rgba in [c[1] for c in COLORS]:
            self.color = rgba

    def set_width(self, width: int) -> None:
        if int(width) in WIDTHS:
            self.width = int(width)

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
        elif name in (ACTION_OCR, ACTION_TRANSLATE):
            # 直接返回 provider 的成败（这两件事**不关窗**，别被最后那行 `self.result or name` 吞掉）
            return self._run_text_action(name)
        elif name == ACTION_CLOSE:
            self.result = "close"
        return self.result or name

    # ---- OCR / 翻译（第二批）：provider 注入，面板只吃归一化后的结果 ----
    def rendered(self):
        """当前画面（带全部标注）—— OCR/翻译看的就是它，不是底图。"""
        return render_to_surface(self.base, self.stack.shapes)

    def _run_text_action(self, name: str, sync=None) -> bool:
        """跑一次识别/翻译。

        `sync=None` 时：**有主循环就走后台线程**（本机 27B 要一两秒、百度要几百毫秒，
        冻住 GTK 循环等于窗子卡死 —— 那正是要避免的）；判据里没有主循环，自动同步跑。
        """
        from capsplus.shot_panel import KIND_OCR, KIND_TRANSLATE
        if name == ACTION_OCR:
            provider, kind, arg = self.on_ocr, KIND_OCR, None
            if provider is None:
                self.panel.set_error("✗ 没接 OCR（这条链路没接上）")
                return False
            arg = self.rendered()
        else:
            provider, kind = self.on_translate, KIND_TRANSLATE
            if provider is None:
                self.panel.set_error("✗ 没接翻译（这条链路没接上）")
                return False
            source = self.panel.translate_input()      # 口径 ③：只认 OCR 原文
            if not source.strip():
                # 云多模态能**直接读图**（跳过 OCR）：偏好是云、又接了这条能力，就直接发图
                if self.on_translate_image is not None:
                    provider, arg = self.on_translate_image, self.rendered()
                    kind = KIND_TRANSLATE
                else:
                    self.panel.set_error("✗ 还没有可翻译的原文：先按 F4 识别一次"
                                         "（本机模型只吃文本；想直接读图就切到云）")
                    return False
            else:
                arg = source

        if sync is None:
            sync = self._loop is None
        if sync:
            return self._apply_text_result(kind, normalize_result(provider(arg)))
        self.panel.start(kind, self.panel.backend or "local")
        self._refresh_panel()

        # **信箱**模式：worker 线程只写一个 Python 变量，主循环用 timeout 每 50ms 来取。
        # 为什么不用 `GLib.idle_add(...)` 从 worker 里回主线程（更"标准"的写法）：PyGObject 下
        # 跨线程摸 GLib 主上下文会**把堆搞坏** —— 2026-10-19 判据连跑三轮就撞到两次
        # `corrupted size vs. prev_size` / `realloc(): invalid next size`（真机上是崩溃，不是报错）。
        # 信箱只多 50ms 延迟，但完全不让工作线程碰 GLib。
        self._mailbox = None

        def worker():
            try:
                self._mailbox = (kind, normalize_result(provider(arg)))
            except Exception as exc:                      # 线程里抛异常也要有交代
                self._mailbox = (kind, {"ok": False, "hint": f"✗ 后台线程炸了：{exc}",
                                        "src": "", "text": "", "backend": "", "ms": 0, "note": ""})

        GLib = _gtk()[2]
        # 排**一次**轮询（回调里自己再排下一次）：`GLib.timeout_add` 只从主线程调，安全
        GLib.timeout_add(POLL_MS, self._drain_mailbox)
        import threading
        thread = threading.Thread(target=worker, daemon=True)
        self.threads.append(thread)
        thread.start()
        return True

    def _drain_mailbox(self):
        """主循环里收结果（每 POLL_MS 看一次，直到收到为止）。返回 False = 不再排下一次。"""
        if self._mailbox is None:
            return True
        kind, res = self._mailbox
        self._mailbox = None
        self._apply_text_result(kind, res)
        self._refresh_panel()
        if getattr(self, "area", None) is not None:
            self.area.queue_draw()
        return False

    def _apply_text_result(self, kind, res) -> bool:
        from capsplus.shot_panel import KIND_OCR
        if res["ok"]:
            if kind == KIND_OCR:
                self.panel.set_ocr(res["text"], res["backend"], res["ms"], res["note"])
            else:
                self.panel.set_translation(res["src"], res["text"], res["backend"],
                                           res["ms"], res["note"])
        else:
            self.panel.set_error(res["hint"], res["note"])
        return res["ok"]

    def copy_panel_text(self) -> tuple:
        """面板上的"复制"：把当前该复制的那段喂给剪贴板（PGC：走注入的 clip，判据不动真剪贴板）。"""
        text = self.panel.primary_text()
        if not text.strip():
            self.panel.set_error("✗ 没有可复制的内容")
            return False, "✗ 没有可复制的内容"
        _Gtk, Gdk, GLib = _gtk()
        display = Gdk.Display.get_default()
        if display is None:
            return False, "✗ 连不上显示服务器，剪贴板不可用"
        display.get_clipboard(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.message = "✓ 已复制面板文本"
        return True, self.message

    # ---- GTK ----
    def _build(self):
        Gtk, Gdk, _GLib = _gtk()
        win = Gtk.Window(title=f"capsplus 标注 — {self.size[0]}×{self.size[1]}")
        win.set_default_size(max(int(self.size[0] * self.scale), 200),
                             max(int(self.size[1] * self.scale), 150) + 48)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.set_margin_top(4)
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
        bar2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar2.set_margin_start(4)
        bar2.set_margin_end(4)
        bar2.set_margin_bottom(4)

        # 颜色与线宽（第二批）：单选，默认第一项 = 第一批的红 3px
        self._color_buttons = {}
        color_first = None
        for name, rgba in COLORS:
            btn = Gtk.RadioButton.new_with_label_from_widget(color_first, name)
            btn.set_mode(False)
            if color_first is None:
                color_first = btn
            btn.set_active(rgba == self.color)
            btn.connect("toggled", self._on_color_toggled, rgba)
            bar2.pack_start(btn, False, False, 0)
            self._color_buttons[name] = btn
        self._width_buttons = {}
        width_first = None
        for width in WIDTHS:
            btn = Gtk.RadioButton.new_with_label_from_widget(width_first, f"{width}px")
            btn.set_mode(False)
            if width_first is None:
                width_first = btn
            btn.set_active(width == self.width)
            btn.connect("toggled", self._on_width_toggled, width)
            bar2.pack_start(btn, False, False, 0)
            self._width_buttons[width] = btn

        for label, action in (("撤销", ACTION_UNDO), ("重做", ACTION_REDO),
                              ("OCR", ACTION_OCR), ("翻译", ACTION_TRANSLATE),
                              ("复制", ACTION_COPY), ("存盘", ACTION_SAVE),
                              ("钉到屏幕上", ACTION_PIN), ("关闭", ACTION_CLOSE)):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self._on_action, action)
            bar2.pack_end(btn, False, False, 0)
        self._action_buttons = {}
        for child in bar2.get_children():
            if isinstance(child, Gtk.Button) and not isinstance(child, Gtk.RadioButton):
                self._action_buttons[child.get_label()] = child
        box.pack_start(bar, False, False, 0)
        box.pack_start(bar2, False, False, 0)

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(int(self.size[0] * self.scale), int(self.size[1] * self.scale))
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("button-release-event", self._on_release)
        box.pack_start(self.area, True, True, 0)

        # 结果面板（识别 / 翻译）：没结果时整块藏着，不占地方也不干扰标注
        from capsplus.shot_panel import PanelView
        self._panel_view = PanelView(on_copy=lambda _m: self.copy_panel_text(),
                                     on_retranslate=lambda _m: self._on_action(None, ACTION_TRANSLATE),
                                     on_switch=lambda m: self._switch_panel_backend(),
                                     on_toggle_view=lambda: self._toggle_panel_view())
        box.pack_start(self._panel_view.build(), False, False, 0)
        # `set_no_show_all` 是 GTK3 里唯一能挡住 `show_all()` 的开关 —— 只 `set_visible(False)`
        # 会被随后的 `win.show_all()` 重新点亮（xvfb 判据抓到过）。
        self._panel_view.widget.set_no_show_all(True)
        self._panel_view.widget.set_visible(False)

        # 底部按键提示条：**失焦时换成可读提示**（不能让人以为"按了没反应"）
        self._hint = Gtk.Label(xalign=0)
        self._hint.set_line_wrap(True)
        self._hint.set_margin_start(6)
        self._hint.set_margin_end(6)
        self._hint.set_margin_bottom(4)
        box.pack_start(self._hint, False, False, 0)
        self.refresh_hint()

        win.add(box)
        win.connect("key-press-event", self._on_key)
        win.connect("focus-in-event", self._on_focus, True)
        win.connect("focus-out-event", self._on_focus, False)
        win.connect("destroy", lambda *_a: self._quit_if_needed())
        return win

    # ---- 提示条 / 面板刷新（只在有窗口时才碰 gi）----
    def refresh_hint(self) -> None:
        if getattr(self, "_hint", None) is not None:
            self._hint.set_text(hint_text(self.focused))

    def _on_focus(self, _win, _event, focused: bool):
        self.focused = bool(focused)
        self.refresh_hint()
        return False

    def _refresh_panel(self) -> None:
        if getattr(self, "_panel_view", None) is None:
            return
        self._panel_view.update(self.panel)
        view = self._panel_view.widget
        if self.panel.kind or self.panel.message:
            # ⚠ GTK3 的 `no-show-all` 是**按控件**生效的：只要它还开着，`show_all()` 会连它
            # **一起跳过**（含子控件）—— 光把它置 visible 只会得到一个"有框没内容"的空白面板。
            # 所以要看的时候先把旗子放下再 show_all（xvfb 截图判据抓到的）。
            view.set_no_show_all(False)
            view.show_all()
        else:
            view.set_no_show_all(True)
            view.hide()

    def _switch_panel_backend(self) -> str:
        """面板上的「切到云/本地」：翻**偏好**并通知接线层，下一次 F2/F4 立刻走新后端。"""
        from capsplus.shot_panel import other_backend
        self.panel.backend = other_backend(self.panel.backend)
        if self.on_switch_backend:
            try:
                self.on_switch_backend(self.panel.backend)
            except Exception as exc:
                self.panel.set_error(f"✗ 切后端时出错：{exc}")
        self._refresh_panel()
        return self.panel.backend

    def _toggle_panel_view(self) -> str:
        from capsplus.shot_panel import KIND_OCR, KIND_TRANSLATE
        if self.panel.kind == KIND_TRANSLATE:
            self.panel.view = KIND_TRANSLATE if self.panel.view == KIND_OCR else KIND_OCR
            self._refresh_panel()
        return self.panel.view

    def _on_color_toggled(self, btn, rgba):
        if btn.get_active():
            self.set_color(rgba)

    def _on_width_toggled(self, btn, width):
        if btn.get_active():
            self.set_width(width)

    def _image_xy(self, event):
        return to_image_coords((event.x, event.y), self.scale)

    def _on_draw(self, _widget, cr):
        cr.scale(self.scale, self.scale)
        render(cr, self.base, self.shapes_to_draw())

    def _on_press(self, _widget, event):
        if event.button != 1:
            return False
        xy = self._image_xy(event)
        what = self.click(xy)
        if what == "text-input":
            self._open_text_entry(xy)
            return True
        if what == "badge-placed":
            self.area.queue_draw()          # 序号是点一下就走，不用拖
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
        self._refresh_panel()
        if self.result in ("save", "close", "pin"):
            self._quit()
        else:
            self.area.queue_draw()

    def _on_key(self, _win, event):
        """窗内键位：动作查 `key_command()`（**唯一一份定义**），这里只负责收尾。"""
        _Gtk, Gdk, _GLib = _gtk()
        name = Gdk.keyval_name(event.keyval) or ""
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        action = key_command(name, ctrl=ctrl, shift=shift)
        if action is None:
            return False                    # 没接手的键让 GTK 照常处理（别吞掉）
        self.action(action)
        self._refresh_panel()
        if self.result in ("save", "close", "pin"):
            self._quit()
        else:
            self.area.queue_draw()
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


def annotate(png_path, on_pin=None, screen=None, directory=None, on_ocr=None,
             on_translate=None, providers=None) -> tuple:
    """打开标注窗；返回 `(result, saved_path, message)`。

    `providers` 是 `shot_text.TextProviders`（或任何有同名方法的对象）：给了就用它，
    没给就退回显式的 `on_ocr`/`on_translate`（判据用后者注入桩）。
    """
    base = load_base(png_path)
    an = Annotator(base, on_pin=on_pin, screen=screen or screen_size(), directory=directory,
                   on_ocr=on_ocr, on_translate=on_translate)
    if providers is not None:
        an.on_ocr = providers.on_ocr
        an.on_translate = providers.on_translate
        an.on_translate_image = providers.on_translate_image
        an.on_switch_backend = providers.switch_translate
    result = an.run()
    return result, an.saved_path, an.message
    result = an.run()
    return result, an.saved_path, an.message