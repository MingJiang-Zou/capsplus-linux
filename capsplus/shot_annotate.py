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

import math
import os
import subprocess
import tempfile
from pathlib import Path

from capsplus.shot_panel import KIND_OCR, KIND_TRANSLATE, PanelModel
# 布局常量与 CSS 同源（**不在这里再写一份**：间距/尺寸只在 shot_style 里定义）
from capsplus.shot_style import (BAR_GAP, PRIMARY_KEYS, SECONDARY_KEYS, STATUS_FONT, SWATCH,
                                 TOOL_BTN, WIDTH_DOT)

COLOR_ANNOT = (0.90, 0.15, 0.15, 1.0)     # 标注默认红（Snipaste 也是这个习惯）
COLOR_SHADOW = (0.0, 0.0, 0.0, 0.55)
LINE_WIDTH = 3
ARROW_HEAD = 14
FONT_SIZE = 22
MOSAIC_BLOCK = 12
TEXT_LIMIT = 500
POLL_MS = 50                        # 后台结果回主循环的轮询间隔（见 _drain_mailbox：不用 idle_add）
CANVAS_MARGIN = 14                        # 截图四周的留白（中性底色衬托）
# 工具条两侧各留的边（窗口宽度下限里算上它）。实测：请求 1034px 的窗口拿到手是 **1032px**
# （GTK/X11 那 2px 的去向没查清），所以留宽一点，别让"装得下"变成刚好压线。
BAR_EDGE = 8
CANVAS_BG = (0.106, 0.106, 0.118)         # 画布中性底色（与 CSS `.cp-canvas` 同色）
HIGHLIGHT_ALPHA = 0.35                    # （旧口径，保留给显式传参的场景）
# 荧光笔就是**半透明黄**这一支：不跟随颜色选择 —— 之所以定死，是因为"深色底 + 深色笔"会调出
# 一条橄榄色，压在上面的字几乎读不出来（lead 看图抓到的）。黄 + α=0.6 在浅底与深底上都能读出字。
HIGHLIGHT_COLOR = (1.00, 0.85, 0.15, 0.70)
BADGE_RADIUS = 14
BADGE_FONT_SIZE = 16

TOOLS = ("rect", "ellipse", "arrow", "line", "highlight", "pen",
         "badge", "text", "mosaic", "eraser", "eyedropper")
TOOL_LABELS = {"rect": "矩形", "ellipse": "椭圆", "arrow": "箭头", "line": "直线",
               "highlight": "荧光笔", "pen": "自由笔", "badge": "序号",
               "text": "文字", "mosaic": "马赛克", "eraser": "橡皮", "eyedropper": "吸管"}

# 工具条分组（**组间细分隔线**，绘制类在左、标记类居中、编辑类靠右；判据断言"分组齐全且无重复"）
TOOL_GROUPS = (
    ("rect", "ellipse", "arrow", "line", "highlight", "pen"),
    ("badge", "text", "mosaic"),
    ("eraser", "eyedropper"),          # 工具组：橡皮与吸管都是"作用于已有画面"的
)

# 选中态手柄（8 个）与命中容差
HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
HANDLE_SIZE = 7            # 手柄方块边长（图像坐标；显示时会被 scale 放大）
SELECT_TOL = 5             # 点中某个标注的容差（图像坐标）
NUDGE_STEP = 1             # 方向键微调步长（Shift = ×10）
NUDGE_BIG = 10

ACTION_NUDGE = "nudge"
ACTION_DELETE = "delete"
ACTION_ENTER = "copy-close"
# 颜色与线宽可选（第二批"和一般截图工具一样"那条）。默认第一项 —— 与第一批的行为一致。
COLORS = (("红", (0.90, 0.15, 0.15, 1.0)),
          ("黄", (1.00, 0.82, 0.10, 1.0)),
          ("绿", (0.20, 0.75, 0.35, 1.0)),
          ("蓝", (0.20, 0.55, 0.95, 1.0)),
          ("黑", (0.05, 0.05, 0.05, 1.0)),
          ("紫", (0.62, 0.32, 0.88, 1.0)),
          ("橙", (1.00, 0.55, 0.10, 1.0)))     # 明江要求"颜色至少 6 个"（原 5 个不够）
WIDTHS = (2, 3, 6)

# ── 工具/颜色/线宽的**窗内快捷键**（2026-10-20 加，明江：工具条上"有几个按钮不知道什么意思"）──
# 这一段是**唯一一份定义**：按钮提示（`tool_tip`/`colour_tip`/`width_tip`）、状态栏那两行
# （`tool_key_hint`）、键处理（`tool_for_key`/`colour_for_key`/`width_for_key`）全都读它，
# 免得"提示里写 R、实际按 R 没反应"这种两头对不上。字母键不引 Ctrl，所以与 Ctrl+C/S/Z/Y/P 不冲突。
TOOL_KEYS = {"rect": "R", "ellipse": "E", "arrow": "A", "line": "L", "highlight": "H",
             "pen": "P", "badge": "B", "text": "T", "mosaic": "M", "eraser": "X",
             "eyedropper": "I"}
# 颜色按**顺序**占 1..7、线宽接着占 8/9/0（与工具条上从左到右的排列一致，不用记）
COLOR_KEYS = {name: str(idx + 1) for idx, (name, _rgba) in enumerate(COLORS)}
WIDTH_KEYS = {int(width): key for width, key in zip(WIDTHS, ("8", "9", "0"))}
# 工具条上"是什么"的短说明只在 `shot_icons.TIPS` 里有一份（`tool_tip()` 拼名字/键/说明三样）

ACTION_UNDO = "undo"
ACTION_REDO = "redo"
ACTION_COPY = "copy"
ACTION_SAVE = "save"
ACTION_PIN = "pin"
ACTION_OCR = "ocr"
ACTION_TRANSLATE = "translate"
ACTION_CLOSE = "close"

# 工具条上的动作分组（判据断言"齐全且无重复"）：撤销重做在最左、复制/存盘/贴图/关闭在最右
EDIT_ACTIONS = (ACTION_UNDO, ACTION_REDO)
ACTION_GROUP = (ACTION_COPY, ACTION_SAVE, ACTION_PIN, ACTION_CLOSE)

# 右键快捷菜单（Snipaste 的手感：复制 / 存盘 / 贴图 / 关闭）
CONTEXT_MENU = (("复制到剪贴板", ACTION_COPY), ("存盘", ACTION_SAVE),
                ("贴到屏幕上", ACTION_PIN), ("关闭", ACTION_CLOSE))

# 窗内键位（**只在这一份里定义**，提示条与事件处理都读它 —— 免得两头对不上）
KEY_ACTIONS = {"F2": ACTION_TRANSLATE, "F3": ACTION_PIN, "F4": ACTION_OCR}
HINT_UNFOCUSED = ("窗口没有焦点：F2/F3/F4 现在**不会**生效（这是提示，不是按了没反应）"
                  "—— 点一下窗口再按")


# --------------------------------------------------- 按钮提示（是什么 + 快捷键，**同一份表**）
def tool_tip(tool: str) -> str:
    """工具按钮的悬浮提示：`矩形（R）：拖一个矩形框`。

    名字读 `TOOL_LABELS`、键读 `TOOL_KEYS`、说明读 **`shot_icons.TIPS`**（不在这里再抄一份说明）
    —— 三样各只有一处定义，所以提示里写的键就是真按得响的键。
    """
    from capsplus.shot_icons import TIPS
    label = TOOL_LABELS.get(tool, tool)
    key = TOOL_KEYS.get(tool, "")
    head = f"{label}（{key}）" if key else label
    what = TIPS.get(tool, "")
    return f"{head}：{what}" if what and what != label else head


def colour_tip(name: str) -> str:
    """颜色圆点的提示：`颜色：红（1）：画出来的线/框用这个色`。"""
    key = COLOR_KEYS.get(name, "")
    return f"颜色：{name}（{key}）：画出来的线/框用这个色" if key else f"颜色：{name}"


def width_tip(width) -> str:
    """线宽圆点的提示：`线宽 3px（9）：越粗越显眼，马赛克不受它影响`。"""
    key = WIDTH_KEYS.get(int(width), "")
    body = f"线宽 {int(width)}px"
    return f"{body}（{key}）：越粗越显眼" if key else f"{body}：越粗越显眼"


