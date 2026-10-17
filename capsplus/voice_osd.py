# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""语音输入的弹窗（GTK3）：录音期**不弹任何窗**，review 期可编辑。

顶层**不 import gi**（CI 不装 PyGObject，判据：`tests/test_no_gi_smoke.py` +
`tests/test_voice_portability.py`）；gi 只在 `_gtk()` 里懒加载。
所以本文件的纯逻辑（`window_plan` / `key_action` / `ui_available`）在没 gi 的机器上照样能测。

三条设计口径：

  1. **录音期不映射窗口**（默认）。原因写死在这儿：xdg-shell **没有**"别把焦点给我"这个请求，
     GNOME 下新 map 的 toplevel 通常就是会被给焦点 —— `set_accept_focus(False)` 在 Wayland 侧
     只是 X11 hint 的等价物，**拦不住合成器**。所以"录音期绝不抢焦点"唯一可靠的实现就是
     **那时候根本没有窗口**。想要现场指示：`CAPSPLUS_VOICE_BADGE=1`（自担风险），
     或者只看 `capsplus-voice status` / socket 上广播的 `state` 事件。
  2. **review 期才夺焦点**：可编辑、`present()` + `grab_focus()` —— 只作用于**我们自己的**控件。
     全文件不许出现 `set_focus(...)` / `present_with_time(...)` / `set_keep_above(...)`：
     前者会去动别的窗口，中者需要激活令牌（GTK3 没有，见 KNOWN-LIMITS），后者在 Wayland 无效。
     **焦点归还**靠 WM 在窗口销毁后的自然回退，我们不自己抢回来（判据：AST 扫 `_gtk` 用法）。
  3. **先关窗再上屏**：Enter 的动作顺序是 `commit → 销毁窗口 → 结局回调`。顺序反了就变成
     "Ctrl+V 贴进我们自己的弹窗"。这条在两个入口里都定死：`open_review()`（worker 用的
     非阻塞入口，主线程建窗、**窗口销毁之后**才 `on_finish`）与 `show()`（阻塞式，返回时
     窗口已销毁；只留给手动跑和单测，worker 不用它 —— "每句话起一个线程跑一个
     MainLoop"正是 2026-10-17「无响应」弹窗的病根，见 `voice_worker` 模块 docstring）。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

MODES = ("recording", "review", "notice")
BADGE_ENV = "CAPSPLUS_VOICE_BADGE"
DEFAULT_TITLE = "语音输入（Caps+M）"
REVIEW_WIDTH = 620
REVIEW_HEIGHT = 200
TEXT_LIMIT = 4000

# 弹窗里那行提示（不带家目录、不带任何个人路径 —— 截图判据会扫）
LABELS = {
    "recording": "正在录音…（再说一次 Caps+M 结束）",
    "decoding": "识别中…",
    "empty": "没听清：没检测到说话（底噪太轻时能量门会把它整段丢掉）",
    "review_title": "确认文本 · Enter 上屏 · Shift+Enter 换行 · Esc 丢弃",
}


# ── 纯逻辑（无 gi，可单测）────────────────────────────────────────────────────
def badge_enabled() -> bool:
    """录音期要不要真弹窗。默认 **不要**（理由见模块 docstring 第 1 条）。"""
    return os.environ.get(BADGE_ENV) == "1"


def window_plan(mode: str) -> dict:
    """模式 → 窗口属性。**代码只照这张表设置，不许在别处散着写**。

    `allow_present_and_focus` 是给我们自己窗口用的（review 才真），
    和"绝不去动别的窗口"是两件事，后者由 AST 判据守。
    """
    if mode not in MODES:
        raise ValueError(f"未知弹窗模式 {mode!r}，只认 {' / '.join(MODES)}")
    review = mode == "review"
    return {
        "mode": mode,
        "decorated": review,                 # 确认页要正常窗口；录音/提示是贴在屏幕中间的小条
        "accept_focus": review,              # 非 review 一律不可聚焦
        "skip_taskbar": True,
        "skip_pager": True,
        "keep_above": False,                 # Wayland 下没这功能（KNOWN-LIMITS 第 6 条）
        "editable": review,                  # 只有确认页能编辑
        "allow_present_and_focus": review,   # 只允许对本窗口 present()/grab_focus()
    }


