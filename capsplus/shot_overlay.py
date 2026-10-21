#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""选区遮罩（第二批 P0 重写）：**冻结帧当背景** + 全局坐标选区（可跨屏）+ 贴边吸附。

为什么重写（2026-10-19 用户真机：「框选的时候全黑屏是盲选，而且貌似无法全屏选择」）：
旧版是"全屏半透明窗 + 压暗 + 指望合成器把桌面透出来"。Wayland 下拿不到 ARGB 表面时那层
"透明"就是**全黑** ⇒ 盲选；而 xvfb 里（X11 有 ARGB 视觉）**永远复现不了**，所以当时的像素判据
全绿。现在：窗口背景 = **该屏的冻结图**（`capsplus/shot_frames.py` 抓的），**不透明**，
就算 alpha 一点都拿不到，用户看到的也是真实屏幕画面 —— 盲选**从结构上不可能再发生**。

同时把两件事一起治了：
  * **跨屏/整屏**：选区用**全局逻辑坐标**，拖出单屏也照收；每块屏只画自己那一块（按屏裁剪）。
    `Ctrl+A` = 整个联合矩形，双击 = 指针所在那块屏，拖到屏幕边缘自动吸附。
  * **不用二次抓帧**：确认后直接从冻结图裁剪（跨屏就每屏各裁一块再拼），
    所以旧版那套"藏窗 → settle → 再抓一帧"整段删掉了 —— 那个 settle 正是为了躲自己的遮罩，
    现在结构上不存在这个问题。

**绝不闪**（用户换掉 Snipaste 就是因为闪）：本文件没有一处动画。判据
（`tests/test_shot_overlay.py::TestNoAnimation`）在扫这件事。

顶层**不 import gi / cairo**（CI 不装 PyGObject 与 pycairo）：两边都在 `_gtk()` / `_cairo()` 里懒加载。
`paint()` 只吃一个 cairo 上下文，判据把它渲染到 `ImageSurface` 上**逐像素断言**。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 常量（判据按它们断言）

DIM_ALPHA = 0.35          # 选区外压暗的强度（压在**冻结图上**，所以再暗也看得见内容）
BORDER_WIDTH = 1
CORNER_LEN = 12           # 四角角标长度
CORNER_WIDTH = 2
HANDLE_SIZE = 7           # 四角 + 四边中点的小方块（拖着调大小用）
READOUT_PAD = 6
FONT_SIZE = 14
HINT_FONT_SIZE = 13
COLOR_DIM = (0.0, 0.0, 0.0, DIM_ALPHA)
COLOR_BORDER = (0.30, 0.85, 0.95, 1.0)
COLOR_HANDLE = (1.0, 1.0, 1.0, 0.95)
COLOR_READOUT_BG = (0.0, 0.0, 0.0, 0.72)
COLOR_READOUT_FG = (1.0, 1.0, 1.0, 1.0)
COLOR_CROSSHAIR = (1.0, 1.0, 1.0, 0.35)
FALLBACK_BG = (0.10, 0.10, 0.12, 1.0)      # 没有冻结图时的兜底底色（**不透明**，绝不画透明）
HINT_TEXT = ("拖动选择 · 双击整屏 · Ctrl+A 全选 · Enter/右键确认 · Esc 取消")

ACTION_IGNORE = "ignore"
ACTION_START = "start"
ACTION_UPDATE = "update"
ACTION_CONFIRM = "confirm"
ACTION_CANCEL = "cancel"
ACTION_FULL = "full"
ACTION_NUDGE = "nudge"                    # 方向键微调选区（1px；Shift=10px）            # Ctrl+A / 双击：直接给出整个联合矩形（或某块屏）
ACTION_SNAP = "snap"            # 松手时吸附到了屏幕边缘


# ------------------------------------------------------------------------ 纯逻辑（可单测）

def key_action(keyval_name: str, ctrl: bool = False):
    """键盘 → 动作。Esc 取消、Enter 确认、Ctrl+A 全选；其余不处理。

    ⚠ 刻意**不占用 Esc 之外的任何字母键**：遮罩窗会短暂持有键盘焦点，
    抢键等于偷用户的输入（`Caps+Esc` 那种"一不小心全关掉"的热键留在 keyd 侧，见贴图那条）。
    """
    if keyval_name == "Escape":
        return ACTION_CANCEL
    if keyval_name in ("Return", "KP_Enter"):
        return ACTION_CONFIRM
    if ctrl and keyval_name.lower() == "a":
        return ACTION_FULL
    if keyval_name in ("Up", "Down", "Left", "Right"):
        return ACTION_NUDGE                  # 方向键微调选区（1px；Shift=10px）
    if keyval_name.lower() == "c":
        # 提取颜色（用户 2026-10-20 要的）：复制光标下那一个像素的 `#RRGGBB`。
        # 只占 `C` **一个字母键**，且只在遮罩窗这几秒里有意义 —— 不碰别的键。
        return ACTION_COPY_COLOUR
    return None


# 放大镜（用户："从冻结帧取样，光标旁 80×80 + 坐标；我们已经有冻结帧，这个很便宜"）
MAG_SIZE = 80
MAG_ZOOM = 4
MAG_GAP = 16


def magnifier_enabled() -> bool:
    """放大镜开关（默认开；`CAPSPLUS_MAGNIFIER=0` 关掉 —— 嫌它挡视线时用）。"""
    import os
    return os.environ.get("CAPSPLUS_MAGNIFIER", "1").strip().lower() not in ("0", "false", "no", "off")


def nudge_rect(rect, dx: int, dy: int, union) -> tuple:
    """把选区按 (dx, dy) 挪一点，**夹在联合矩形里**（不许挪出屏幕）。"""
    x, y, w, h = rect
    max_x = union[0] + union[2] - w
    max_y = union[1] + union[3] - h
    return (int(min(max(x + dx, union[0]), max(max_x, union[0]))),
            int(min(max(y + dy, union[1]), max(max_y, union[1]))), w, h)


ACTION_COPY_COLOUR = "copy-colour"        # 按 C：把光标下的颜色复制走（提取颜色）