def tool_key_hint() -> str:
    """工具/颜色/线宽的键位一栏（**从上面三张表现算**，所以永远不会和实现漂）。

    状态栏第二行与 `hint_text()` 都读它 —— 明江要的"键位提示保持与实现一致"就靠这条。
    """
    tools = " ".join(f"{TOOL_KEYS[t]}={TOOL_LABELS[t]}" for t in TOOLS if TOOL_KEYS.get(t))
    colours = " ".join(f"{key}={name}" for name, key in COLOR_KEYS.items())
    widths = " ".join(f"{key}={int(w)}px" for w, key in WIDTH_KEYS.items())
    return f"工具：{tools} · 颜色：{colours} · 线宽：{widths}"


def tool_for_key(name: str):
    """字母键 → 工具名（没这个键返回 None）。`TOOL_KEYS` 是唯一一份表。"""
    key = (name or "").lower()
    for tool, hot in TOOL_KEYS.items():
        if hot.lower() == key:
            return tool
    return None


def colour_for_key(name: str):
    """数字键 → 颜色 rgba（没这个键返回 None）。"""
    for colour, key in COLOR_KEYS.items():
        if key == (name or ""):
            return dict(COLORS)[colour]
    return None


def width_for_key(name: str):
    """数字键 → 线宽（没这个键返回 None）。"""
    for width, key in WIDTH_KEYS.items():
        if key == (name or ""):
            return width
    return None


HINT_FOCUSED = ("　·　".join(tuple(PRIMARY_KEYS) + tuple(SECONDARY_KEYS))
                + "　·　" + tool_key_hint())

CLIPBOARD_CMD = ("wl-copy", "--type", "image/png")


class Shape:
    """一个标注图形。`kind` ∈ TOOLS；`rect` 是**图像坐标**下的 `(x, y, w, h)`；`text` 只有文字用。"""

    points = None                  # 自由笔的点集（其余图元为 None）

    def __init__(self, kind: str, rect, text: str = "", color=COLOR_ANNOT, width: int = LINE_WIDTH,
                 points=None):
        assert kind in TOOLS, f"未知图形 {kind}"
        if kind == "highlight" and color == COLOR_ANNOT:
            color = HIGHLIGHT_COLOR                 # 默认（没显式指定颜色）就是半透明黄
        self.kind = kind
        self.rect = (int(rect[0]), int(rect[1]), max(1, int(rect[2])), max(1, int(rect[3])))
        self.text = text
        self.color = color
        self.width = width
        self.points = list(points) if points else None     # ⚠ 忘了这行 ⇒ 自由笔的点全丢（判据抓到）

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

    # ---- 下面三条给"可编辑的标注"用（拖动/缩放/橡皮）：按**身份**找，不按 __eq__ ──
    def index_of(self, shape) -> int:
        for idx, item in enumerate(self._items):
            if item is shape:
                return idx
        return -1

    def trim_redo(self) -> None:
        """任何**新**编辑都要清掉"重做"那一段（与画图软件一致）。"""
        del self._items[self._index:]

    def remove_at(self, idx: int):
        if not (0 <= idx < len(self._items)):
            return None
        shape = self._items.pop(idx)
        if idx < self._index:
            self._index -= 1
        return shape

    def insert_at(self, idx: int, shape) -> None:
        idx = max(0, min(idx, len(self._items)))
        self._items.insert(idx, shape)
        if idx <= self._index:
            self._index += 1

    def replace_at(self, idx: int, shape) -> None:
        if 0 <= idx < len(self._items):
            self._items[idx] = shape

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
    if name in ("Return", "KP_Enter"):
        return ACTION_ENTER                      # Snipaste 的手感：回车 = 复制并关
    if name in ARROW_KEYS:
        return ACTION_NUDGE                      # 微调选的标注（步长看 nudge_delta）
    if name in ("Delete", "BackSpace"):
        return ACTION_DELETE
    return None


ARROW_KEYS = ("Up", "Down", "Left", "Right")


def nudge_delta(name: str, shift: bool = False) -> tuple:
    """方向键 → `(dx, dy)`：1px；按住 Shift 是 10px（Snipaste 的手感）。"""
    step = NUDGE_BIG if shift else NUDGE_STEP
    return {"Left": (-step, 0), "Right": (step, 0),
            "Up": (0, -step), "Down": (0, step)}.get(name, (0, 0))


def context_menu_items() -> tuple:
    """右键快捷菜单的条目（判据据此断言"四个动作都在、且没有重复"）。"""
    return CONTEXT_MENU


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


def render(cr, base_surface, shapes, size=None, selected=None, hover=None) -> None:
    """把底图 + 全部图形画到 cairo 上下文上（**不碰 gi**，判据渲染到 ImageSurface 上断言像素）。

    `selected` / `hover` 传**图形对象**（`Shape`）：选中的画描边 + 8 个手柄，hover 的画虚线框
    —— 这样"这东西能拖"一眼看得出来（明江说"没有描边和手柄，看不出可编辑"）。
    """
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
            start, end = shape_endpoints(shape)       # **方向按画的人定的**（不再写死对角线）
            cr.move_to(*start)
            cr.line_to(*end)
            cr.stroke()
            tri = arrow_head(start, end)
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
        elif shape.kind == "ellipse":
            x, y, w, h = shape.rect
            cr.save()
            cr.translate(x + w / 2.0, y + h / 2.0)
            cr.scale(max(w / 2.0, 0.5), max(h / 2.0, 0.5))
            cr.arc(0, 0, 1.0, 0, 2 * math.pi)
            cr.restore()
            cr.stroke()
        elif shape.kind == "line":
            start, end = shape_endpoints(shape)       # 同上：端点与方向都按实际拖拽
            cr.move_to(*start)
            cr.line_to(*end)
            cr.stroke()
        elif shape.kind == "pen":
            _draw_polyline(cr, shape)
        elif shape.kind == "badge":
            _draw_badge(cr, shape)
        elif shape.kind == "mosaic":
            _draw_mosaic(cr, base_surface, shape.rect, size)

    _draw_selection(cr, selected, hover)


DIRECTIONAL_TOOLS = ("arrow", "line")     # 有方向的：起点/终点不能靠"矩形对角线"猜


def shape_endpoints(shape) -> tuple:
    """箭头/直线的 `(起点, 终点)`。

    有 `points` 就用它（**方向是画的人定的**）；没有则退回"矩形左下 → 右上"——
    那是老数据的兼容路径（老图形没存方向），新画的图形一律走 points。
    """
    if shape.points and len(shape.points) >= 2:
        return (tuple(shape.points[0]), tuple(shape.points[-1]))
    x, y, w, h = shape.rect
    if shape.kind == "arrow":
        return ((x, y + h), (x + w, y))
    return ((x, y), (x + w, y + h))


def set_shape_endpoints(shape, start, end) -> None:
    """把端点写回图形：`points` 存方向，`rect` 存包围盒（正宽高，选择/手柄/裁剪都用它）。"""
    shape.points = [tuple(start), tuple(end)]
    shape.rect = _points_rect(shape.points)


def _point_segment_distance(point, start, end) -> float:
    """点到线段的距离（箭头/直线的命中判定用）。"""
    px, py = point
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    if dx == 0 and dy == 0:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / float(dx * dx + dy * dy)))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _shape_state(shape) -> tuple:
    """图形状态快照（撤销用）：矩形 + 点集。"""
    return (tuple(shape.rect), tuple(shape.points) if shape.points else None)


def _apply_shape_state(shape, state) -> None:
    rect, points = state
    shape.rect = tuple(rect)
    if points:
        shape.points = list(points)


def _points_rect(points) -> tuple:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(max(xs) - min(xs), 1), max(max(ys) - min(ys), 1))


def _delta(xy, origin) -> tuple:
    """拖动位移 = 当前点 − 按下点。**每帧都从按下点算**（不然会累积误差、越拖越飘）。"""
    return (int(round(xy[0] - origin[0])), int(round(xy[1] - origin[1])))


