# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""选区遮罩（第一批第 2 步）：GTK3 全屏半透明遮罩 + 橡皮筋 + 实时宽×高读数。

**绝不闪**（明江换掉 Snipaste 就是因为闪）——所以本文件里没有一处动画：不 `ease`、不
`set_opacity` 过渡、不 CSS transition，窗口一 map 就是最终样子。判据
（`tests/test_shot_overlay.py::TestNoAnimation`）就是在扫这件事。

顶层**不 import gi / cairo**（CI 不装 PyGObject 与 pycairo，判据：`tests/test_no_gi_smoke.py`）：
两边都在 `_gtk()` / `_cairo()` 里懒加载。所以本文件的纯逻辑与 `paint()` 在没 gi、没显示服务器的
机器上照样能测 —— `paint()` 只吃一个 cairo 上下文，判据把它渲染到 `ImageSurface` 上**逐像素断言**。

三条定死的口径：

  1. **一屏一窗**（`fullscreen_on_monitor`）。Wayland 下 GTK 不能自由摆放窗口，跨屏一次拖拽做不到；
     每块屏各自可拖，选区坐标统一换算成**全局逻辑坐标**再交给抓帧（抓帧用的就是这套坐标）。
  2. **放行键盘失败不能让人卡住**：鼠标那条路（右键确认）永远有效；键盘（Enter/Esc）只在窗口
     拿到焦点时才到 —— 提示行两种都写了。全文件不 `grab` 输入（Wayland 上 grab 不可靠，
     还会把别人的输入吞掉）。
  3. **确认 → 先藏窗、等一帧、再交坐标**：我们这扇窗必须在抓帧前从屏幕上消失（否则截图里
     会带着自己的遮罩）。藏窗之后由调用方负责下发抓帧，中间隔 `settle_ms` 让合成器提交移除。
"""

from __future__ import annotations

import time

# ---------------------------------------------------------------- 常量（判据按它们断言）

DIM_ALPHA = 0.35          # 选区外压暗的强度
BORDER_WIDTH = 1          # 选区内缘描边（像素）
CORNER_LEN = 10           # 四角角标长度
CORNER_WIDTH = 2
READOUT_PAD = 6
FONT_SIZE = 14
COLOR_DIM = (0.0, 0.0, 0.0, DIM_ALPHA)
COLOR_BORDER = (0.30, 0.85, 0.95, 1.0)        # 亮青：在暗背景上够显眼，也不必用动画强调
COLOR_READOUT_BG = (0.0, 0.0, 0.0, 0.72)
COLOR_READOUT_FG = (1.0, 1.0, 1.0, 1.0)
HINT_TEXT = "拖动选择 · Enter/右键确认 · Esc 取消"

ACTION_IGNORE = "ignore"
ACTION_START = "start"     # 左键按下：开始/重划选区
ACTION_UPDATE = "update"   # 拖动中
ACTION_CONFIRM = "confirm"
ACTION_CANCEL = "cancel"


# ------------------------------------------------------------------------ 纯逻辑（可单测）

def key_action(keyval_name: str):
    """键盘 → 动作。只认 Esc 与 Enter（含小键盘 Enter），其余不处理。"""
    if keyval_name == "Escape":
        return ACTION_CANCEL
    if keyval_name in ("Return", "KP_Enter"):
        return ACTION_CONFIRM
    return None


def button_action(button: int, has_selection: bool) -> str:
    """鼠标 → 动作。

    左键 = 开始/重划；右键 = 确认；**右键时还没有选区就当取消**（点一下什么都不发生会让人以为卡了）。
    """
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
    """该屏本地 → 全局（交抓帧用）。"""
    return (rect[0] + monitor[0], rect[1] + monitor[1], rect[2], rect[3])


def readout_text(rect) -> str:
    """实时读数：`宽 × 高`（Snipaste 也是这个习惯；再带一处起点坐标，方便对齐）。"""
    return f"{rect[2]} × {rect[3]}"


def readout_position(rect, area) -> tuple:
    """读数框的位置：默认在选区**上方外侧**；贴到屏幕上边缘时挪到选区里面。"""
    x = rect[0]
    y = rect[1] - (FONT_SIZE + 2 * READOUT_PAD)
    if y < area[1]:
        y = rect[1] + READOUT_PAD
    if x < area[0]:
        x = area[0]
    return (x, y)


def dim_bands(rect, area) -> list:
    """选区**外面**要压暗的矩形（上/下/左/右四条带）。

    为什么用四条带而不是"整屏压暗 + 挖洞"：挖洞要靠 `OPERATOR_CLEAR` 之类的合成技巧，
    多一层隐式依赖；四条带在像素上等价，判据一眼能算出来。
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