def hex_colour(rgb) -> str:
    """`(r, g, b)`（0–255）→ `#RRGGBB`（大写，跟设计/前端习惯一致）。"""
    return "#%02X%02X%02X" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def pixel_colour(frame, xy, area, scale: float = 0.0) -> tuple:
    """取冻结帧上某**逻辑坐标**的 RGB；取不到返回 `None`。

    坐标要经过"逻辑 → 帧像素"的映射：帧可能是 1.3333×（本机实测），窗口也可能有 scale。
    判据拿一张已知颜色的假冻结图来量（逐像素标尺），所以这里的换算必须精确。
    """
    if frame is None or xy is None:
        return None
    if scale <= 0:
        scale = (frame.get_width() / float(area[2])) if area[2] else 1.0
    px = int(min(max(xy[0] * scale, 0), frame.get_width() - 1))
    py = int(min(max(xy[1] * scale, 0), frame.get_height() - 1))
    frame.flush()
    data = bytes(frame.get_data())
    stride = frame.get_stride()
    off = py * stride + px * 4
    return (data[off + 2], data[off + 1], data[off])       # cairo ARGB32 在小端是 BGRA


def magnifier_source(frame_size, xy, size=MAG_SIZE, zoom=MAG_ZOOM):
    """放大镜该取**哪一块**、放大多少倍（纯函数，判据直接断言取样位置对不对）。

    取以光标为中心的一小块（边长 = size/zoom），边界贴住画面时夹住，保证不越界。
    """
    frame_w, frame_h = frame_size
    span = max(int(round(size / float(zoom))), 4)
    half = span / 2.0
    left = int(round(min(max(xy[0] - half, 0), max(frame_w - span, 0))))
    top = int(round(min(max(xy[1] - half, 0), max(frame_h - span, 0))))
    return {"src": (left, top, span, span), "zoom": zoom, "size": size}


def magnifier_position(xy, area, size=MAG_SIZE, gap=MAG_GAP) -> tuple:
    """放大镜放光标旁边：右/下放不下就翻到左/上（别把放大镜怼到屏幕外）。"""
    width, height = area[2], area[3]
    x = xy[0] + gap
    if x + size > width - 4:
        x = xy[0] - gap - size
    y = xy[1] + gap
    if y + size > height - 4:
        y = xy[1] - gap - size
    return (int(min(max(x, 4), max(width - size - 4, 4))),
            int(min(max(y, 4), max(height - size - 4, 4))))