def draw_canvas(cr, canvas, base_surface, shapes, selected=None, hover=None) -> None:
    """画布：中性底色 + **居中的截图** + 标注（含选中描边/手柄、hover 虚线）。

    纯函数（不碰 gi）⇒ 判据能把它渲染到 ImageSurface 上逐像素断言：
    "截图居中、四周有留白、底色是中性的、选中手柄画出来了"。
    """
    cairo = _cairo()
    width, height = canvas["size"]
    cr.save()
    cr.set_source_rgb(*CANVAS_BG)
    cr.rectangle(0, 0, width, height)
    cr.fill()
    cr.restore()
    ix, iy, iw, ih = canvas["image"]
    base_w, base_h = base_surface.get_width(), base_surface.get_height()
    cr.save()
    cr.translate(ix, iy)
    cr.scale(iw / float(base_w), ih / float(base_h))
    cr.rectangle(0, 0, base_w, base_h)         # **裁到截图矩形**：标注不许画到留白区（导出也不带）
    cr.clip()
    render(cr, base_surface, shapes, size=(base_w, base_h), selected=selected, hover=hover)
    cr.restore()
    cr.save()
    cr.set_source_rgba(1, 1, 1, 0.28)          # 图像细边框：一眼看出截图边界
    cr.set_line_width(1)
    cr.rectangle(ix - 0.5, iy - 0.5, iw + 1, ih + 1)
    cr.stroke()
    cr.restore()


def window_plan(image_size, screen, bar_h=40, margin=CANVAS_MARGIN, gap=BAR_GAP,
                min_width=0) -> dict:
    """窗口规划：**先给工具条留一条带**（图像下方），再把截图缩放到剩下的空间里。

    这样"工具条不遮挡截图"在几何上就成立（不是靠运气）；只有连最小缩放都放不下时，
    `bar_layout` 才会退回"贴着图内下沿"，并被判据标成 `overlaps=True`。

    `min_width` = **工具条固有宽度 + 两边**（调用方用 `bar_min_window_width()` 算）。为什么要有它
    （2026-10-20 明江真机：260×420 的窄选区，工具条右端那一排颜色圆点被窗口黑边切掉）：
    窗口尺寸原来**只由截图算**，而工具条的宽度与截图无关（本机实测 1012px）——窄图必然算出一个
    比工具条窄的窗口，`bar_layout` 把条夹到 `x=2` 之后右端就伸到窗口外面去了。
    治本就是这条：**窗口宽度不得小于工具条需要的宽度**（明江给的三选一里的①）。
    图像仍在画布里居中，多出来的宽度就是两侧留白。
    """
    strip = int(bar_h) + int(gap) * 2
    avail = (max(screen[0] - margin * 2, 64), max(screen[1] - strip - margin * 2, 64))
    scale = fit_scale(image_size, avail)
    canvas = canvas_layout(image_size, scale, margin, reserve_bottom=strip, min_width=min_width)
    return {"scale": scale, "canvas": canvas, "window": canvas["size"], "strip": strip}


def _draw_polyline(cr, shape) -> None:
    """自由笔：把拖出来的点连成折线（点多的时候顺滑靠 round cap/join）。"""
    pts = list(shape.points or [])
    if len(pts) < 2:
        return
    cr.move_to(*pts[0])
    for pt in pts[1:]:
        cr.line_to(*pt)
    cr.stroke()


def shape_bounds(shape) -> tuple:
    """图形的最小外接矩形 `(x, y, w, h)`（自由笔用点集算，其余用 rect）。"""
    if shape.kind == "pen" and shape.points:
        xs = [p[0] for p in shape.points]
        ys = [p[1] for p in shape.points]
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    if shape.kind == "text":
        return (shape.rect[0], shape.rect[1],
                max(shape.rect[2], len(shape.text or "") * FONT_SIZE * 0.6),
                max(shape.rect[3], FONT_SIZE))
    return tuple(shape.rect)


def handles_for(bounds, size=HANDLE_SIZE) -> dict:
    """8 个手柄：四角 + 四边中点（**中心点坐标**）。"""
    x, y, w, h = bounds
    cx, cy = x + w / 2.0, y + h / 2.0
    return {"nw": (x, y), "n": (cx, y), "ne": (x + w, y), "e": (x + w, cy),
            "se": (x + w, y + h), "s": (cx, y + h), "sw": (x, y + h), "w": (x, cy)}


def handle_at(bounds, xy, size=HANDLE_SIZE) -> str:
    """点在哪个手柄上（没点中返回 ""）。容差按手柄边长的一半 + 2。"""
    tol = size / 2.0 + 2
    for name, (hx, hy) in handles_for(bounds, size).items():
        if abs(xy[0] - hx) <= tol and abs(xy[1] - hy) <= tol:
            return name
    return ""


def clamp_to_image(xy, image_size) -> tuple:
    """把坐标夹进截图矩形内 —— **留白区不许落笔**（明江看到的"能画到框外"就是这条没做）。"""
    width, height = image_size
    return (int(min(max(xy[0], 0), max(width - 1, 0))),
            int(min(max(xy[1], 0), max(height - 1, 0))))


def clamp_rect_to_image(rect, image_size) -> tuple:
    """把矩形裁进截图矩形内（保留宽高，往回收）—— 导出时自然只导出截图那一块。"""
    x, y, w, h = rect
    width, height = image_size
    w = int(min(max(w, 1), width))
    h = int(min(max(h, 1), height))
    x = int(min(max(x, 0), width - w))
    y = int(min(max(y, 0), height - h))
    return (x, y, w, h)


def clip_shape_to_image(shape, image_size) -> bool:
    """把某个图形的几何裁进截图矩形；返回是否被裁过。

    ⚠ 点集类图形（自由笔 / 箭头 / 直线）必须**先夹点、再由点算包围盒** —— 顺序反了的话
    `clamp_rect_to_image`（保持宽高往回收）会把已经算好的盒子再挪一次，方向跟着串。
    """
    before = (tuple(shape.rect), tuple(shape.points) if shape.points else None)
    if shape.points:
        shape.points = [clamp_to_image(pt, image_size) for pt in shape.points]
        shape.rect = _points_rect(shape.points)
    else:
        shape.rect = clamp_rect_to_image(shape.rect, image_size)
    return (tuple(shape.rect), tuple(shape.points) if shape.points else None) != before


def hit_test(shape, xy, tol=SELECT_TOL, inside=False) -> bool:
    """点是否落在某个标注上。

    `inside=False`（默认，用于**选中/拖动**）：空心图元只有靠近**边线**才算命中 —— 这样在已有的
    框里还能接着画新东西（画图软件的老规矩）。
    `inside=True`（用于**橡皮**）：只要在它的包围盒里就算命中（"点一下删掉那个标注"要够好点）。
    """
    if inside and shape.kind != "pen":
        x, y, w, h = shape_bounds(shape)
        return (x - tol <= xy[0] <= x + w + tol) and (y - tol <= xy[1] <= y + h + tol)
    if inside and shape.kind == "pen":
        pass
    if shape.kind == "pen" and shape.points:
        for pt in shape.points:
            if abs(pt[0] - xy[0]) <= tol + shape.width and abs(pt[1] - xy[1]) <= tol + shape.width:
                return True
        return False
    if shape.kind == "badge":
        x, y, w, h = shape.rect
        return (x - tol <= xy[0] <= x + w + tol) and (y - tol <= xy[1] <= y + h + tol)
    x, y, w, h = shape_bounds(shape)
    if shape.kind == "highlight":
        return (x - tol <= xy[0] <= x + w + tol) and (y - tol <= xy[1] <= y + h + tol)
    if shape.kind in DIRECTIONAL_TOOLS:
        # 箭头/直线是**斜的**：按"点到线段的距离"判命中。
        # 只看包围盒边线的话，点在斜线正中反而不算命中（2026-10-20 顺手修的）
        start, end = shape_endpoints(shape)
        return _point_segment_distance(xy, start, end) <= tol + shape.width
    # 线状/框状图元：按边线的距离判定（点空心处不算命中）
    near_x = min(abs(xy[0] - x), abs(xy[0] - (x + w))) <= tol + shape.width
    near_y = min(abs(xy[1] - y), abs(xy[1] - (y + h))) <= tol + shape.width
    inside_x = x - tol <= xy[0] <= x + w + tol
    inside_y = y - tol <= xy[1] <= y + h + tol
    if shape.kind == "ellipse":
        return inside_x and inside_y
    return (near_x and inside_y) or (near_y and inside_x)