def key_action(keyval: str, mode: str, shift: bool = False, ctrl: bool = False) -> str:
    """按键 → 动作：`commit` / `newline` / `cancel` / `paste_hint` / `ignore`。

    为什么单独一个纯函数：Enter 的语义是这个弹窗的全部交互（Enter 上屏、Shift+Enter 换行、
    Esc 丢弃），行为测试要靠真键盘事件才验得了（KNOWN-LIMITS 第 8 条说界面只能人眼），
    所以把**判定**下沉成纯函数用单测锁住，GTK 侧只负责把键名递进来。
    """
    if mode not in MODES:
        return "ignore"
    if keyval == "Escape":
        return "cancel"
    if mode != "review":
        return "ignore"
    if keyval in ("Return", "KP_Enter"):
        return "newline" if (shift and not ctrl) else "commit"
    if keyval == "v" and ctrl:
        return "paste_hint"                  # 弹窗自己有焦点时 Ctrl+V 就是贴进弹窗，提醒一下
    return "ignore"


def text_for_display(text: str, limit: int = TEXT_LIMIT) -> str:
    """进控件前的清洗：去掉控制字符、截超长（剪贴板与注入都吃不下无界文本）。"""
    clean = "".join(ch for ch in (text or "") if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return clean[:limit]


def ui_available() -> bool:
    """现在能不能弹窗。只**读**环境变量，绝不设置（判据：`tests/test_display_env_hygiene.py`）。"""
    if os.environ.get("CAPSPLUS_VOICE_UI") == "0":
        return False
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        if not (runtime and os.path.exists(os.path.join(runtime, "wayland-0"))):
            return False
    try:
        import gi                                        # noqa: F401  懒加载：没装 PyGObject 就退到无 UI
    except Exception:                                    # noqa: BLE001  ImportError / ValueError(typelib)
        return False
    return True


@dataclass
class UiOutcome:
    """`VoiceUi.show()` 的结论。`action` ∈ commit / cancel / closed / unavailable / failed。"""
    action: str = "unavailable"
    text: str = ""
    note: str = ""

    @property
    def commit(self) -> bool:
        return self.action == "commit"


# ── GTK 部分（全部在函数里取 gi）─────────────────────────────────────────────
def _gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk, Pango          # noqa: F401
    return Gtk, Gdk, Pango


class VoiceUi:
    """一个 UI 会话（一次 review 一个对象；**主循环不归它管**，全进程只有一个，在 worker 主线程）。

    用法（worker 侧，2026-10-17 定稿）：

        ui = VoiceUi(on_finish=回调)
        ui.post_text("…")            # 建窗前的事件先排队，build 时补上
        outcome = ui.open_review()   # **主线程**建窗，非阻塞；None = 开窗成功等回调
        # commit/cancel 之后：窗口已销毁 → on_finish(UiOutcome) 在 idle_add 里回来

    `show()` 是阻塞式的旧入口（手动跑 / 单测用），返回时窗口同样已经销毁。
    `post_*` 是线程安全的（走 GLib.idle_add），没建窗之前调用会先攒着，建窗时补上。
    """

    def __init__(self, title: str = DEFAULT_TITLE, on_finish=None):
        self.on_finish = on_finish        # callable(UiOutcome)，只会被叫一次
        self.title = title
        self.mode = "recording"
        self.note = LABELS["recording"]
        self.text = ""
        self._pending: list[tuple] = []
        self._window = None
        self._view = None
        self._label = None
        self._loop = None
        self._action = ""
        self._done = False

    # -- 能不能用 -------------------------------------------------------------
    def available(self) -> bool:
        return ui_available()

    # -- 事件入口（线程安全）---------------------------------------------------
    def post_state(self, state: str, note: str = "") -> None:
        self._emit(("state", state, note or LABELS.get(state, "")))

    def post_text(self, text: str) -> None:
        self._emit(("text", text_for_display(text)))

    def post_error(self, code: str, message: str = "", hint: str = "") -> None:
        self._emit(("error", code, message, hint))

    def close(self) -> None:
        self._emit(("close", self._action or "closed"))

    def _emit(self, event: tuple) -> None:
        if self._done:                       # show() 已经返回过：会话结束，别再攒事件
            return
        if self._window is None:
            self._pending.append(event)
            return
        try:
            from gi.repository import GLib
            GLib.idle_add(self._apply, event)
        except Exception:                                # noqa: BLE001
            self._apply(event)

    def _apply(self, event: tuple) -> bool:
        """主线程里真正改窗口（`GLib.idle_add` 的回调，返回 False 表示只跑一次）。"""
        kind = event[0]
        if kind == "state":
            _, state, note = event
            self.note = note
            if state in ("recording", "decoding"):
                self._show_notice(note)
            elif state == "idle":
                self._hide_window()
        elif kind == "text":
            self.text = event[1]
            self._show_review()
        elif kind == "error":
            _, code, message, hint = event
            self._show_notice(LABELS["empty"] if code == "no_speech" else
                              (message or code) + (("\n" + hint) if hint else ""))
        elif kind == "close":
            self._finish(event[1])
        return False

    # -- 建窗 -----------------------------------------------------------------
    def build(self) -> None:
        """只建窗、不起主循环。

        为什么与 `show()` 分开：界面测试要在**同一个线程**里断言窗口属性
        （`get_accept_focus()` / `get_editable()` / 合成一个 Enter 事件），
        而 GTK3 不是线程安全的 —— 把建窗单独留个口子，测试就不必拿线程去赌。

        注意这里**不创建 MainLoop**：主循环是 worker 主线程那份（进程唯一）；
        只有阻塞式 `show()` 才自带一个临时循环。
        """
        if self._window is not None:
            return
        Gtk, _Gdk, _Pango = _gtk()
        self._window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self._window.set_title(self.title)
        self._window.set_default_size(REVIEW_WIDTH, REVIEW_HEIGHT)
        self._window.set_position(Gtk.WindowPosition.CENTER)
        self._window.connect("delete-event", lambda *_a: (self._finish("closed"), True)[1])
        self._window.connect("key-press-event", self._on_key)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(10)
        self._label = Gtk.Label(label=self.note)
        self._label.set_line_wrap(True)
        self._label.set_xalign(0.0)
        box.pack_start(self._label, False, False, 0)

        self._view = Gtk.TextView()
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view.set_left_margin(6)
        self._view.set_right_margin(6)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self._view)
        box.pack_start(scroll, True, True, 0)
        self._window.add(box)

        self._apply_mode(window_plan(self.mode))
        for event in list(self._pending):
            self._apply(event)
        self._pending.clear()
        self._window.show_all()

    def open_review(self) -> UiOutcome | None:
        """**主线程、非阻塞**地建好 review 窗。成功返回 None（结局稍后走 `on_finish`）；
        失败返回 `UiOutcome`（调用方据此退回直接上屏，文本不许被静默丢掉）。

        这是 worker 唯一的开窗入口。主线程检查不是形式主义：2026-10-17 的
        「"voice_worker.py" 无响应」弹窗，病根就是旧代码在后台线程里建窗跑循环；
        进程级判据 `tools/verify-voice-mainloop.sh` 与行为判据 A
        （`tests/test_voice_mainloop.py`）都钉在这条上。
        """
        if not self.available():
            return UiOutcome(action="unavailable", note="没装 PyGObject 或连不上显示服务")
        if threading.current_thread() is not threading.main_thread():
            return UiOutcome(action="failed", note="开窗必须在进程主线程")
        try:
            self.build()
        except Exception as exc:                        # noqa: BLE001 建窗失败要能退回无头路径
            return UiOutcome(action="failed", note=f"{type(exc).__name__}: {exc}")
        self._present_if_review()
        return None

    def _present_if_review(self) -> None:
        """review 才允许 present()/grab_focus()，而且**只对自己的控件**。"""
        if self._window is not None and window_plan(self.mode)["allow_present_and_focus"]:
            self._window.present()
            self._view.grab_focus()

    def show(self) -> UiOutcome:
        """建窗并**自己跑一个临时主循环**，直到 commit/cancel。阻塞式旧入口：
        手动跑和单测用它；worker 用 `open_review()`（进程唯一主循环在 worker 主线程）。
        **没 gi/没显示时返回 unavailable**。"""
        if not self.available():
            return UiOutcome(action="unavailable", note="没装 PyGObject 或连不上显示服务")
        try:
            from gi.repository import GLib
            self._loop = GLib.MainLoop()
        except Exception as exc:                        # noqa: BLE001
            return UiOutcome(action="failed", note=f"{type(exc).__name__}: {exc}")
        try:
            self.build()
        except Exception as exc:                        # noqa: BLE001 建窗失败要能退回无头路径
            return UiOutcome(action="failed", note=f"{type(exc).__name__}: {exc}")
        self._present_if_review()
        try:
            self._loop.run()
        except KeyboardInterrupt:
            self._finish("cancel")
        # 主循环退出时窗口已经销毁 ⇒ WM 把焦点自然还给原窗口，调用方这时候才注入
        return UiOutcome(action=self._action or "closed", text=self.text, note=self.note)

    def _apply_mode(self, plan: dict) -> None:
        """唯一的设窗口属性出口：一切按 `window_plan` 的表来，别处不许自己调。"""
        if self._window is None:
            return
        _Gtk, _Gdk, _Pango = _gtk()
        self._window.set_decorated(plan["decorated"])
        self._window.set_accept_focus(plan["accept_focus"])
        self._window.set_skip_taskbar_hint(plan["skip_taskbar"])
        self._window.set_skip_pager_hint(plan["skip_pager"])
        self._view.set_editable(plan["editable"])
        self._view.set_cursor_visible(plan["editable"])

    def _buffer_text(self, text: str) -> None:
        self._view.get_buffer().set_text(text)

    def _current_text(self) -> str:
        buf = self._view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def _show_notice(self, note: str) -> None:
        self.mode = "notice" if self._window else self.mode
        self.note = note
        if self._label:
            self._label.set_text(note)
        if self._window:
            self._apply_mode(window_plan("notice"))

    def _show_review(self) -> None:
        self.mode = "review"
        self.note = LABELS["review_title"]
        if not self._window:
            return
        self._apply_mode(window_plan("review"))
        self._label.set_text(LABELS["review_title"])
        self._buffer_text(self.text)
        self._window.show_all()
        # review 才允许 present()/grab_focus()，而且**只对自己的控件**
        self._window.present()
        self._view.grab_focus()

    def _hide_window(self) -> None:
        if self._window:
            self._window.hide()

    def _on_key(self, _widget, event):
        """键盘：判定交给纯函数 `key_action`，这里只执行。"""
        Gtk, Gdk, _Pango = _gtk()
        name = Gdk.keyval_name(event.keyval) or ""
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        action = key_action(name, self.mode, shift=shift, ctrl=ctrl)
        if action == "commit":
            self.text = text_for_display(self._current_text())
            self._finish("commit")
            return True
        if action == "cancel":
            self._finish("cancel")
            return True
        if action == "paste_hint":
            self._show_notice("弹窗里有焦点，Ctrl+V 只会贴进这里 —— "
                              "先 Esc 关掉，再在目标位置粘贴")
            return True
        return False                                        # newline / ignore 交给 GTK 默认处理

    def _finish(self, action: str) -> None:
        if self._done:
            return
        self._done = True
        self._action = action
        if self._window is not None:
            try:
                self._window.destroy()                     # 销毁 = 焦点自然归还，不做任何手动抢回
            except Exception:                              # noqa: BLE001
                pass
            self._window = None
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
        if self.on_finish is not None:
            # **窗口销毁之后**才把结局交回 worker（注入必须排在销毁后，见模块 docstring 第 3 条）。
            # idle_add 把它排到主循环的下一轮 —— worker 的主循环跑在主线程上。
            outcome = UiOutcome(action=self._action or "closed", text=self.text, note=self.note)
            callback, self.on_finish = self.on_finish, None
            try:
                from gi.repository import GLib
                GLib.idle_add(lambda: (callback(outcome), False)[1])
            except Exception:                              # noqa: BLE001 没 GLib 的直调路径：当场叫
                callback(outcome)