def _cairo():
    import cairo
    return cairo


def text_box(text: str) -> tuple:
    """一行文字的底色框尺寸（宽度按字数估：这层不引 Pango，用 cairo 的 toy 字体 API）。"""
    return (12 + 9 * len(text), FONT_SIZE + 2 * READOUT_PAD)


def paint(cr, area, rect, hint: bool = True) -> None:
    """把遮罩画到 cairo 上下文上（**不碰 gi**，判据直接渲染到 ImageSurface 上断言像素）。

    像素口径（判据照这个算）：
      * 先用 `OPERATOR_SOURCE` 把整块画布清成**全透明** —— GTK 的 draw 里不画的地方是未定义内容，
        不显式清一遍就可能出现"半透明压暗却是脏底色"的怪相；清过之后：
        选区**外** = 黑 α=DIM_ALPHA，选区**内** = 全透明（屏幕原样透出来）。
      * 选区内缘 1px 亮青描边 + 四角加粗角标
      * 读数（宽×高）画在选区上方外侧；`hint` 为真时在左上角画一行操作提示
    """
    cairo = _cairo()
    ax, ay, aw, ah = area
    cr.save()
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.restore()

    if rect and rect[2] > 0 and rect[3] > 0:
        bands = dim_bands(rect, area)
    else:
        bands = [(ax, ay, aw, ah)]
    cr.set_source_rgba(*COLOR_DIM)
    for bx, by, bw, bh in bands:
        cr.rectangle(bx, by, bw, bh)
    cr.fill()

    if hint:
        hw, hh = text_box(HINT_TEXT)
        cr.set_source_rgba(*COLOR_READOUT_BG)
        cr.rectangle(ax + 8, ay + 8, hw, hh)
        cr.fill()
        cr.set_source_rgba(*COLOR_READOUT_FG)
        cr.select_font_face("Sans")
        cr.set_font_size(FONT_SIZE)
        cr.move_to(ax + 8 + READOUT_PAD, ay + 8 + FONT_SIZE + READOUT_PAD - 3)
        cr.show_text(HINT_TEXT)

    if not (rect and rect[2] > 0 and rect[3] > 0):
        return

    x, y, w, h = rect
    cr.set_source_rgba(*COLOR_BORDER)
    cr.set_line_width(BORDER_WIDTH)
    cr.rectangle(x + 0.5, y + 0.5, max(w - 1, 1), max(h - 1, 1))
    cr.stroke()

    # 四角角标：比 1px 边框更容易看清选区边界（纯静态，不用动画）
    cr.set_line_width(CORNER_WIDTH)
    for cx, cy, dx, dy in ((x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        cr.move_to(cx, cy + dy * CORNER_LEN)
        cr.line_to(cx, cy)
        cr.line_to(cx + dx * CORNER_LEN, cy)
    cr.stroke()

    text = readout_text(rect)
    rx, ry = readout_position(rect, area)
    tw, th = text_box(text)
    cr.set_source_rgba(*COLOR_READOUT_BG)
    cr.rectangle(rx, ry, tw, th)
    cr.fill()
    cr.set_source_rgba(*COLOR_READOUT_FG)
    cr.move_to(rx + READOUT_PAD, ry + FONT_SIZE + READOUT_PAD - 3)
    cr.show_text(text)


# ------------------------------------------------------------------------------ GTK 部分

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
    """选区遮罩窗口组。**每个方法内部**才碰 gi，模块导入本身不需要 gi。"""

    def __init__(self, settle_ms: int = None, on_confirm=None, monitors=None):
        from capsplus.shot_frame import SETTLE_MS
        self.settle_ms = SETTLE_MS if settle_ms is None else settle_ms
        self.on_confirm = on_confirm
        self.monitors = monitors
        self.rect = None            # 全局逻辑坐标下的选区
        self.active = None          # 选区所在屏的索引
        self.dragging = False
        self._anchor = None         # 本地坐标的起点
        self.windows = []
        self.result = None
        self.outcome = None         # "confirm" / "cancel"

    # ---- 事件（纯状态机，事件对象只用到 .x/.y/.button/.keyval 四个属性 → 判据能造假的）----
    def on_press(self, local_xy, button: int):
        action = button_action(button, self.rect is not None)
        if action == ACTION_START:
            self.dragging = True
            self._anchor = local_xy
            self.rect = None
            return ACTION_START
        if action == ACTION_CONFIRM:
            return self.confirm()
        if action == ACTION_CANCEL:
            return self.cancel()
        return ACTION_IGNORE

    def on_motion(self, monitor_idx: int, local_xy):
        if not self.dragging or self._anchor is None:
            return ACTION_IGNORE
        from capsplus.shot_frame import normalize_rect
        local = normalize_rect(self._anchor[0], self._anchor[1], local_xy[0], local_xy[1])
        self.active = monitor_idx
        self.rect = to_global(self.monitors[monitor_idx], local)
        return ACTION_UPDATE

    def on_release(self):
        """松手**不确认**（按第 2 步的口径：右键或 Enter 才确认）—— 松手后还能重划、还能微调。"""
        self.dragging = False
        return ACTION_IGNORE

    def on_key(self, keyval_name: str):
        action = key_action(keyval_name)
        if action == ACTION_CONFIRM:
            return self.confirm()
        if action == ACTION_CANCEL:
            return self.cancel()
        return ACTION_IGNORE

    # ---- 收尾 ----
    def confirm(self):
        """确认选区。**注意不要在这里退出主循环**：要等 settle 那一下过完再退。

        2026-10-19 实测踩到的坑（xvfb 真事件判据抓出来的）：第一版在事件处理器里
        `if self.outcome: self._quit()` —— 确认后主循环立刻退出，settle 的 timeout 根本没机会跑，
        于是"藏窗 → 等一帧 → 交坐标"里的"等一帧"从来没发生过，抓帧有可能把遮罩自己拍进去。
        现在：确认只**排** settle，由 `_tick` 到点后交坐标并退出循环；取消才是立刻退。
        """
        from capsplus.shot_frame import accept_rect
        if not self.rect or not accept_rect(self.rect):
            return ACTION_IGNORE        # 太小/没有选区：确认无效，别给出 1×1 的垃圾图
        self.outcome = "confirm"
        self.result = self.rect
        self.hide_all()
        self._settle_then_fire()
        return ACTION_CONFIRM

    def cancel(self):
        self.outcome = "cancel"
        self.result = None
        self.hide_all()
        self._fire()
        return ACTION_CANCEL

    def hide_all(self):
        for _win in self.windows:
            _win.hide()

    def _fire(self):
        if self.on_confirm:
            self.on_confirm(self.result)

    def _settle_then_fire(self):
        """藏窗之后等 `settle_ms` 再交坐标：合成器得把我们这扇窗的移除提交出去，
        否则抓帧会把自己的遮罩拍进去。这期间主循环照跑（在 loop 里排的 timeout）。"""
        _Gtk, _Gdk, GLib = _gtk()
        # PyGObject 的 timeout_add 是 **(interval, callback)**，priority 只能当关键字传
        # （`gi/overrides/GLib.py`：`timeout_add(interval, function, *user_data, priority=…)`）。
        # 写成 GJS 那种 (priority, interval, cb) 会得到 `Callback needs to be a function or method
        # not int` —— 那一下回调永远不排上，确认选区之后界面就卡住不动了（xvfb 界面判据抓到的）。
        GLib.timeout_add(max(int(self.settle_ms), 0), self._tick)

    def _tick(self):
        self._fire()            # 交坐标（调用方拿到时，遮罩已经藏了 settle_ms）
        self._quit_loop()
        return False

    def _quit_loop(self):
        """退出主循环（未建窗时 `_quit` 不存在 —— 纯状态机判据里就是这么跑的）。"""
        quit_fn = getattr(self, "_quit", None)
        if callable(quit_fn):
            quit_fn()

    # ---- 建窗与主循环（只在真跑时用）----
    def build(self, display, loop_quit):
        Gtk, Gdk, _GLib = _gtk()
        screen = Gdk.Screen.get_default()
        for idx, mon in enumerate(self.monitors):
            win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            win.set_decorated(False)
            win.set_resizable(False)
            win.set_accept_focus(True)
            win.set_app_paintable(True)
            win.set_skip_taskbar_hint(True)
            win.set_skip_pager_hint(True)
            visual = screen.get_rgba_visual()
            if visual is not None:
                win.set_visual(visual)
            win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                           Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK |
                           Gdk.EventMask.STRUCTURE_MASK)
            win.connect("draw", self._on_draw)
            win.connect("button-press-event", self._on_press_event)
            win.connect("motion-notify-event", self._on_motion_event)
            win.connect("button-release-event", lambda *_a: self.on_release())
            win.connect("key-press-event", self._on_key_event)
            win.realize()
            # GTK3 的签名是 `fullscreen_on_monitor(screen, gint monitor)` —— **监控器序号**，
            # 不是 Gdk.Monitor 对象（传对象会得到 `Must be number, not X11Monitor`；
            # GTK4 才是传对象）。`monitor_rects()` 的顺序与 display.get_monitor(i) 一致，所以给 idx。
            win.fullscreen_on_monitor(screen, idx)
            win.show_all()
            self.windows.append(win)
        self._quit = loop_quit

    def _on_draw(self, widget, cr):
        area = (0, 0, widget.get_allocated_width(), widget.get_allocated_height())
        local = None
        if self.rect and self.active is not None:
            local = to_local(self.monitors[self.active], self.rect)
        paint(cr, area, local)

    def _monitor_index_of(self, widget):
        for idx, win in enumerate(self.windows):
            if win is widget:
                return idx
        return None

    def _on_press_event(self, widget, event):
        idx = self._monitor_index_of(widget)
        if idx is None:
            return False
        self.on_press((int(event.x), int(event.y)), event.button)
        if self.outcome == "cancel":
            self._quit()          # 确认走 settle 那条路（见 confirm）
        elif not self.outcome:
            self._redraw()
        return True

    def _on_motion_event(self, widget, event):
        idx = self._monitor_index_of(widget)
        if idx is None:
            return False
        if self.on_motion(idx, (int(event.x), int(event.y))) == ACTION_UPDATE:
            self._redraw()
        return True

    def _on_key_event(self, _widget, event):
        name = ""
        try:
            import gi
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk
            name = Gdk.keyval_name(event.keyval) or ""
        except Exception:
            name = ""
        self.on_key(name)
        if self.outcome == "cancel":
            self._quit()
        return True

    def _redraw(self):
        for win in self.windows:
            if win.get_window() is not None:
                win.get_window().invalidate_rect(None, False)


def select_region(settle_ms: int = None, on_confirm=None, monitors=None) -> tuple:
    """跑一次选区：返回 `(global_rect | None)`，取消返回 None。

    调用方拿到坐标时，**遮罩窗已经藏好并过了 settle**（见 `Overlay.confirm`）。
    """
    Gtk, _Gdk, GLib = _gtk()
    if monitors is None:
        monitors, display = monitor_rects()
    else:
        display = _Gdk.Display.get_default()
    if not monitors or display is None:
        return None
    overlay = Overlay(settle_ms=settle_ms, on_confirm=on_confirm, monitors=monitors)
    loop = GLib.MainLoop()
    overlay.build(display, loop.quit)
    loop.run()
    return overlay.result