def object_at(shapes, xy, tol=SELECT_TOL, inside=False) -> int:
    """最上面（最后画的）被点中的图形下标；没有返回 -1。"""
    for idx in range(len(shapes) - 1, -1, -1):
        if hit_test(shapes[idx], xy, tol, inside=inside):
            return idx
    return -1


def translate_rect(rect, dx, dy) -> tuple:
    return (rect[0] + dx, rect[1] + dy, rect[2], rect[3])


def resize_rect(rect, handle, xy) -> tuple:
    """拖手柄改大小：只有被拖的那条边/角会动，其余不变（最小 4px，别让它翻转成负的）。"""
    x, y, w, h = rect
    left, top, right, bottom = x, y, x + w, y + h
    if "w" in handle:
        left = min(xy[0], right - 4)
    if "e" in handle:
        right = max(xy[0], left + 4)
    if "n" in handle:
        top = min(xy[1], bottom - 4)
    if "s" in handle:
        bottom = max(xy[1], top + 4)
    return (left, top, right - left, bottom - top)


def shape_label(shape) -> str:
    """状态栏上"选中了什么"：名字 + 尺寸（比"选中"两个字有用得多）。"""
    x, y, w, h = shape_bounds(shape)
    name = TOOL_LABELS.get(shape.kind, shape.kind)
    if shape.kind == "text":
        return f"{name}「{(shape.text or '')[:12]}」"
    if shape.kind == "badge":
        return f"{name} {shape.text}"
    return f"{name} {int(round(w))}×{int(round(h))}"


def _draw_selection(cr, selected=None, hover=None) -> None:
    """选中的图形：明显描边 + 8 个手柄；hover 的图形：虚线框（"能拖"的反馈）。"""
    if hover is not None and hover is not selected:
        # **双色虚线**：浅色截图上白线看不见、深色截图上线看不见 —— 所以底下压一道深色、
        # 上面再画一道白色（判据抓到过"白底上画白虚线等于没画"）。
        bounds = shape_bounds(hover)
        _outline(cr, bounds, (0.0, 0.0, 0.0, 0.60), 1.0, dashes=(3, 3))
        _outline(cr, bounds, (1.0, 1.0, 1.0, 0.95), 1.0, dashes=(3, 3), offset=3)
    if selected is None:
        return
    bounds = shape_bounds(selected)
    _outline(cr, bounds, (0.15, 0.55, 1.0, 0.95), 1.5)
    for (hx, hy) in handles_for(bounds).values():
        cr.set_source_rgba(1, 1, 1, 1)
        cr.rectangle(hx - HANDLE_SIZE / 2.0, hy - HANDLE_SIZE / 2.0, HANDLE_SIZE, HANDLE_SIZE)
        cr.fill_preserve()
        cr.set_source_rgba(0.15, 0.55, 1.0, 0.95)
        cr.set_line_width(1.5)
        cr.stroke()


def _outline(cr, bounds, rgba, width, dashes=None, offset: int = 0) -> None:
    cr.save()
    cr.set_source_rgba(*rgba)
    cr.set_line_width(width)
    if dashes:
        cr.set_dash(dashes, offset)
    x, y, w, h = bounds
    cr.rectangle(x - 2, y - 2, w + 4, h + 4)
    cr.stroke()
    cr.restore()


def canvas_layout(image_size, scale=1.0, margin=CANVAS_MARGIN,
                  reserve_bottom=0, reserve_top=0, min_width=0) -> dict:
    """截图在画布里的位置：**居中 + 四周留白**（不是贴边画，那样看不出边界）。

    `reserve_bottom` / `reserve_top` 是给工具条/抽屉预留的带（上下各算一条，图像本身仍居中于
    剩余空间）。`min_width` 是**窗口宽度下限**（窄图时让画布跟着窗口变宽，图像仍在里面居中）——
    存在的理由见 `window_plan`。
    """
    img_w = max(int(round(image_size[0] * scale)), 1)
    img_h = max(int(round(image_size[1] * scale)), 1)
    canvas_w = max(img_w + int(margin) * 2, int(min_width))
    left = (canvas_w - img_w) // 2          # 图像**水平居中**：min_width 生效时不再固定等于 margin
    top = int(margin) + int(reserve_top)
    return {"image": (left, top, img_w, img_h), "scale": scale, "margin": int(margin),
            "size": (canvas_w,
                     img_h + int(margin) * 2 + int(reserve_top) + int(reserve_bottom))}


def bar_min_window_width(bar_width: int, edge: int = BAR_EDGE) -> int:
    """能让工具条**完整显示**的最小窗口宽度（`bar_layout` 会把条夹在 `[edge, wx-bw-edge]` 里）。"""
    return max(int(bar_width), 0) + int(edge) * 2


def bar_layout(canvas, bar_size, window_size, gap=BAR_GAP) -> dict:
    """工具条放哪：**优先图像下方**（不遮挡图像），下方放不下就翻到上方；
    上下都放不下才贴着图像内下沿（这种情况会标记 `overlaps=True`，判据据此断言"只在没办法时才遮"）。
    """
    ix, iy, iw, ih = canvas["image"]
    bw, bh = bar_size
    wx, wy = window_size
    x = ix + (iw - bw) / 2.0
    x = max(min(x, wx - bw - 2), 2)
    below = iy + ih + gap
    if below + bh <= wy - 2:
        return {"x": x, "y": below, "flipped": False, "overlaps": False}
    above = iy - gap - bh
    if above >= 2:
        return {"x": x, "y": above, "flipped": True, "overlaps": False}
    return {"x": x, "y": max(wy - bh - 2, 2), "flipped": False, "overlaps": True}


def bar_overlaps_image(bar, canvas) -> bool:
    """工具条有没有压到截图区域（判据：有留白可用时**必须**不压）。"""
    ix, iy, iw, ih = canvas["image"]
    bx, by, bw, bh = bar["x"], bar["y"], bar["w"], bar["h"]
    return not (bx + bw <= ix or bx >= ix + iw or by + bh <= iy or by >= iy + ih)


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


def render_to_surface(base_surface, shapes, selected=None, hover=None):
    """渲染成一张新 surface（存盘与复制都走它，保证"存下来的 == 屏幕上看到的"）。

    ⚠ 存盘/复制**不传** `selected`/`hover`：手柄与虚线是**编辑期的提示**，不该被存进图里。
    （判据用它们来断言"选中/hover 在像素上看得见"。）
    """
    cairo = _cairo()
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, base_surface.get_width(),
                             base_surface.get_height())
    render(cairo.Context(out), base_surface, shapes, selected=selected, hover=hover)
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


