# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""工具条图标：**用 cairo 自己画**，不依赖图标主题。

为什么不直接用图标主题（`Gtk.Image.new_from_icon_name`）：绘制类工具（矩形/椭圆/箭头/直线/荧光笔/自由笔）
在 Adwaita 里根本没有对应的符号图标；混用会出现"一半有图标、一半是文字"。自己画的好处还有两条：
① 与线宽/颜色同一套几何，看起来是一家人；② **可以在判据里逐像素断言**（画出来是空的、或两个图标
长得一模一样，判据当场红）。

约定：每个图标都在 `(0, 0, size, size)` 的方框里作画，四周留 `PAD_RATIO` 的空白；
用**当前 source 颜色**画（调用方设好颜色再调），线条统一 `line` 宽。
"""

from __future__ import annotations

PAD_RATIO = 0.20
DEFAULT_LINE = 1.8

# 工具条上出现的全部图标（判据据此断言"每个都有实现、且都画得出东西"）
ICON_NAMES = (
    "rect", "ellipse", "arrow", "line", "highlight", "pen",      # 绘制类
    "badge", "text", "mosaic",                                   # 标记类
    "eraser", "eyedropper", "undo", "redo",                      # 编辑类
    "copy", "save", "pin", "close",                              # 动作类
    "ocr", "translate", "check", "magnifier",                    # 辅助
)

# 工具 → 图标（工具自身的名字就是图标名，这里只是留个显式的对照表，防止改名时漏改）
TOOL_ICONS = {name: name for name in ("rect", "ellipse", "arrow", "line", "highlight",
                                      "pen", "badge", "text", "mosaic", "eraser",
                                      "eyedropper")}

# 每个图标的悬浮提示（工具条用，别写"2px"这种没人看得懂的东西）
# ⚠ 工具那 11 条是**"这东西是干什么的"**（`shot_annotate.tool_tip()` 会把名字与快捷键拼在前面，
#   拼出来的就是按钮上那句中文提示）——2026-10-20 用户："有几个按钮不知道什么意思"，所以这里
#   写的是动作而不是重复一遍名字（原来 `"rect": "矩形"` 拼出来只有「矩形（R）」，等于没说）。
TIPS = {
    "rect": "拖一个矩形框", "ellipse": "拖一个椭圆",
    "arrow": "从起点拖到终点，箭头指向拖的方向", "line": "拖一条直线",
    "highlight": "按住拖着划荧光笔（半透明黄，压在字上仍读得出来）", "pen": "按住拖着自由画",
    "badge": "点一下落一个序号圆标（1、2、3…自动递增）", "text": "点一下弹输入框，回车落地",
    "mosaic": "拖一块马赛克（盖隐私用）", "eraser": "点一下删掉那个标注",
    "eyedropper": "点一下取那个像素的颜色（取色不改画面、不进撤销栈）",
    "undo": "撤销（Ctrl+Z）", "redo": "重做（Ctrl+Y）",
    "copy": "复制到剪贴板（Enter）", "save": "存盘（Ctrl+S）",
    "pin": "贴到屏幕上（F3）", "close": "关闭（Esc）",
    "ocr": "识别文字（F4）", "translate": "翻译（F2）",
}


def _arrow_head(cr, tip, angle, size):
    for delta in (-0.5, 0.5):
        cr.move_to(*tip)
        cr.line_to(tip[0] - size * 0.9 * __import__("math").cos(angle - delta),
                   tip[1] - size * 0.9 * __import__("math").sin(angle - delta))


def draw_icon(cr, name: str, size: float, line: float = DEFAULT_LINE) -> bool:
    """把 `name` 画进 `(0, 0, size, size)`（用当前 source 颜色）。不认识的名字返回 False。

    全部用 cairo 基本图元（圆弧/矩形/折线），**不依赖字体** —— 判据里没有字体也能跑。
    """
    import math
    s = float(size)
    pad = s * PAD_RATIO
    lo, hi = pad, s - pad
    mid = s / 2.0
    cr.save()
    cr.set_line_width(max(line, 1.0))
    cr.set_line_cap(1)          # round
    cr.set_line_join(1)         # round

    def box(x, y, w, hg):
        cr.rectangle(x, y, w, hg)

    if name == "rect":
        box(lo, lo + s * 0.04, hi - lo, (hi - lo) - s * 0.08)
        cr.stroke()
    elif name == "ellipse":
        cr.save()
        cr.translate(mid, mid)
        cr.scale(1.0, 0.72)
        cr.arc(0, 0, (hi - lo) / 2.0, 0, 2 * math.pi)
        cr.restore()
        cr.stroke()
    elif name in ("arrow", "line"):
        cr.move_to(lo, hi)
        cr.line_to(hi, lo)
        cr.stroke()
        if name == "arrow":
            _arrow_head(cr, (hi, lo), math.atan2(-1, 1), s * 0.16)
            cr.stroke()
    elif name == "highlight":
        cr.save()
        cr.set_line_width(s * 0.30)
        cr.set_line_cap(1)
        cr.move_to(lo, mid + s * 0.14)
        cr.line_to(hi, mid - s * 0.10)
        cr.stroke()
        cr.restore()
    elif name == "pen":
        cr.move_to(lo, hi)                       # 笔尖
        cr.line_to(mid - s * 0.06, mid + s * 0.06)
        cr.line_to(hi - s * 0.06, lo + s * 0.06)  # 笔杆
        cr.line_to(mid + s * 0.04, mid - s * 0.10)
        cr.close_path()
        cr.stroke()
    elif name == "badge":
        cr.arc(mid, mid, (hi - lo) / 2.0, 0, 2 * math.pi)
        cr.stroke()
        cr.move_to(mid - s * 0.02, mid + s * 0.10)      # 里面的"1"
        cr.line_to(mid + s * 0.06, mid - s * 0.12)
        cr.line_to(mid + s * 0.06, mid + s * 0.12)
        cr.stroke()
    elif name == "text":
        cr.move_to(lo, lo + s * 0.02)            # "T"：上横 + 竖
        cr.line_to(hi, lo + s * 0.02)
        cr.stroke()
        cr.move_to(mid, lo + s * 0.02)
        cr.line_to(mid, hi)
        cr.stroke()
    elif name == "mosaic":
        step = (hi - lo) / 2.0
        for ix in (0, 1):
            for iy in (0, 1):
                cr.rectangle(lo + ix * step + s * 0.02, lo + iy * step + s * 0.02,
                             step - s * 0.06, step - s * 0.06)
                cr.fill()
    elif name == "eyedropper":
        # 吸管：斜管身 + 尖头 + 顶上的球（与其它图标同一支笔触，画完自己 stroke）
        cr.move_to(lo + s * 0.10, hi - s * 0.10)
        cr.line_to(mid + s * 0.06, mid - s * 0.04)
        cr.stroke()
        cr.move_to(lo + s * 0.06, hi - s * 0.20)        # 尖头（一个小三角）
        cr.line_to(lo + s * 0.20, hi - s * 0.06)
        cr.line_to(lo + s * 0.10, hi - s * 0.10)
        cr.close_path()
        cr.fill()
        cr.arc(hi - s * 0.16, lo + s * 0.16, s * 0.13, 0, 2 * math.pi)
        cr.stroke()
    elif name == "eraser":
        cr.move_to(lo, hi - s * 0.12)            # 斜着的橡皮块
        cr.line_to(mid - s * 0.02, lo + s * 0.06)
        cr.line_to(hi - s * 0.06, lo + s * 0.20)
        cr.line_to(mid + s * 0.10, hi - s * 0.02)
        cr.close_path()
        cr.stroke()
        cr.move_to(mid - s * 0.02, lo + s * 0.06)   # 分界线
        cr.line_to(mid + s * 0.12, hi - s * 0.14)
        cr.stroke()
    elif name in ("undo", "redo"):
        flip = 1 if name == "undo" else -1
        cr.save()
        cr.translate(mid, mid)
        cr.scale(flip, 1)
        cr.arc(-s * 0.02, s * 0.02, s * 0.24, math.pi * 0.15, math.pi * 1.25)
        cr.stroke()
        _arrow_head(cr, (-s * 0.26, -s * 0.12), math.pi * 0.75, s * 0.13)
        cr.stroke()
        cr.restore()
    elif name == "copy":
        box(lo, lo, hi - lo - s * 0.10, hi - lo - s * 0.10)
        cr.stroke()
        box(lo + s * 0.12, lo + s * 0.12, hi - lo - s * 0.10, hi - lo - s * 0.10)
        cr.stroke()
    elif name == "save":
        cr.move_to(mid, lo)                      # 下箭头
        cr.line_to(mid, mid + s * 0.06)
        cr.stroke()
        _arrow_head(cr, (mid, mid + s * 0.06), math.pi / 2, s * 0.14)
        cr.stroke()
        cr.move_to(lo, hi - s * 0.06)            # 托盘
        cr.line_to(lo, hi)
        cr.line_to(hi, hi)
        cr.line_to(hi, hi - s * 0.06)
        cr.stroke()
    elif name == "pin":
        cr.arc(mid, mid - s * 0.02, s * 0.20, 0, 2 * math.pi)   # 钉头
        cr.stroke()
        cr.move_to(mid - s * 0.16, mid - s * 0.02)              # 横杆
        cr.line_to(mid + s * 0.16, mid - s * 0.02)
        cr.stroke()
        cr.move_to(mid, mid + s * 0.18)                         # 针
        cr.line_to(mid, hi)
        cr.stroke()
    elif name == "close":
        cr.move_to(lo, lo)
        cr.line_to(hi, hi)
        cr.move_to(hi, lo)
        cr.line_to(lo, hi)
        cr.stroke()
    elif name == "ocr":
        span = (hi - lo) * 0.34                   # 四个角框
        for cx, sx in ((lo, 1), (hi, -1)):
            for cy, sy in ((lo, 1), (hi, -1)):
                cr.move_to(cx + sx * span, cy)
                cr.line_to(cx, cy)
                cr.line_to(cx, cy + sy * span)
        cr.stroke()
        cr.move_to(lo + s * 0.18, mid)            # 中间一小段"文字"
        cr.line_to(hi - s * 0.18, mid)
        cr.stroke()
    elif name == "translate":
        cr.move_to(lo, mid - s * 0.08)            # 上箭头 →
        cr.line_to(hi, mid - s * 0.08)
        cr.stroke()
        _arrow_head(cr, (hi, mid - s * 0.08), 0, s * 0.12)
        cr.stroke()
        cr.move_to(hi, mid + s * 0.10)            # 下箭头 ←
        cr.line_to(lo, mid + s * 0.10)
        cr.stroke()
        _arrow_head(cr, (lo, mid + s * 0.10), math.pi, s * 0.12)
        cr.stroke()
    elif name == "check":
        cr.move_to(lo, mid)
        cr.line_to(mid - s * 0.02, hi - s * 0.10)
        cr.line_to(hi, lo + s * 0.04)
        cr.stroke()
    elif name == "magnifier":
        cr.arc(mid - s * 0.06, mid - s * 0.06, s * 0.24, 0, 2 * math.pi)
        cr.stroke()
        cr.move_to(mid + s * 0.12, mid + s * 0.12)
        cr.line_to(hi, hi)
        cr.stroke()
    else:
        cr.restore()
        return False
    cr.restore()
    return True


def icon_size(name: str, size: float, line: float = DEFAULT_LINE) -> tuple:
    """给判据用的元信息：`(名字, 尺寸, 线宽)`。"""
    return (name, float(size), float(line))


def _cairo():
    import cairo                      # 懒加载：顶层不 import（无头 CI 没有 pycairo 也要能 import 本模块）
    return cairo


def render_icon(name: str, size: int, rgba, line: float = DEFAULT_LINE):
    """把图标画成一张 ARGB32 surface（给 `Gtk.Image.new_from_surface` 用）。失败返回 None。"""
    try:
        cairo = _cairo()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(size), int(size))
        cr = cairo.Context(surface)
        cr.set_source_rgba(*rgba)
        if not draw_icon(cr, name, int(size), line):
            return None
        surface.flush()
        return surface
    except Exception:
        return None


def swatch_surface(rgba, size: int = 16):
    """颜色圆点（色块）：一眼看出是哪个颜色，不用读文字。"""
    try:
        cairo = _cairo()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(size), int(size))
        cr = cairo.Context(surface)
        cr.set_source_rgba(*rgba)
        cr.arc(size / 2.0, size / 2.0, size / 2.0 - 1, 0, 6.2832)
        cr.fill()
        surface.flush()
        return surface
    except Exception:
        return None


def width_dot_surface(width: int, size: int = 16, rgba=(0.92, 0.92, 0.92, 1.0)):
    """线宽圆点：**用粗细表示**，不写"2px/3px/6px"文字。"""
    try:
        cairo = _cairo()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(size), int(size))
        cr = cairo.Context(surface)
        cr.set_source_rgba(*rgba)
        radius = max(min(size / 2.0 - 2, float(width) * 1.6), 1.5)
        cr.arc(size / 2.0, size / 2.0, radius, 0, 6.2832)
        cr.fill()
        surface.flush()
        return surface
    except Exception:
        return None