def draw_colour_readout(cr, xy, rgb, area, size=(104, 26)) -> tuple:
    """在指针旁边画 `#RRGGBB` + 一块色样（遮罩阶段的"提取颜色"，按 `C` 复制）。

    贴在放大镜**旁边**；越界自动翻面并夹进绘制区域（与放大镜同一套避让思路）。
    """
    text = hex_colour(rgb)
    w, h = size
    x, y = xy[0] + 8, xy[1] + 8
    if x + w > area[0] + area[2]:
        x = xy[0] - w - 8
    if y + h > area[1] + area[3]:
        y = xy[1] - h - 8
    x = max(area[0], min(x, area[0] + area[2] - w))
    y = max(area[1], min(y, area[1] + area[3] - h))
    cr.save()
    cr.set_source_rgba(0.08, 0.08, 0.10, 0.88)
    cr.rectangle(x, y, w, h)
    cr.fill()
    cr.set_source_rgb(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    cr.rectangle(x + 3, y + 3, h - 6, h - 6)
    cr.fill()
    cr.set_line_width(1)
    cr.set_source_rgba(1, 1, 1, 0.5)
    cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
    cr.stroke()
    cr.set_source_rgb(0.97, 0.97, 0.97)
    cr.select_font_face("Sans")
    cr.set_font_size(12)
    cr.move_to(x + h + 4, y + h - 9)
    cr.show_text(text)
    cr.restore()
    # ⚠ 这里原来有一行 `cr.flush()` —— pycairo 的 `cairo.Context` **没有** flush 方法
    #   （只有 `cairo.Surface.flush()`；本文件另外两处 flush 都打在 Surface 上，是对的）。
    #   后果不是"这次没刷出去"，而是 **paint 处理器每帧抛 AttributeError**：
    #   遮罩画不出十字线/读数，框选看起来"完全没反应"（2026-10-20 用户真机报"截图有问题"、
    #   两次 Caps+F1 后只能去开 Snipaste）。删掉即可：GTK 的 draw 回调由 toolkit 自己提交。
    return (x, y, w, h)


def draw_magnifier(cr, frame, xy, area, size=MAG_SIZE, zoom=MAG_ZOOM) -> bool:
    """画放大镜：**从冻结帧取样**（不是重新抓屏，所以零成本、零延迟）+ 坐标读数。

    返回是否画了（拿不到帧就返回 False —— 调用方据此决定要不要画坐标）。
    """
    cairo = _cairo()                       # 本模块顶层不 import cairo（无头 CI 也要能 import）
    if frame is None:
        return False
    frame_w, frame_h = frame.get_width(), frame.get_height()
    if frame_w < 2 or frame_h < 2:
        return False
    info = magnifier_source((frame_w, frame_h), xy, size, zoom)
    zoom = info["zoom"]
    sx, sy, sw, sh = info["src"]
    dx, dy = magnifier_position(xy, area, size)
    cr.save()
    cr.rectangle(dx, dy, size, size)
    cr.clip()
    cr.translate(dx, dy)
    cr.scale(size / float(sw), size / float(sh))
    # **最近邻**：放大镜是用来对准像素的，双线性会把单像素糊成一片（判据："1 个像素该放大成
    # 约 zoom 个像素宽"就是被这条抓的）。
    cr.set_source_surface(frame, -sx, -sy)      # 取样偏移交给"源表面偏移"这一条，别自己拼矩阵
    cr.get_source().set_filter(cairo.FILTER_NEAREST)
    cr.paint()
    cr.restore()
    # 边框 + 中心十字（对准：光标的那个像素在放大镜正中间）
    cr.save()
    cr.set_source_rgba(1, 1, 1, 0.85)
    cr.set_line_width(2)
    cr.rectangle(dx, dy, size, size)
    cr.stroke()
    # 中心**方格**而不是十字线：十字线正好盖住你要对准的那个像素（第一版就是这样，判据抓到
    # "正中读不到那个红点"）。方格把中心那个像素框出来，看得见又不挡。
    half = size / 2.0
    box = max(zoom, 2)
    cr.set_source_rgba(1, 1, 1, 0.75)
    cr.set_line_width(1)
    cr.rectangle(dx + half - box / 2.0, dy + half - box / 2.0, box, box)
    cr.stroke()
    gap = box / 2.0 + 3
    cr.set_source_rgba(1, 1, 1, 0.45)
    cr.move_to(dx + half, dy + 5)
    cr.line_to(dx + half, dy + half - gap)
    cr.move_to(dx + half, dy + half + gap)
    cr.line_to(dx + half, dy + size - 5)
    cr.move_to(dx + 5, dy + half)
    cr.line_to(dx + half - gap, dy + half)
    cr.move_to(dx + half + gap, dy + half)
    cr.line_to(dx + size - 5, dy + half)
    cr.stroke()
    # 坐标读数（挂在放大镜下面）
    text = f"{int(xy[0])}, {int(xy[1])}"
    cr.select_font_face("Sans")
    cr.set_font_size(13)
    extents = cr.text_extents(text)
    box_w = extents.width + 12
    box_h = extents.height + 8
    box_x = min(max(dx + size / 2.0 - box_w / 2.0, 2), max(area[2] - box_w - 2, 2))
    box_y = dy + size + 4
    if box_y + box_h > area[3] - 2:
        box_y = dy - box_h - 4        # 下面放不下就挂上面
    cr.set_source_rgba(0, 0, 0, 0.72)
    cr.rectangle(box_x, box_y, box_w, box_h)
    cr.fill()
    cr.set_source_rgba(1, 1, 1, 0.95)
    cr.move_to(box_x + 6, box_y + box_h - 5)
    cr.show_text(text)
    cr.restore()
    return True


def button_action(button: int, has_selection: bool, double: bool = False) -> str:
    """鼠标 → 动作。左键拖 = 画；双击 = 整屏；右键 = 确认（没选区时算取消）。"""
    if button == 1:
        return ACTION_START
    if button == 3:
        return ACTION_CONFIRM if has_selection else ACTION_CANCEL
    return ACTION_IGNORE


def monitor_at(monitors, x: int, y: int):
    """点在**哪块屏**上（`monitors` 是全局逻辑坐标的 `[(x,y,w,h), …]`）。都不在返回 None。"""
    for idx, (mx, my, mw, mh) in enumerate(monitors):
        if mx <= x < mx + mw and my <= y < my + mh:
            return idx
    return None


def to_local(monitor, rect) -> tuple:
    """全局 → 该屏本地坐标（画图用）。"""
    return (rect[0] - monitor[0], rect[1] - monitor[1], rect[2], rect[3])


def to_global(monitor, rect) -> tuple:
    """该屏本地 → 全局。"""
    return (rect[0] + monitor[0], rect[1] + monitor[1], rect[2], rect[3])


def readout_text(rect) -> str:
    """实时读数：`宽 × 高`（Snipaste 也是这个习惯）。"""
    return f"{rect[2]} × {rect[3]}"


def coord_text(rect) -> str:
    """起点坐标 —— 对齐、报数时都要它（Snipaste 也在读数里显示）。"""
    return f"{rect[0]}, {rect[1]}"


def readout_position(rect, area) -> tuple:
    """读数框的位置：默认在选区**上方外侧**；贴到屏幕上边缘时挪到选区里面。"""
    x = rect[0]
    y = rect[1] - (FONT_SIZE + 2 * READOUT_PAD)
    if y < area[1]:
        y = rect[1] + READOUT_PAD
    if x < area[0]:
        x = area[0]
    return (x, y)


def hint_position(area) -> tuple:
    """提示条固定在窗口**底部居中**（放左上角会盖住内容，也不像截图工具）。"""
    w, h = text_box(HINT_TEXT, HINT_FONT_SIZE)
    ax, ay, aw, ah = area
    return (ax + max(8, (aw - w) // 2), ay + ah - h - 10)


def dim_bands(rect, area) -> list:
    """选区**外面**要压暗的四条带（上/下/左/右）。

    为什么是四条带而不是"整屏压暗 + 挖洞"：挖洞要靠 `OPERATOR_CLEAR` 之类的合成技巧，
    而**透明正是这次 P0 的病根**（拿不到 ARGB 就变黑）。四条带在像素上等价，判据一眼能算出来。
    """
    ax, ay, aw, ah = area
    x, y, w, h = rect
    x2, y2 = min(x + w, ax + aw), min(y + h, ay + ah)
    x, y = max(x, ax), max(y, ay)
    bands = []
    if y > ay:
        bands.append((ax, ay, aw, y - ay))                      # 上
    if y2 < ay + ah:
        bands.append((ax, y2, aw, ay + ah - y2))                # 下
    if x > ax:
        bands.append((ax, y, x - ax, y2 - y))                   # 左
    if x2 < ax + aw:
        bands.append((x2, y, ax + aw - x2, y2 - y))             # 右
    return [b for b in bands if b[2] > 0 and b[3] > 0]


def handle_points(rect) -> list:
    """八个可抓手柄：四角 + 四边中点（Snipaste 那种"拖角调大小"）。"""
    x, y, w, h = rect
    return [(x, y), (x + w / 2, y), (x + w, y),
            (x + w, y + h / 2), (x + w, y + h),
            (x + w / 2, y + h), (x, y + h), (x, y + h / 2)]


def handle_at(rect, xy, tolerance: int = HANDLE_SIZE) -> bool:
    """点在不在某个手柄上（命中判定，判据能直接调）。"""
    if not rect or rect[2] <= 0 or rect[3] <= 0:
        return False
    for hx, hy in handle_points(rect):
        if abs(hx - xy[0]) <= tolerance and abs(hy - xy[1]) <= tolerance:
            return True
    return False


def _cairo():
    import cairo
    return cairo


def text_box(text: str, font_size: int = FONT_SIZE) -> tuple:
    """一行文字的底色框尺寸（宽度按字数估：这层不引 Pango，用 cairo 的 toy 字体 API）。"""
    return (12 + int(font_size * 0.65) * len(text), font_size + 2 * READOUT_PAD)


def paint(cr, area, rect, frame=None, hint: bool = True, crosshair=None, handles: bool = True,
          magnifier=None, colour=None):
    """把遮罩画到 cairo 上下文上（**不碰 gi**，判据直接渲染到 ImageSurface 上断言像素）。

    像素口径（判据照这个算）：
      1. **背景必须是冻结图**（`frame`，与窗口同尺寸的 surface）：整块先铺它 —— 不透明，
         所以"全黑盲选"不可能出现；`frame=None` 时铺 `FALLBACK_BG`（不透明的近黑），
         **绝不铺透明**（透明 = 这次 P0 的病根）。
      2. 选区**外**压一层黑 α=DIM_ALPHA（压在冻结图上：暗了但内容看得见）。
      3. 选区内：1px 亮青描边 + 四角角标 + 八个白色手柄。
      4. 读数（宽 × 高 + 起点坐标）在选区上方外侧；`crosshair` 给定时画十字准线。
      5. `hint` 为真时底部居中画一行操作提示。
      6. `magnifier` 给定时（本地坐标 `(x, y)`）画**从冻结帧取样**的放大镜 + 坐标（最后画，压最上面）。
    """
    cairo = _cairo()
    ax, ay, aw, ah = area
    if frame is not None:
        # **显式映射**：帧的像素尺寸与绘制区域**不一定相等** ——
        # 2026-10-19 实测同一台机同一天：三屏联合（逻辑 4800×1080）回来的图是 **6400×1440（1.3333×）**，
        # 而单屏 (0,0,1920,1080) 回来的是 **1920×1080（1×）**；窗口的 scale_factor 还可能是 2。
        # 所以必须按"帧多大就铺多满"来缩放，不能假设 1:1（假设了就会只画出一角/被拉错位）。
        fw, fh = frame.get_width(), frame.get_height()
        if fw > 0 and fh > 0:
            cr.save()
            cr.translate(ax, ay)
            cr.scale(aw / fw, ah / fh)
            cr.set_source_surface(frame, 0, 0)
            cr.paint()
            cr.restore()
    else:
        cr.set_source_rgba(*FALLBACK_BG)
        cr.rectangle(ax, ay, aw, ah)
        cr.fill()

    has_rect = bool(rect and rect[2] > 0 and rect[3] > 0)
    bands = dim_bands(rect, area) if has_rect else [(ax, ay, aw, ah)]
    cr.set_source_rgba(*COLOR_DIM)
    for bx, by, bw, bh in bands:
        cr.rectangle(bx, by, bw, bh)
    cr.fill()

    if crosshair is not None:
        cx, cy = crosshair
        cr.set_source_rgba(*COLOR_CROSSHAIR)
        cr.set_line_width(1)
        cr.move_to(ax, cy + 0.5)
        cr.line_to(ax + aw, cy + 0.5)
        cr.move_to(cx + 0.5, ay)
        cr.line_to(cx + 0.5, ay + ah)
        cr.stroke()

    if hint:
        hx, hy = hint_position(area)
        hw, hh = text_box(HINT_TEXT, HINT_FONT_SIZE)
        cr.set_source_rgba(*COLOR_READOUT_BG)
        cr.rectangle(hx, hy, hw, hh)
        cr.fill()
        cr.set_source_rgba(*COLOR_READOUT_FG)
        cr.select_font_face("Sans")
        cr.set_font_size(HINT_FONT_SIZE)
        cr.move_to(hx + READOUT_PAD, hy + HINT_FONT_SIZE + READOUT_PAD - 3)
        cr.show_text(HINT_TEXT)

    if not has_rect:
        return

    x, y, w, h = rect
    cr.set_source_rgba(*COLOR_BORDER)
    cr.set_line_width(BORDER_WIDTH)
    cr.rectangle(x + 0.5, y + 0.5, max(w - 1, 1), max(h - 1, 1))
    cr.stroke()

    cr.set_line_width(CORNER_WIDTH)
    for cx, cy, dx, dy in ((x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        cr.move_to(cx, cy + dy * CORNER_LEN)
        cr.line_to(cx, cy)
        cr.line_to(cx + dx * CORNER_LEN, cy)
    cr.stroke()

    if handles:
        cr.set_source_rgba(*COLOR_HANDLE)
        for hx, hy in handle_points(rect):
            cr.rectangle(hx - HANDLE_SIZE / 2, hy - HANDLE_SIZE / 2, HANDLE_SIZE, HANDLE_SIZE)
        cr.fill()

    rx, ry = readout_position(rect, area)
    cr.set_source_rgba(*COLOR_READOUT_BG)
    cr.rectangle(rx, ry, text_box(readout_text(rect))[0],
                 text_box(readout_text(rect))[1] + FONT_SIZE + 4)
    cr.fill()
    cr.set_source_rgba(*COLOR_READOUT_FG)
    cr.select_font_face("Sans")
    cr.set_font_size(FONT_SIZE)
    cr.move_to(rx + READOUT_PAD, ry + FONT_SIZE + READOUT_PAD - 3)
    cr.show_text(readout_text(rect))
    cr.move_to(rx + READOUT_PAD, ry + 2 * FONT_SIZE + READOUT_PAD + 1)
    cr.set_source_rgba(0.75, 0.85, 0.95, 1.0)
    cr.show_text(coord_text(rect))

    if magnifier is not None:              # 放大镜最后画：压在所有东西上面
        draw_magnifier(cr, frame, magnifier, area)
    if colour is not None:                 # 提取颜色：色值读数贴在放大镜/准线旁边
        draw_colour_readout(cr, magnifier or crosshair or (area[0] + 8, area[1] + 8), colour, area)


# ------------------------------------------------------------------------------ GTK 部分

# ── 窗口几何（2026-10-19 P0 的病根就在这三行）────────────────────────────────
# 实测（xvfb 复现 lead 的真机数字）：
#   ① `realize()` → `fullscreen_on_monitor()` → `show_all()`  ⇒ device **200×200**
#      （200×200 是 GTK toplevel 的默认尺寸；mutter 把这张 stale 缓冲**拉伸铺满整块屏**，
#       于是"整屏纯黑 / 一块矩形、看不到画面"——正是用户看到的现象）
#   ② `set_default_size(mon_w, mon_h)` → `fullscreen_on_monitor()` → `show_all()` ⇒ device 1600×900 ✓
# 所以：**先给尺寸，再全屏，最后显示**；并且不要提前 `realize()`。
OVERLAY_MIN_SETTLE = 150          # map 之后等一小会儿再量尺寸（合成器要一帧提交）
OVERLAY_MAX_CHECKS = 10           # 自检重试次数（每次 ~150ms ⇒ 最多约 1.5s，防合成器慢造成误判）


def overlay_geometry(win, screen, idx, monitor) -> None:
    """把窗口**显式**设成该屏尺寸，再全屏到该屏。顺序不许改（见上面的实测）。"""
    win.set_default_size(int(monitor[2]), int(monitor[3]))
    win.fullscreen_on_monitor(screen, idx)


def geometry_expectation(monitor, scale: int = 1) -> tuple:
    """该屏**设备像素**下应当多大的窗口（逻辑尺寸 × 该屏 scale_factor）。"""
    return (int(monitor[2]) * int(scale), int(monitor[3]) * int(scale))


def acceptable_sizes(monitor, scale: int = 1) -> set:
    """这个窗口**可以**是多少像素。

    两种解释都放行（Wayland 上 GDK 报的到底是设备像素还是逻辑像素有歧义，2026-10-19 没能在无头环境
    里定论 —— 只认一种的话，万一认错就是"每次都自检失败、遮罩每次自己关掉"，比原来的 bug 更糟）：
      * 设备像素：逻辑尺寸 × scale；
      * 逻辑像素：逻辑尺寸本身。
    两者都**远大于** GTK 的默认 200×200 ⇒ 今天这个 bug 照样被抓住（200×200 两种解释都不合法）。
    """
    w, h = int(monitor[2]), int(monitor[3])
    sizes = {(w * int(scale), h * int(scale)), (w, h)}
    # 允许 ±2px 的四舍五入（有些合成器会少给几个像素）
    return {s for exact in sizes for s in [(exact[0] + d1, exact[1] + d2)
                                           for d1 in (-2, -1, 0, 1, 2) for d2 in (-2, -1, 0, 1, 2)]}


def geometry_problems(monitors, sizes, scales) -> list:
    """自检：每块屏的窗口尺寸对不对。返回问题清单（空 = 全对）。

    **不许静默**：量出来不对就得说清"期望多少、实得多少"，调用方据此关窗报错
    （把黑遮罩留在屏幕上、或者拿一个错误的窗口去截图，都比直接说"这次没成"更糟）。
    """
    problems = []
    for idx, monitor in enumerate(monitors):
        expected = acceptable_sizes(monitor, scales[idx] if idx < len(scales) else 1)
        got = tuple(sizes[idx]) if idx < len(sizes) and sizes[idx] else (0, 0)
        if got not in expected:
            want = f"{monitor[2]}×{monitor[3]}（或 ×{scales[idx] if idx < len(scales) else 1} 的设备像素）"
            problems.append(f"屏{idx}：期望 {want}，实得 {got[0]}×{got[1]}")
    return problems


def geometry_message(problems) -> str:
    return ("✗ 遮罩窗口尺寸没对上（" + "；".join(problems) + "）—— 已关掉遮罩，"
            "没有拿这张图去截（宁可不截，也不给一张全黑的）。"
            "这台机是 Wayland：窗口尺寸按理由合成器定，若反复出现请把 monitors/scale 记下来报上来")


def _gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    return Gtk, Gdk, GLib


def monitor_rects():
    """按 GDK 看到的显示器几何（全局逻辑坐标）—— 与抓帧那套坐标一致（本机三屏实测对齐）。"""
    _Gtk, Gdk, _GLib = _gtk()
    display = Gdk.Display.get_default()
    if display is None:
        return [], None
    out = []
    for i in range(display.get_n_monitors()):
        g = display.get_monitor(i).get_geometry()
        out.append((g.x, g.y, g.width, g.height))
    return out, display


class Overlay:
    """选区遮罩窗口组（一屏一窗，每窗背景 = 那块屏的冻结图）。**每个方法内部**才碰 gi。"""

    def __init__(self, frames=None, on_confirm=None, monitors=None, snap=True):
        self.frames = list(frames or [])
        self.on_confirm = on_confirm
        self.monitors = monitors
        self.rect = None            # 全局逻辑坐标下的选区（允许跨屏）
        from capsplus.shot_frames import union_rect
        self.union = union_rect(self.monitors or [(0, 0, 1920, 1080)])   # 方向键微调时的边界
        self.active = None          # 选区"主屏"索引（画交互提示用）
        self.pointer = None         # 最后一次指针位置（本地坐标 + 屏索引）
        self.dragging = False
        self._anchor = None         # **全局**坐标的起点（跨屏拖拽靠它才不乱）
        self.windows = []
        self.result = None
        self.outcome = None         # "confirm" / "cancel" / "geometry-fail"
        self.geometry_ok = None     # map 后自检结果（None = 还没量）
        self.magnifier = magnifier_enabled()   # 光标旁的放大镜（从冻结帧取样）
        self.failure = ""           # 自检失败时给调用方的话
        self.snap = snap

    # ---- 事件（纯状态机；事件对象只用到 .x/.y/.button 三个属性 → 判据能造假的）----
    def on_press(self, monitor_idx: int, local_xy, button: int = 1, double: bool = False):
        action = button_action(button, self.rect is not None, double=double)
        if action == ACTION_START:
            if double:
                return self.select_monitor(monitor_idx)
            self.dragging = True
            self._anchor = self._global_of(monitor_idx, local_xy)
            self.rect = None
            self.active = monitor_idx
            return ACTION_START
        if action == ACTION_CONFIRM:
            return self.confirm()
        if action == ACTION_CANCEL:
            return self.cancel()
        return ACTION_IGNORE

    def on_motion(self, monitor_idx: int, local_xy):
        self.pointer = (monitor_idx, local_xy)
        if not self.dragging or self._anchor is None:
            return ACTION_IGNORE
        from capsplus.shot_frame import normalize_rect
        here = self._global_of(monitor_idx, local_xy)
        # 起点与当前点都在**全局**坐标里算 ⇒ 拖到另一块屏照样对（旧版用本地坐标，跨屏必错）
        rect = normalize_rect(self._anchor[0], self._anchor[1], here[0], here[1])
        self.rect = rect
        self.active = monitor_idx
        return ACTION_UPDATE

    def on_release(self):
        """松手**不确认**（右键/Enter 才确认）—— 松手后还能重划，并顺手做贴边吸附。"""
        self.dragging = False
        if self.rect and self.snap:
            from capsplus.shot_frames import snap_rect
            snapped = snap_rect(self.rect, self.monitors or [])
            if snapped != self.rect:
                self.rect = snapped
                return ACTION_SNAP
        return ACTION_IGNORE

    def on_key(self, keyval_name: str, ctrl: bool = False):
        action = key_action(keyval_name, ctrl=ctrl)
        if action == ACTION_CONFIRM:
            return self.confirm()
        if action == ACTION_CANCEL:
            return self.cancel()
        if action == ACTION_FULL:
            return self.select_all()
        return ACTION_IGNORE

    # ---- 整屏 / 单屏两种捷径 ----
    def select_all(self):
        """Ctrl+A：整个联合矩形（"全屏选择"一键到位）。"""
        from capsplus.shot_frames import union_rect
        self.rect = union_rect(self.monitors or [])
        self.active = 0 if self.monitors else None
        return ACTION_FULL

    def select_monitor(self, monitor_idx: int):
        """双击：指针所在那块屏的整块（多屏时不至于被迫全选）。"""
        if self.monitors and 0 <= monitor_idx < len(self.monitors):
            self.rect = tuple(self.monitors[monitor_idx])
            self.active = monitor_idx
            return ACTION_FULL
        return ACTION_IGNORE

    # ---- 收尾 ----
    def confirm(self):
        """确认选区：有合法选区才认（太小 = 手抖，别给出 1×1 的垃圾图）。

        这里**不再等 settle**：确认后调用方直接从冻结图裁剪，屏幕都不再看一眼，
        所以"遮罩把自己拍进去"结构上不可能发生（旧版那套 settle 是为此存在的）。
        """
        from capsplus.shot_frame import accept_rect
        if not self.rect or not accept_rect(self.rect):
            return ACTION_IGNORE
        self.outcome = "confirm"
        self.result = self.rect
        self.hide_all()
        self._fire()
        self._quit_loop()
        return ACTION_CONFIRM

    def cancel(self):
        self.outcome = "cancel"
        self.result = None
        self.hide_all()
        self._fire()
        self._quit_loop()
        return ACTION_CANCEL

    def hide_all(self):
        for win in self.windows:
            win.hide()

    def _fire(self):
        if self.on_confirm:
            self.on_confirm(self.result)

    def _quit_loop(self):
        quit_fn = getattr(self, "_quit", None)
        if callable(quit_fn):
            quit_fn()

    def _global_of(self, monitor_idx: int, local_xy) -> tuple:
        monitor = self.monitors[monitor_idx]
        return (local_xy[0] + monitor[0], local_xy[1] + monitor[1])

    # ---- 建窗与主循环 ----
    def build(self, display, loop_quit):
        Gtk, Gdk, _GLib = _gtk()
        screen = Gdk.Screen.get_default()
        for idx, mon in enumerate(self.monitors):
            win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            win.set_decorated(False)
            win.set_resizable(False)
            win.set_accept_focus(True)
            win.set_skip_taskbar_hint(True)
            win.set_skip_pager_hint(True)
            # 背景是冻结图（不透明）⇒ 不需要 RGBA visual、更不 `set_app_paintable`。
            # 旧版正是依赖"能拿到 ARGB 表面"，拿不到就全黑 —— 这条别再改回去。
            win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                           Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK |
                           Gdk.EventMask.STRUCTURE_MASK)
            win.connect("draw", self._on_draw, idx)
            win.connect("button-press-event", self._on_press_event, idx)
            win.connect("motion-notify-event", self._on_motion_event, idx)
            win.connect("button-release-event", self._on_release_event, idx)
            win.connect("key-press-event", self._on_key_event, idx)
            # GTK3 的签名是 `fullscreen_on_monitor(screen, gint monitor)` —— **监控器序号**，
            # 不是 Gdk.Monitor 对象（传对象会得到 `Must be number, not X11Monitor`）。
            # ⚠ 这里**不许提前 `realize()`**，也**不许省掉 `set_default_size`**：见 overlay_geometry 的实测。
            overlay_geometry(win, screen, idx, mon)
            win.show_all()
            self.windows.append(win)
        self._quit = loop_quit
        self._schedule_geometry_check()

    # ---- map 之后自检尺寸（不对就关窗报错，绝不留一张黑遮罩）----
    def _schedule_geometry_check(self, attempt: int = 0) -> None:
        _Gtk, _Gdk, GLib = _gtk()
        GLib.timeout_add(OVERLAY_MIN_SETTLE, self._check_geometry, attempt)

    def _check_geometry(self, attempt: int = 0):
        """量每块屏的窗口 device 尺寸；不对就重试 `resize`，再不对就关窗 + 交一句可操作的话。"""
        sizes, scales = [], []
        for idx, win in enumerate(self.windows):
            window = win.get_window()
            sizes.append((window.get_width(), window.get_height()) if window else (0, 0))
            scales.append(win.get_scale_factor())
        problems = geometry_problems(self.monitors, sizes, scales)
        if not problems:
            if attempt == 0:
                # 这行是给真机验收用的：一眼看出"窗口 device 尺寸 = 屏幕逻辑尺寸 × scale"
                expect = [geometry_expectation(mon, scales[i] if i < len(scales) else 1)
                          for i, mon in enumerate(self.monitors)]
                print(f"[capsplus] overlay geometry ok sizes={sizes} scales={scales} "
                      f"monitors={self.monitors} expect_device={expect}")
            self.geometry_ok = True
            return False
        if attempt + 1 < OVERLAY_MAX_CHECKS:
            for idx, win in enumerate(self.windows):          # 再试一次显式 resize
                win.resize(self.monitors[idx][2], self.monitors[idx][3])
            self._schedule_geometry_check(attempt + 1)
            return False
        self.geometry_ok = False
        self.failure = geometry_message(problems)
        self.outcome = "geometry-fail"
        self.result = None
        self.hide_all()
        self._quit_loop()
        return False

    def _scaled_frame(self, idx, frame, area):
        """**预缩放缓存**：把冻结图先缩到"与窗口同尺寸"，之后每帧只做同尺寸 blit。

        性能体检（2026-10-20 实测，本机同图）：
          * 每帧把 2560×1440 的源缩到 1920×1080：**p50 32.1 ms / p95 33.0 ms**（一帧就超 60Hz 两倍）
          * 预缩放一次、之后只做同尺寸 `set_source_surface` + `paint()`：**p50 0.76 ms / p95 0.82 ms**
          ⇒ 快 **~42×**。用户说的"框选特别卡"主要就是这条：拖选区时每次都要整幅重画 + 每帧重新缩放。
        缓存键带 `area` 与帧对象 id（换帧/换窗口尺寸就重做），且**只留最新一份**（别把每块屏的图攒着）。
        """
        if frame is None:
            return None
        key = (id(frame), tuple(area))
        cached = getattr(self, "_scaled_cache", None)
        if cached is None:
            cached = self._scaled_cache = {}
        hit = cached.get(idx)
        if hit is not None and hit[0] == key:
            return hit[1]
        cairo = _cairo()
        aw, ah = max(1, int(area[2])), max(1, int(area[3]))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, aw, ah)
        cr = cairo.Context(surface)
        fw, fh = frame.get_width(), frame.get_height()
        if fw > 0 and fh > 0:
            cr.scale(aw / fw, ah / fh)
            cr.set_source_surface(frame, 0, 0)
            cr.paint()
        # **每块屏留自己那一份**（不是只留最新一份）：拖选区时三块屏要一起重画，
        # 只留一份的话每次重画都会把另外两块屏的图重新缩一遍（实测缩一次 36ms，
        # 三块屏 108ms/帧 ⇒ 正是"框选特别卡"）。每份 ≈ 屏逻辑尺寸 ×4 字节（1920×1080 ≈ 8.3MB）。
        cached[idx] = (key, surface)
        return surface

    def _frame_for(self, idx: int):
        for frame in self.frames:
            if frame.rect == tuple(self.monitors[idx]):
                return frame.surface
        if 0 <= idx < len(self.frames):
            return self.frames[idx].surface
        return None

    def _local_rect(self, idx: int):
        """该屏本地坐标系下的选区（跨屏时可能是负坐标/超出屏宽 —— cairo 自己会裁）。"""
        if not self.rect:
            return None
        return to_local(self.monitors[idx], self.rect)

    def colour_at_pointer(self):
        """指针下的颜色（从**冻结帧**取，按当前指针所在那块屏换算）。取不到返回 None。"""
        if not self.pointer:
            return None
        idx, xy = self.pointer
        if idx >= len(self.windows) or idx >= len(self.monitors):
            return None
        mon = self.monitors[idx]
        return pixel_colour(self._frame_for(idx), xy, (0, 0, mon[2], mon[3]))

    def copy_colour(self) -> tuple:
        """按 `C`：把光标下的 `#RRGGBB` 送进剪贴板（复用剪贴板那条链路，失败如实说）。"""
        rgb = self.colour_at_pointer()
        if rgb is None:
            return False, "✗ 取不到颜色（指针不在屏上？）"
        text = hex_colour(rgb)
        from capsplus import shot_annotate as SA
        ok, why = SA.copy_text_to_clipboard(text)
        self.colour_text = text
        return ok, (f"✓ 已复制颜色 {text}" if ok else why)

    def _on_draw(self, widget, cr, idx):
        # 只依赖传进来的 widget 与显示器几何 —— **不查 self.windows**：
        # 这样判据能拿一个 `Gtk.OffscreenWindow + DrawingArea` 走**真实 draw 处理器**渲染（见
        # test_shot_overlay 的 TestRealWidgetDrawPath），而不是只调 paint() 绕开 GTK 这一层。
        area = (0, 0, max(widget.get_allocated_width(), self.monitors[idx][2]),
                max(widget.get_allocated_height(), self.monitors[idx][3]))
        crosshair = None
        magnifier = None
        if self.pointer and self.pointer[0] == idx:
            crosshair = self.pointer[1]
            if self.magnifier:                 # 放大镜：从**冻结帧**取样（零成本、零延迟）
                magnifier = self.pointer[1]
        colour = None
        if crosshair is not None and getattr(self, "colour_readout", True):
            colour = pixel_colour(self._scaled_frame(idx, self._frame_for(idx), area),
                                  crosshair, area)
        paint(cr, area, self._local_rect(idx),
              frame=self._scaled_frame(idx, self._frame_for(idx), area),
              crosshair=crosshair, magnifier=magnifier, colour=colour)
        # ⚠ **必须 return True**（2026-10-19 真机事故）：`GtkWidget::draw` 的默认处理器跑在自定义
        # 处理器**之后**，会把控件自己的主题背景刷在我们的画上面 —— 表现就是"三个窗口尺寸都对，
        # 屏幕上却是整块纯白"（Adwaita 的 #fafafa ≈ 250；用户看到纯白、我抓屏读数也是均匀 250，
        # 两者一致）。返回 True = 停止信号发射 ⇒ 默认背景不再覆盖。
        # 反例：删掉这一行 → `tests/test_shot_overlay.TestWindowsBackgroundNotOverpaint` 必红
        #（那条判据必须用真 `Gtk.Window`；`Gtk.DrawingArea` 本来就没背景，测不出来，会假绿）。
        return True

    def _on_press_event(self, _widget, event, idx):
        double = bool(getattr(event, "type", None) == 16)      # Gdk.EventType.DOUBLE_BUTTON_PRESS
        self.on_press(idx, (int(event.x), int(event.y)), event.button, double=double)
        if not self.outcome:
            self._redraw()
        return True

    def _on_motion_event(self, _widget, event, idx):
        """鼠标移动：**指针动就重画**（不按键也要跟手）。

        2026-10-20 真机事故（用户："十字线不跟鼠标走、要按着拖才跟"）：上一批的"性能体检 B3"
        把这里改成对 GdkWindow 调 **子区域** `invalidate_rect(dirty, False)` —— 而 GTK3 对
        **直接打在 GdkWindow 上的子区域失效不发 `draw` 信号**（只有整幅 `invalidate_rect(None,…)`
        才发）。于是第一次移动走"整幅"那条路（画得出来），之后每次移动都走脏矩形那条路 ⇒
        **一次都不重画**，十字线冻在第一次的位置上；而拖动时走的是 `moved != ACTION_IGNORE`
        的整幅那条路，所以"按着拖反而跟手"。
        实测（真机 ydotool 连移 30 次，探针记录）：MOTION 690 次 / REDRAW 689 次 / **DRAW 只有 30 次**
        （还都在起止两头），两次抓屏逐像素差 **0.0000%**。
        另：`tests/test_shot_overlay.TestMotionOnlyRedrawsADirtyRect` 当时是绿的 —— 它拿假窗口断言
        "调没调那个 API、参数多小"，而不是"draw 有没有发生、像素有没有变"，所以拦不住。

        现在回到 `GtkWidget.queue_draw()`（GTK 的正确入口，实测 40/40 次都发 draw），
        并把代价压在图本身：预缩放缓存后整幅重画的 p50 = **0.87ms**（见 `_scaled_frame`），
        60Hz 一帧 16.7ms 里占 5%，**不需要**再省那块脏矩形。
        选区变了（`moved != ACTION_IGNORE`）才需要三块屏一起重画（压暗带/读数整片都变）。
        """
        prev = self.pointer[0] if self.pointer else None
        moved = self.on_motion(idx, (int(event.x), int(event.y)))
        if moved != ACTION_IGNORE:
            self._redraw()                     # 选区变了：每块屏的压暗带与读数都变了
        else:
            self._redraw(idx=idx)              # 只有指针那块屏变
            if prev is not None and prev != idx:
                self._redraw(idx=prev)         # 换屏：旧那块屏上留着的准线要擦掉
        return True

    def _on_release_event(self, _widget, _event, idx):
        self.on_release()
        self._redraw()
        return True

    def _on_key_event(self, _widget, event, idx):
        name, ctrl, shift = "", False, False
        try:
            import gi
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk
            name = Gdk.keyval_name(event.keyval) or ""
            ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
            shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        except Exception:
            name, ctrl, shift = "", False, False
        action = key_action(name, ctrl=ctrl)
        if action == ACTION_NUDGE:
            from capsplus.shot_annotate import nudge_delta      # 步长与标注窗同源
            dx, dy = nudge_delta(name, shift)
            self.nudge(dx, dy)
        elif action == ACTION_COPY_COLOUR:
            ok, why = self.copy_colour()                       # 提取颜色：复制 #RRGGBB
            print(why)
            self._redraw()
        else:
            self.on_key(name, ctrl=ctrl)
        if not self.outcome:
            self._redraw()
        return True

    def nudge(self, dx: int, dy: int) -> bool:
        """方向键微调选区（1px；Shift=10px），夹在联合矩形里 —— 不许挪出屏幕。"""
        if not self.rect:
            return False
        self.rect = nudge_rect(self.rect, dx, dy, self.union)
        return True

    def _redraw(self, idx=None):
        """让窗口重画：`idx=None` = 全部窗口，给了 `idx` = 只有那一块屏。

        ⚠ **必须走 `GtkWidget.queue_draw()`**，不许回退成 `GdkWindow.invalidate_rect(rect, False)`
        （子区域那条不发 `draw`，见 `_on_motion_event` 的实测）。反例：把这里换回
        `win.get_window().invalidate_rect(...)` ⇒ `tests/test_shot_overlay` 的
        `TestMotionActuallyRepaints` 必红（真窗口 + 真像素）。
        """
        targets = self.windows if idx is None else self.windows[idx:idx + 1]
        for win in targets:
            if win.get_window() is not None:
                win.queue_draw()


def select_region(frames, monitors=None, on_confirm=None) -> tuple:
    """跑一次选区：返回 `(global_rect | None)`；取消返回 None。

    `frames` 是 `shot_frames.freeze()` 的产物（每屏一块冻结图）—— **必须给**：
    没有冻结图就没有背景，那正是要根治的"全黑盲选"，所以这里不做"没有也凑合开窗"的兜底。
    """
    Gtk, _Gdk, GLib = _gtk()
    _ = Gtk
    if monitors is None:
        monitors, display = monitor_rects()
    else:
        display = _Gdk.Display.get_default()
    if not monitors or display is None:
        return None
    overlay = Overlay(frames=frames, on_confirm=on_confirm, monitors=monitors)
    loop = GLib.MainLoop()
    overlay.build(display, loop.quit)
    loop.run()
    # 返回 `(rect, failure)`：几何自检没过时必须让调用方**知道**，不能静默给个 None 就当"用户取消了"
    return overlay.result, (overlay.failure or None)