def copy_text_to_clipboard(text: str, runner=None, which=None) -> tuple:
    """把**文本**（色值那种）送进剪贴板：`wl-copy --type text/plain` + 同样的守护步规矩。

    与复制图片走同一条 runner（守护型命令不接管道，见 `_default_clipboard_runner` 的教训）。
    """
    import shutil
    which = which or shutil.which
    if which("wl-copy") is None:
        return False, "✗ 这台机没有 wl-copy（Wayland 剪贴板工具没装）"
    runner = runner or _default_clipboard_runner
    argv = list(CLIPBOARD_CMD) + ["--type", "text/plain"]
    try:
        proc = runner(argv, str(text).encode("utf-8"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"✗ wl-copy 失败：{exc}"
    if getattr(proc, "returncode", 0) != 0:
        return False, f"✗ wl-copy 退出码 {proc.returncode}"
    return True, f"✓ 已复制 {text}"


def copy_to_clipboard(data: bytes, runner=None, which=None) -> tuple:
    """复制到剪贴板；返回 `(ok, 说明)`。没有 wl-copy 时**如实说**，不假装成功。"""
    import shutil
    which = which or shutil.which
    if which("wl-copy") is None:
        return False, "✗ 这台机没有 wl-copy（Wayland 剪贴板工具没装）"
    runner = runner or _default_clipboard_runner
    try:
        proc = runner(clipboard_command(), data)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"✗ wl-copy 失败：{exc}"
    if getattr(proc, "returncode", 0) != 0:
        err = getattr(proc, "stderr", b"") or b""
        return False, f"✗ wl-copy 退出码 {proc.returncode}：{err[:200].decode(errors='replace')}"
    return True, "✓ 已复制到剪贴板"


def _default_clipboard_runner(argv, data):
    """喂 `wl-copy`：**只等父进程退出，绝不接管道的另一端**。

    ⚠ 2026-10-19 真机事故（明江："我多按几次 Ctrl+C 程序就会异常"）：`wl-copy` 会 fork 一个
    **长期存活的"留守者"**替你守剪贴板，而它 fork 前只把 stdin/stdout 换成 /dev/null，
    **stderr 原样继承** ⇒ 用 `capture_output=True` 就等于在等一条永不 EOF 的管道：
    实测每次复制 `ok=False`、**卡满 5 秒 timeout**（主线程被冻住，连按几次就堆成"程序异常"），
    而同一命令不接管道是 **rc=0、0.03 秒**。
    与 `voice_inject.run_step` 的"守护步"是同一条规矩（那边 2026-10-19 上午刚踩过同一个坑）：
    守护型命令不许用管道 EOF 当结束信号，输出直接丢给 DEVNULL（`returncode` 照样拿得到）。
    """
    return subprocess.run(argv, input=data, timeout=5,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        self.screen = screen           # 用来算"给工具条留出的那条带"（见 window_plan）
        self.drag_from = None
        self.pending = None            # 正在拖的那个图形（未提交）
        # ── 可编辑的标注（明江："没有描边和手柄，看不出可编辑"）──
        self.selected = None           # 选中的 Shape（**对象引用**，不进栈副本）
        self.hover = None              # 鼠标下的 Shape（虚线框反馈）
        self.edits = []                # 拖动/缩放/删除的编辑历史（撤销先走它）
        self.edit_index = 0
        self._drag_mode = None         # None / "move" / "resize:nw" / "draw"
        self._drag_shape = None
        self._drag_before = None
        self.result = None             # "copy" / "save" / "pin" / "close"
        self.saved_path = None
        self.message = ""

    # ---- 纯逻辑：事件 → 动作 ----
    def shapes_to_draw(self) -> list:
        out = list(self.stack.shapes)
        if self.pending is not None:
            out.append(self.pending)
        return out

    picked = ""            # 吸管取到的颜色（`#RRGGBB`，状态栏显示）

    def press(self, image_xy) -> None:
        """按下：**先看是不是在动已有标注**（拖动/缩放手柄），否则起一笔新的。"""
        if self.tool == "eyedropper":
            ok, hex_ = self.pick_colour(image_xy)      # 取色不产生图形、不进撤销栈
            if ok:
                self.message = f"✓ 取了颜色 {hex_}（接着画的图元就用它）"
            self._drag_mode = None
            return
        mode = self.start_drag(image_xy)
        self._drag_mode = mode
        self.drag_from = None if mode != "draw" else image_xy
        if mode == "draw" and self.tool == "pen":
            self.pending = Shape("pen", (image_xy[0], image_xy[1], 1, 1), color=self.color,
                                 width=self.width, points=[image_xy])

    def drag(self, image_xy) -> None:
        if self._drag_mode == "move":
            self.move_selected(*_delta(image_xy, self._drag_origin))
        elif self._drag_mode and self._drag_mode.startswith("resize:"):
            self.resize_selected(self._drag_mode.split(":", 1)[1], image_xy)
        elif self.drag_from is None:
            return
        elif self.tool == "pen":
            if self.pending is not None:
                self.pending.points.append(image_xy)
                self.pending.rect = _points_rect(self.pending.points)
        else:
            from capsplus.shot_frame import normalize_rect
            rect = normalize_rect(self.drag_from[0], self.drag_from[1], image_xy[0], image_xy[1])
            self.pending = Shape(self.tool, rect, color=self.color, width=self.width)
            if self.tool in DIRECTIONAL_TOOLS:
                # **方向要留住**：矩形只有正宽高，光靠它推不出"从哪拖到哪"
                # （2026-10-20 P0：起点到处跑、终点 y 不动就是这么来的）
                set_shape_endpoints(self.pending, self.drag_from, image_xy)

    def release(self) -> bool:
        """松手：拖动/缩放落地（记一笔编辑），画的那笔画进栈（太小的丢掉）。"""
        from capsplus.shot_frame import accept_rect
        mode, self._drag_mode = self._drag_mode, None
        self.drag_from = None
        if mode in ("move",) or (mode and mode.startswith("resize:")):
            shape = self._drag_shape
            if shape is not None and self._drag_before is not None:
                after = _shape_state(shape)
                if after != self._drag_before:
                    self._record_edit(shape, self._drag_before, after)
            return True
        shape, self.pending = self.pending, None
        if shape is None:
            return False
        if shape.kind == "text":
            return False            # 文字等输入框里敲完再落地（见 commit_text）
        if shape.kind == "pen":
            if len(shape.points or []) < 2 or not accept_rect(shape.rect):
                return False
        elif not accept_rect(shape.rect):
            return False
        clip_shape_to_image(shape, self.size)        # **落笔一律裁到截图范围内**
        self.stack.add(shape)
        self.selected = None
        return True

    # ---- 选中 / 拖动 / 缩放 / 删除（明江要的"看得出可编辑"）----
    def selected_bounds(self):
        return shape_bounds(self.selected) if self.selected is not None else None

    def select_at(self, image_xy, tol=SELECT_TOL):
        """点中某个标注 ⇒ 选中并返回下标；点空白 ⇒ 取消选中返回 -1。"""
        idx = object_at(self.stack.shapes, image_xy, tol)
        self.selected = self.stack.shapes[idx] if idx >= 0 else None
        return idx

    def hover_at(self, image_xy):
        idx = object_at(self.stack.shapes, image_xy, SELECT_TOL)
        self.hover = self.stack.shapes[idx] if idx >= 0 else None
        return idx

    def start_drag(self, image_xy) -> str:
        """按下时决定这次是什么动作：缩放手柄 > 拖动已有标注 > 画新的。"""
        if self.selected is not None:
            handle = handle_at(shape_bounds(self.selected), image_xy)
            if handle:
                self._drag_shape, self._drag_before = self.selected, _shape_state(self.selected)
                self._drag_origin = image_xy
                return f"resize:{handle}"
        idx = object_at(self.stack.shapes, image_xy, SELECT_TOL)
        if idx >= 0 and self.tool != "eraser":        # 橡皮交给 click()（点一下删，不拖）
            self.selected = self.stack.shapes[idx]
            self._drag_shape, self._drag_before = self.selected, _shape_state(self.selected)
            self._drag_origin = image_xy
            return "move"
        self._drag_shape, self._drag_before = None, None
        return "draw"

    def move_selected(self, dx, dy) -> bool:
        shape = self.selected
        if shape is None:
            return False
        shape.rect = translate_rect(shape.rect, dx, dy)
        if shape.points:
            shape.points = [(p[0] + dx, p[1] + dy) for p in shape.points]
        clip_shape_to_image(shape, self.size)        # 拖也不许拖出截图
        return True

    def resize_selected(self, handle, image_xy) -> bool:
        shape = self.selected
        if shape is None:
            return False
        old_rect = shape.rect
        new_rect = resize_rect(old_rect, handle, image_xy)
        shape.rect = new_rect
        if shape.points and shape.kind in DIRECTIONAL_TOOLS:
            # 端点按"旧盒子 → 新盒子"的比例映射：方向保持，端点跟着手柄走
            ox, oy, ow, oh = old_rect
            nx, ny, nw, nh = new_rect
            shape.points = [
                (int(round(nx + (p[0] - ox) * (nw / ow if ow else 1))),
                 int(round(ny + (p[1] - oy) * (nh / oh if oh else 1))))
                for p in shape.points]
        clip_shape_to_image(shape, self.size)
        return True

    def nudge_selected(self, dx, dy) -> bool:
        """方向键微调：1px；Shift=10px。**没选中就先提示**（不是默默不动）。"""
        if self.selected is None:
            self.message = "先点一下要挪的标注（方向键微调 1px，Shift=10px）"
            return False
        before = _shape_state(self.selected)
        if not self.move_selected(dx, dy):
            return False
        self._record_edit(self.selected, before, _shape_state(self.selected))
        return True

    def delete_selected(self) -> bool:
        """删掉选中的标注（Del 键 / 橡皮点一下）。删除也进编辑历史 ⇒ 撤销得回来。"""
        shape = self.selected
        if shape is None:
            return False
        idx = self.stack.index_of(shape)
        if idx < 0:
            return False
        self.stack.trim_redo()
        self.stack.remove_at(idx)
        self._record_edit(shape, _shape_state(shape), None, kind="delete", deleted_at=idx)
        self.selected, self.hover = None, None
        self.message = "✓ 已删掉那个标注（Ctrl+Z 撤销）"
        return True

    # ---- 编辑历史（撤销/重做先走它，再走画的那条栈）----
    def _record_edit(self, shape, before, after, kind="edit", deleted_at=None) -> None:
        del self.edits[self.edit_index:]
        self.edits.append({"shape": shape, "before": before, "after": after,
                           "kind": kind, "deleted_at": deleted_at})
        self.edit_index = len(self.edits)

    def undo_edit(self) -> bool:
        if self.edit_index == 0:
            return False
        self.edit_index -= 1
        self._apply_edit(self.edits[self.edit_index], forward=False)
        return True

    def redo_edit(self) -> bool:
        if self.edit_index >= len(self.edits):
            return False
        self._apply_edit(self.edits[self.edit_index], forward=True)
        self.edit_index += 1
        return True

    def _apply_edit(self, edit, forward: bool) -> None:
        """撤销/重做一笔编辑。

        ⚠ 两种编辑的形状不一样（第一版把"删除"和"改几何"混成一条分支，撤销删除时静默什么都没发生）：
          * `kind="edit"`（拖动/缩放/微调）：图形一直在栈里，只是几何变了 ⇒ 换回 before/after 的几何；
          * `kind="delete"`：图形已经从栈里**拿掉**了 ⇒ 撤销要按原下标 `insert_at` 插回去，
            重做要再 `remove_at`。
        """
        shape = edit["shape"]
        if edit.get("kind") == "delete":
            if forward:
                idx = self.stack.index_of(shape)
                if idx >= 0:
                    self.stack.remove_at(idx)
                if self.selected is shape:
                    self.selected = None
            elif self.stack.index_of(shape) < 0:
                self.stack.insert_at(edit.get("deleted_at") or 0, shape)
            return
        if self.stack.index_of(shape) < 0:
            return
        _apply_shape_state(shape, edit["after"] if forward else edit["before"])

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

    def pick_colour(self, image_xy) -> tuple:
        """吸管：取画布上那一个像素的颜色，设为当前绘制颜色；返回 `(ok, hex)`。

        颜色**从底图（截图）取**、不是从标注层取 —— 所以精确到那个像素。
        """
        x, y = int(image_xy[0]), int(image_xy[1])
        if not (0 <= x < self.size[0] and 0 <= y < self.size[1]):
            return False, ""
        try:
            self.base.flush()
            data = bytes(self.base.get_data())
            stride = self.base.get_stride()
            off = y * stride + x * 4
            rgb = (data[off + 2], data[off + 1], data[off])        # cairo 小端是 BGRA
        except Exception:
            return False, ""
        self.color = (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, 1.0)
        self.picked = "#%02X%02X%02X" % rgb
        return True, self.picked

    def click(self, image_xy) -> str:
        """单击：文字弹输入框、序号当场落一个圆标、**橡皮删掉点中的标注**，其余什么也不做。"""
        if self.tool == "text":
            return "text-input"
        if self.tool == "badge":
            self.place_badge(image_xy)
            return "badge-placed"
        if self.tool == "eraser":
            idx = object_at(self.stack.shapes, image_xy, SELECT_TOL, inside=True)
            if idx < 0:
                return "erase-miss"
            self.selected = self.stack.shapes[idx]
            self.delete_selected()
            return "erased"
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
            if not self.undo_edit():          # 先撤销"拖动/缩放/删除"这类编辑
                self.stack.undo()
        elif name == ACTION_REDO:
            if not self.redo_edit():
                self.stack.redo()
        elif name == ACTION_DELETE:
            return ACTION_DELETE if self.delete_selected() else ACTION_DELETE
        elif name == ACTION_ENTER:
            # Snipaste 的手感：回车 = 复制并关（复制成功才关，失败就留着让人看见那句提示）
            busy = render_to_surface(self.base, self.stack.shapes)
            ok, msg = self._clip(png_bytes(busy))
            self.message = msg
            if ok:
                self.result = "copy"
            return ACTION_ENTER
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
        from capsplus import shot_style
        from capsplus.shot_icons import render_icon
        from capsplus.shot_panel import PanelView

        shot_style.install()                        # 设计语言（跟随 GNOME 深浅色）
        bar_h = TOOL_BTN + shot_style.BAR_PAD * 2 + 6

        win = Gtk.Window(title=f"capsplus 标注 — {self.size[0]}×{self.size[1]}")
        win.get_style_context().add_class("cp-window")

        # **先建工具条、再算窗口尺寸**（顺序是这条修法的全部）：工具条的固有宽度与截图无关，
        # 窗口宽度必须**不小于它**，否则窄选区下工具条右端会被窗口切掉（2026-10-20 真机）。
        self._build_bar(Gtk, self._theme_colour(win, Gtk))
        bar_w = self._bar_intrinsic_width()
        plan = window_plan(self.size, self.screen, bar_h=bar_h,
                           min_width=bar_min_window_width(bar_w))
        self.plan = plan
        self.scale = plan["scale"]                  # 给工具条留出那条带之后重新算的缩放
        win.set_default_size(*plan["window"])

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(*plan["canvas"]["size"])
        self.area.get_style_context().add_class(shot_style.class_names_for("canvas"))
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("button-release-event", self._on_release)

        # 工具条**贴近截图但不遮挡**：默认放在图像下方那条预留带里（几何见 bar_layout）
        layout = bar_layout(plan["canvas"], (bar_w, bar_h), plan["window"])
        self._bar.set_halign(Gtk.Align.CENTER)
        self._bar.set_valign(Gtk.Align.START)
        self._bar.set_margin_top(int(layout["y"]))
        self._bar_layout = layout

        overlay = Gtk.Overlay()
        overlay.add(self.area)
        overlay.add_overlay(self._bar)

        # 结果抽屉与状态栏是**独立的一层**（明江："不许和工具条挤在同一行"）
        self._panel_view = PanelView(on_copy=lambda _m: self.copy_panel_text(),
                                     on_retranslate=lambda _m: self._on_action(None, ACTION_TRANSLATE),
                                     on_switch=lambda m: self._switch_panel_backend(),
                                     on_toggle_view=lambda: self._toggle_panel_view())
        drawer = self._panel_view.build()
        # `set_no_show_all` 是 GTK3 里唯一能挡住 `show_all()` 的开关 —— 只 `set_visible(False)`
        # 会被随后的 `win.show_all()` 重新点亮（xvfb 判据抓到过）。
        drawer.set_no_show_all(True)
        drawer.set_visible(False)

        # 状态栏**分两档**（lead："太密、信息层次没出来"）：主行给常用键，次要行收其余的键
        self._status = Gtk.Label(xalign=0)
        self._status.set_line_wrap(True)
        self._status.get_style_context().add_class("cp-status-main")
        self._status_more = Gtk.Label(xalign=0)
        self._status_more.set_line_wrap(True)
        self._status_more.get_style_context().add_class("cp-status-more")
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        status_box.pack_start(self._status, False, False, 0)
        status_box.pack_start(self._status_more, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.pack_start(overlay, True, True, 0)
        box.pack_start(drawer, False, False, 0)
        box.pack_start(status_box, False, False, 0)

        win.add(box)
        win.connect("key-press-event", self._on_key)
        win.connect("focus-in-event", self._on_focus, True)
        win.connect("focus-out-event", self._on_focus, False)
        win.connect("destroy", lambda *_a: self._quit_if_needed())
        self._build_menu(Gtk)
        self._sync_bar_state()
        self.refresh_status()
        return win

    # ---- 工具条：一条、居中、图标 + 分组 + 高亮 ----
    def _theme_colour(self, win, Gtk, selected: bool = False):
        """主题前景色（图标用它画 ⇒ 深浅色主题都看得清）。统一走 `shot_style.theme_colour`。"""
        from capsplus import shot_style
        return shot_style.theme_colour(win, selected=selected)

    def _bar_width(self) -> int:
        return self._bar_intrinsic_width()

    def _bar_intrinsic_width(self) -> int:
        """工具条**装进窗口**需要的宽度（含 CSS 的 padding / border）。

        `get_preferred_width()[1]`（natural）就是它，实测对得上每一项：
        **1026 = 33 个子控件 950 + 间距 2×32=64 + `.cp-bar` 的 padding 5×2 + border 1×2 = 950+64+12**
        （2026-10-20 在 xvfb 真窗口里逐项量过；同一套算法在另一轮量到 1024 = 948+64+12，也对得上）。
        ⚠ 别改成"只把子控件宽度加起来"（=1014）：那会**少算 CSS 那 12px**，工具条右端仍会被窗口切掉
        6px —— 真窗口判据 `TestNarrowWindowInARealWindow` 抓到的就是这个。
        """
        try:
            natural = int(self._bar.get_preferred_width()[1])
        except Exception:
            natural = 0
        if natural > 0:
            return natural
        try:                                    # 兜底：至少把按钮本身算全（不含 CSS 的量，宁可窄一点）
            children = self._bar.get_children()
            spacing = self._bar.get_spacing()
            return sum(c.get_preferred_width()[1] for c in children) + spacing * max(0, len(children) - 1)
        except Exception:
            return 720

    def _icon_button(self, Gtk, icon, tip, callback, kind="action"):
        from capsplus import shot_style
        from capsplus.shot_icons import render_icon
        btn = Gtk.Button()
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_tooltip_text(tip)
        self._attach_hover_hint(btn, tip)
        # 图标颜色按**这个按钮自己的底色**挑（浅底深图标/深底浅图标）—— 主按钮底色是选中色，
        # 赌主题变量会得到"白图标 + 白底 = 空方块"（窗口图抓到过）。
        # 主按钮上的图标画粗一档：底色是"选中色"，细线在浅底上容易被看成一个空方块
        line = 2.4 if kind == "primary" else 1.8
        surface = render_icon(icon, TOOL_BTN - 8, shot_style.readable_colour(btn, self._icon_rgba),
                              line=line)
        if surface is not None:
            btn.set_image(Gtk.Image.new_from_surface(surface))
        else:                                   # 图标画不出来也别空着（判据会红）
            btn.set_label(tip[:2])
        for cls in shot_style.class_names_for(kind).split():
            btn.get_style_context().add_class(cls)
        btn.connect("clicked", callback)
        return btn

    def _attach_hover_hint(self, btn, tip: str) -> None:
        """把"鼠标停在这个按钮上"写进状态栏那条主行（**与悬浮提示同一句文案**，不另抄一份）。

        明江 2026-10-20："有几个按钮不知道什么意思" —— 提示气泡要等一秒才弹，状态栏是即时的；
        两条都从同一个 `tip` 来，所以不会一个说法一个样。移开就回到"当前工具"。
        """
        def enter(_w, _e):
            self._hover_hint = tip
            self.refresh_status()
            return False

        def leave(_w, _e):
            self._hover_hint = ""
            self.refresh_status()
            return False

        btn.connect("enter-notify-event", enter)
        btn.connect("leave-notify-event", leave)

    def _build_bar(self, Gtk, fg):
        from capsplus import shot_style
        from capsplus.shot_icons import TIPS, swatch_surface, width_dot_surface
        self._icon_rgba = fg
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for cls in shot_style.class_names_for("bar").split():
            bar.get_style_context().add_class(cls)
        self._buttons, self._swatches, self._widths, self._actions = {}, {}, {}, {}

        def add_sep():
            sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            sep.get_style_context().add_class(shot_style.class_names_for("sep"))
            bar.pack_start(sep, False, False, 0)

        # ── 编辑类：撤销 / 重做（**最左**）──
        for action, icon in ((ACTION_UNDO, "undo"), (ACTION_REDO, "redo")):
            btn = self._icon_button(Gtk, icon, TIPS[icon], lambda _b, a=action: self._on_action(None, a))
            bar.pack_start(btn, False, False, 0)
            self._actions[action] = btn
        add_sep()

        # ── 绘制类 / 标记类 / 编辑类：按组排，组间细分隔线 ──
        first = None
        for group in TOOL_GROUPS:
            for tool in group:
                btn = Gtk.RadioButton.new_from_widget(first)
                btn.set_mode(False)
                if first is None:
                    first = btn
                btn.set_relief(Gtk.ReliefStyle.NONE)
                btn.set_tooltip_text(tool_tip(tool))
                self._attach_hover_hint(btn, tool_tip(tool))
                surface = None
                try:
                    from capsplus.shot_icons import render_icon
                    surface = render_icon(tool, TOOL_BTN - 8, shot_style.readable_colour(btn, fg))
                except Exception:
                    surface = None
                if surface is not None:
                    btn.set_image(Gtk.Image.new_from_surface(surface))
                else:
                    btn.set_label(TOOL_LABELS.get(tool, tool))
                for cls in shot_style.class_names_for("tool").split():
                    btn.get_style_context().add_class(cls)
                btn.set_active(tool == self.tool)
                btn.connect("toggled", self._on_tool_toggled, tool)
                bar.pack_start(btn, False, False, 0)
                self._buttons[tool] = btn
            add_sep()

        # ── 颜色：色块圆点（选中加描边）──
        color_first = None
        for name, rgba in COLORS:
            btn = Gtk.RadioButton.new_from_widget(color_first)
            btn.set_mode(False)
            if color_first is None:
                color_first = btn
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.set_tooltip_text(colour_tip(name))
            self._attach_hover_hint(btn, colour_tip(name))
            surface = swatch_surface(rgba, SWATCH - 8)
            if surface is not None:
                btn.set_image(Gtk.Image.new_from_surface(surface))
            else:
                btn.set_label(name)
            for cls in shot_style.class_names_for("swatch").split():
                btn.get_style_context().add_class(cls)
            btn.set_active(rgba == self.color)
            btn.connect("toggled", self._on_color_toggled, rgba)
            bar.pack_start(btn, False, False, 0)
            self._swatches[name] = btn
        add_sep()

        # ── 线宽：粗细圆点（不写"2px"）──
        width_first = None
        for width in WIDTHS:
            btn = Gtk.RadioButton.new_from_widget(width_first)
            btn.set_mode(False)
            if width_first is None:
                width_first = btn
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.set_tooltip_text(width_tip(width))
            self._attach_hover_hint(btn, width_tip(width))
            surface = width_dot_surface(width, WIDTH_DOT - 8, shot_style.readable_colour(btn, fg))
            if surface is not None:
                btn.set_image(Gtk.Image.new_from_surface(surface))
            else:
                btn.set_label(str(width))
            for cls in shot_style.class_names_for("dot").split():
                btn.get_style_context().add_class(cls)
            btn.set_active(width == self.width)
            btn.connect("toggled", self._on_width_toggled, width)
            bar.pack_start(btn, False, False, 0)
            self._widths[width] = btn
        add_sep()

        # ── 动作类（**最右**）：复制是主按钮，关闭在最后 ──
        kinds = {ACTION_COPY: "primary"}
        for action, icon in ((ACTION_COPY, "copy"), (ACTION_SAVE, "save"),
                             (ACTION_PIN, "pin"), (ACTION_CLOSE, "close")):
            btn = self._icon_button(Gtk, icon, TIPS[icon],
                                    lambda _b, a=action: self._on_action(None, a),
                                    kind=kinds.get(action, "action"))
            bar.pack_start(btn, False, False, 0)
            self._actions[action] = btn
        self._bar = bar
        bar.show_all()
        return bar

    def _sync_bar_state(self) -> None:
        """把"当前工具/颜色/线宽"的高亮同步到控件上（CSS 类，不是改颜色）。"""
        for tool, btn in getattr(self, "_buttons", {}).items():
            self._set_active_class(btn, "tool", tool == self.tool)
        for (name, rgba), btn in zip(COLORS, getattr(self, "_swatches", {}).values()):
            self._set_active_class(btn, "swatch", rgba == self.color)
        for width, btn in getattr(self, "_widths", {}).items():
            self._set_active_class(btn, "dot", width == self.width)

    def _sync_radio_buttons(self, tool=None, colour=None, width=None) -> None:
        """把**快捷键**选中的工具/颜色/线宽同步到单选按钮上（不然按键改了状态、按钮还亮着旧的）。

        按钮自己的 `toggled` 处理器会顺手 `set_tool/set_color/set_width` 并同步高亮，
        所以这里只负责"按下那个键 = 按下那个按钮"这一件事（不重复实现一遍状态机）。
        """
        if tool and tool in getattr(self, "_buttons", {}):
            self._buttons[tool].set_active(True)
        if colour is not None:
            for name, btn in getattr(self, "_swatches", {}).items():
                if dict(COLORS).get(name) == colour:
                    btn.set_active(True)
        if width is not None and width in getattr(self, "_widths", {}):
            self._widths[width].set_active(True)

    @staticmethod
    def _set_active_class(btn, kind: str, active: bool) -> None:
        """把"选中态"的 CSS 类加上或摘掉（基础类始终保持）。

        第一版把"加/摘"写在一个条件里，结果旧工具的 `cp-btn-active` 摘不掉（真窗口判据当场红）。
        """
        from capsplus import shot_style
        ctx = btn.get_style_context()
        base = set(shot_style.class_names_for(kind).split())
        active_cls = set(shot_style.class_names_for(kind, active=True).split()) - base
        for cls in base:
            if not ctx.has_class(cls):
                ctx.add_class(cls)
        for cls in active_cls:
            if active and not ctx.has_class(cls):
                ctx.add_class(cls)
            elif not active and ctx.has_class(cls):
                ctx.remove_class(cls)

    def _build_menu(self, Gtk) -> None:
        """右键快捷菜单（Snipaste 的手感：复制 / 存盘 / 贴图 / 关闭）。"""
        menu = Gtk.Menu()
        for label, action in context_menu_items():
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _w, a=action: self._on_action(None, a))
            menu.append(item)
        menu.show_all()
        self._menu = menu

    def refresh_status(self) -> None:
        """状态栏：字号够大、对比度够，快捷键提示放这里（不再是微缩灰字）。

        主行带"当前工具（键）"或"悬停的那个按钮"（`_hover_hint`），次要行带工具/颜色/线宽的键位
        —— 两行都从 `TOOL_KEYS`/`COLOR_KEYS`/`WIDTH_KEYS` 现算，所以**不会和实现漂**。
        """
        if getattr(self, "_status", None) is None:
            return
        from capsplus import shot_style
        selected = shape_label(self.selected) if self.selected is not None else ""
        self._status.set_text(shot_style.status_text(
            self.focused, selected, self.message, picked=self.picked,
            tool=f"{TOOL_LABELS.get(self.tool, self.tool)}（{TOOL_KEYS.get(self.tool, '')}）",
            hover=getattr(self, "_hover_hint", "")))
        if getattr(self, "_status_more", None) is not None:
            self._status_more.set_text(shot_style.status_more(self.focused, extra=tool_key_hint()))

    # ---- 提示条 / 面板刷新（只在有窗口时才碰 gi）----
    def refresh_hint(self) -> None:
        """状态栏（**兼容旧名字**：第一批的判据调的是它）。"""
        self.refresh_status()

    def _on_focus(self, _win, _event, focused: bool):
        self.focused = bool(focused)
        self.refresh_status()
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
            self._sync_bar_state()

    def _on_width_toggled(self, btn, width):
        if btn.get_active():
            self.set_width(width)
            self._sync_bar_state()

    def _on_tool_toggled(self, btn, tool):
        if btn.get_active():
            self.set_tool(tool)
            self._sync_bar_state()
            if getattr(self, "area", None) is not None:
                self.area.queue_draw()

    def _image_xy(self, event):
        canvas = (getattr(self, "plan", None) or {}).get("canvas") or canvas_layout(
            self.size, self.scale)
        ix, iy, _iw, _ih = canvas["image"]
        # 夹进截图矩形：留白区按下去 = 按在边界上（**留白区不许落笔**）
        return clamp_to_image(to_image_coords((event.x - ix, event.y - iy), self.scale), self.size)

    def _on_draw(self, _widget, cr):
        canvas = (getattr(self, "plan", None) or {}).get("canvas") or canvas_layout(
            self.size, self.scale)
        draw_canvas(cr, canvas, self.base, self.shapes_to_draw(),
                    selected=self.selected, hover=self.hover)

    def _on_press(self, _widget, event):
        if event.button == 3:                       # 右键：快捷菜单（复制/存盘/贴图/关闭）
            self._popup_menu(event)
            return True
        if event.button != 1:
            return False
        xy = self._image_xy(event)
        what = self.click(xy)
        if what == "text-input":
            self._open_text_entry(xy)
            return True
        if what in ("badge-placed", "erased", "erase-miss"):
            self.selected = None if what == "erased" else self.selected
            self.refresh_status()
            self.area.queue_draw()          # 序号/橡皮是点一下就走，不用拖
            return True
        self.press(xy)                      # 里面会决定"拖动/缩放已有标注"还是"画新的"
        self.refresh_status()
        self.area.queue_draw()
        return True

    def _on_motion(self, _widget, event):
        xy = self._image_xy(event)
        if self._drag_mode:
            self.drag(xy)
        else:                               # 没在拖：更新 hover（"这东西能拖"的反馈）
            self.hover_at(xy)
        self.area.queue_draw()
        return True

    def _on_release(self, _widget, _event):
        self.release()
        self.refresh_status()
        self.area.queue_draw()
        return True

    def _popup_menu(self, event) -> None:
        menu = getattr(self, "_menu", None)
        if menu is None:
            return
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(None, None, None, None, event.button, getattr(event, "time", 0))

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

    def _on_action(self, _btn, action):
        self.action(action)
        self._refresh_panel()
        self.refresh_status()
        if self.result in ("save", "close", "pin"):
            self._quit()
        else:
            self.area.queue_draw()

    def _on_key(self, _win, event):
        """窗内键位：动作查 `key_command()`（**唯一一份定义**），这里只负责收尾。

        没被 `key_command` 接手的键再试**工具/颜色/线宽**那三张表（`TOOL_KEYS`/`COLOR_KEYS`/
        `WIDTH_KEYS`）—— 按钮提示里写着哪个键，这里就认哪个键（同一份表，不会对不上）。
        """
        _Gtk, Gdk, _GLib = _gtk()
        name = Gdk.keyval_name(event.keyval) or ""
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        action = key_command(name, ctrl=ctrl, shift=shift)
        if action is None and not ctrl:
            tool = tool_for_key(name)
            colour = colour_for_key(name)
            width = width_for_key(name)
            if tool or colour is not None or width is not None:
                if tool:
                    self.set_tool(tool)
                if colour is not None:
                    self.set_color(colour)
                if width is not None:
                    self.set_width(width)
                self._sync_bar_state()               # 高亮跟着键走（不然按了键看不出换了没）
                self._sync_radio_buttons(tool, colour, width)
                self.refresh_status()
                if getattr(self, "area", None) is not None:      # 没建窗时（判据直接调）不碰控件
                    self.area.queue_draw()
                return True
        if action is None:
            return False                    # 没接手的键让 GTK 照常处理（别吞掉）
        if action == ACTION_NUDGE:
            dx, dy = nudge_delta(name, shift)      # 方向键微调选中的标注（1px；Shift=10px）
            self.nudge_selected(dx, dy)
        elif action == ACTION_DELETE:
            self.delete_selected()
        else:
            self.action(action)
        self._refresh_panel()
        self.refresh_status()
        if self.result in ("save", "close", "pin", "copy") and action in (ACTION_ENTER, ACTION_COPY):
            self._quit()                    # 回车=复制并关（Ctrl+C 仍然只复制、不关）
        elif self.result in ("save", "close", "pin"):
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