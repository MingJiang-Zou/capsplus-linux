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
  4. **"先关窗"过去只是一句承诺，现在是一份数据**（2026-10-17 补）：本文件把窗口的真实
     生命周期 `map` / `unmap` / `focus-in` / `focus-out` / `destroy` 记成**纯数据时间线**
     （`VoiceUi.timeline`，条目只有 `{kind, us}`），注入方读它来决定"我确不在焦点链上了吗"，
     诊断也把它原样写进日志。**不许只把这件事写在日志散文里** —— 散文没法断言，也没法反例。
     注意"拿到 unmap/focus-out"是**我们这一侧**的事实：Wayland 不保证合成器会把它交给谁，
     所以时间线只能证实"我们撤了"，证不了"焦点回到了原文档"；证不了的那部分由
     `voice_worker` 标成**未确认**，不许写成成功。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from functools import partial

MODES = ("recording", "review", "notice")
# 录音心跳事件的类型名。**单独拎出来当常量**：`_emit` 要用它做"没窗口就丢"的判断，
# 写字面量两处迟早漂开，漂开的后果是心跳又被攒起来（就是它要防的那个 bug）。
PROGRESS_KIND = "progress"
# 窗口生命周期里我们要记下来的五种信号（`focus_in`/`focus_out` 连的是 GTK 的
# `focus-in-event`/`focus-out-event`，见 `LIFECYCLE_SIGNALS`）。
LIFECYCLE_KINDS = ("map", "unmap", "focus_in", "focus_out", "destroy")
# 其中能算"**我已经不在焦点链上**"的事实 —— 注入必须等到其中之一，等不到就标未确认。
RELEASE_KINDS = ("unmap", "focus_out")
# 我们自己打的点（不是信号）：`finish` = 用户按下 Enter/Esc 的那一刻，诊断算间隔用它。
MARKER_KINDS = ("finish",)
LIFECYCLE_SIGNALS = {"map": "map", "unmap": "unmap", "focus_in": "focus-in-event",
                     "focus_out": "focus-out-event", "destroy": "destroy"}
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


# 录音指示条那块小窗的尺寸。窄而矮：它是贴在屏幕中间的一块牌子，不是面板。
BADGE_WIDTH = 460
BADGE_HEIGHT = 76
# 电平判读的两条线，**按 VAD 当次的门限**算（不另写一个绝对阈值 ——
# 这个仓为"门限的第二份真相"付过两次代价，见 voice_levels 的模块 docstring）。
#   > 门限        ⇒ 这一帧在 VAD 眼里就是"有声"，与判定完全一致；
#   > 门限 × 0.3  ⇒ 还没过门但明显有动静（说话声偏轻、或离得远）；
#   再低         ⇒ 就是底噪／没在说话。
QUIET_RATIO = 0.3


def level_verdict(rms: float, gate: float) -> str:
    """"现在这一帧算不算有声"的**唯一**一处判读（牌子与 `meter --live` 共用）。

    `gate <= 0`（还没校准完）时返回 "—"：**宁可少说，不许编** —— 拿一个假的判读去
    指导用户调麦克风，比不判读更糟。
    """
    if gate <= 0:
        return "—"
    if rms > gate:
        return "有声"
    if rms > gate * QUIET_RATIO:
        return "偏轻"
    return "还没听到说话"


def recording_line(elapsed_ms: int, rms: float, peak: int, gate: float = 0.0,
                   max_ms: int = 0) -> str:
    """录音**进行中**那块牌子上的一行字（纯函数，无 gi 也能测）。

    为什么要有（2026-10-20 用户原话「只可以是短语音」）：上一版录音期屏幕上什么都没有，
    于是"它到底还在不在录、有没有听到我说话、已经录了多久"三件事全靠猜；
    而端点判定恰恰是**按静音时长**下刀的，用户看不见就会以为"它自己断了"。
    这条把三件事都摆在脸上：时长 / 电平读数 / 电平判读。

    `gate` 传 0（比如还没校准完）时不猜判读，只报数（判读规则见 `level_verdict`）。
    """
    parts = [f"● 录音中 {elapsed_ms / 1000.0:.1f}s", f"电平 RMS {rms:.0f} / 峰 {peak}"]
    if gate > 0:
        verdict = level_verdict(rms, gate)
        if verdict == "偏轻":
            verdict += "（凑近一点）"
        parts.append(f"{verdict}（门限 {gate:.0f}）")
    if max_ms > 0 and elapsed_ms >= max_ms - 5000:
        parts.append(f"快到 {max_ms / 1000:.0f}s 上限了")
    return " · ".join(parts)


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


# ── 时间线：时基与读取（纯数据，没有 gi 也能跑）────────────────────────────────
_CLOCK = None


def _glib_clock_us() -> int:
    from gi.repository import GLib
    return int(GLib.get_monotonic_time())


def _system_clock_us() -> int:
    return int(time.monotonic() * 1_000_000)


def now_us() -> int:
    """单调微秒时基。首选 `GLib.get_monotonic_time()`，这台机没 PyGObject 才退 `time.monotonic`。

    **进程内只选一次**并记住：两个时钟的纪元不同，混着用算出来的间隔是垃圾（比没诊断更糟）。
    """
    global _CLOCK
    if _CLOCK is None:
        try:
            _glib_clock_us()
            _CLOCK = _glib_clock_us
        except Exception:                                # noqa: BLE001 ImportError / 坏 typelib
            _CLOCK = _system_clock_us
    return _CLOCK()


def focus_released(timeline, after_us=None) -> dict:
    """从**纯时间线**回答"焦点交出了没有" —— 不碰 GTK，单测直接喂一个 list 就能断言。

    返回的 `release_kind` 是**哪一种信号**算的（`unmap`/`focus_out`），`at_us` 是它的时刻；
    worker 诊断里那个 `gate=` 说的是"谁让的这一步"（event/floor/cap），两件事别混。

    `after_us=None` 时取时间线里 `finish`（用户按下 Enter）那个打点的时刻：在那之前来过的
    `focus_out`（用户自己点了一下别处）不能算"我们把窗口撤了"。
    只统计 **finish 之后**的条目，返回的 `unmap_seen` / `focus_out_seen` 也是这个口径 ——
    诊断行要回答的是"自 Enter 以来"，不是"这辈子"。
    """
    entries = [e for e in (timeline or []) if isinstance(e, dict)]
    marks = [e for e in entries if e.get("kind") == "finish"]
    if after_us is None:
        after_us = int(marks[-1].get("us", 0)) if marks else 0
    after_us = int(after_us)
    out = {"released": False, "release_kind": "", "at_us": 0, "enter_ms": None,
           "finish_us": int(marks[-1].get("us", 0)) if marks else 0,
           "unmap_seen": False, "focus_out_seen": False, "destroy_seen": False,
           "mapped_by_timeline": None, "entries": len(entries)}
    for entry in entries:
        kind = entry.get("kind")
        us = int(entry.get("us", 0) or 0)
        if kind == "map":
            out["mapped_by_timeline"] = True
        elif kind == "unmap":
            out["mapped_by_timeline"] = False
        elif kind == "destroy":
            out["mapped_by_timeline"] = False
        if us < after_us:
            continue
        if kind == "unmap":
            out["unmap_seen"] = True
        elif kind == "focus_out":
            out["focus_out_seen"] = True
        elif kind == "destroy":
            out["destroy_seen"] = True
        if kind in RELEASE_KINDS and not out["released"]:
            out.update(released=True, release_kind=kind, at_us=us)
    if out["released"] and out["finish_us"]:
        out["enter_ms"] = round((out["at_us"] - out["finish_us"]) / 1000.0, 1)
    return out


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

    def __init__(self, title: str = DEFAULT_TITLE, on_finish=None, clock=None):
        self.on_finish = on_finish        # callable(UiOutcome)，只会被叫一次
        self.title = title
        self.mode = "recording"
        self.note = LABELS["recording"]
        self._review_hint = ""            # review 页顶栏的覆盖文案（`set_review_hint`）
        self.text = ""
        # 窗口真实生命周期：**纯数据**时间线（条目只有 {kind, us}），注入方与诊断都读它。
        self.timeline: list[dict] = []
        self._clock = clock or now_us
        self._release_waiters: list = []
        self._pending: list[tuple] = []
        self._window = None
        self._probe_widget = None          # 销毁后**只留着读状态量**的那份窗口引用
        self._view = None
        self._scroll = None
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

    def set_review_hint(self, hint: str) -> None:
        """review 页顶部那行字的**一次性覆盖**（默认是 `LABELS["review_title"]`）。

        给"录满上限被截断"这种**必须被看见**的情况用（2026-10-20）：文本本身是对的，
        少的只是后半句 —— 用户必须知道"不是我没说清楚，是它录到 60s 就停了"，
        否则下次还会以为识别不准。**必须在 `post_text()` 之前调**：建窗那一刻才读这个值。
        """
        self._review_hint = hint

    def post_error(self, code: str, message: str = "", hint: str = "") -> None:
        self._emit(("error", code, message, hint))

    def post_progress(self, elapsed_ms: int, rms: float, peak: int,
                      gate: float = 0.0, max_ms: int = 0) -> None:
        """录音中的心跳：把"还在录 / 录了多久 / 现在多响"刷到那块牌子上。

        **没有窗口时直接丢掉**（不是攒着）—— 见 `_emit` 的 `PROGRESS_KIND` 分支。
        """
        self._emit((PROGRESS_KIND, int(elapsed_ms), float(rms), int(peak),
                    float(gate), int(max_ms)))

    def close(self) -> None:
        self._emit(("close", self._action or "closed"))

    def _emit(self, event: tuple) -> None:
        if self._done:                       # show() 已经返回过：会话结束，别再攒事件
            return
        if self._window is None:
            # 心跳类事件**只在有窗口时才有意义**：攒着它们会在建窗那一刻一次放出来
            # （录音 60s = 60 次刷新），把真正要显示的 note/文本淹掉。
            if event[0] == PROGRESS_KIND:
                return
            self._pending.append(event)
            return
        try:
            from gi.repository import GLib
            GLib.idle_add(self._apply, event)
        except Exception:                                # noqa: BLE001
            self._apply(event)

    # -- 生命周期时间线 ---------------------------------------------------------
    def _record(self, kind: str, *ignored) -> None:
        """生命周期**唯一的写入点**：一条 `{kind, us}` 进时间线；焦点交出的事实顺本通知等待者。

        它是 GTK 信号处理器（`partial` 把 `kind` 放在最前，widget/事件都落到 `ignored`），
        所以返回值必须是 None —— 返回 True 会把 `focus-out-event` 之类吃掉，默认处理就断了。
        等待者的回调异常**只记不下**：这里抛出去会把信号发射链打断（`_guarded` 那套教训）。
        """
        self.timeline.append({"kind": kind, "us": int(self._clock())})
        if kind not in RELEASE_KINDS:
            return
        waiters, self._release_waiters = self._release_waiters, []
        for callback in waiters:
            try:
                callback(self.release_fact())
            except Exception:                            # noqa: BLE001 诊断不许掀翻主循环
                pass

    def release_fact(self) -> dict:
        """`focus_released(self.timeline)` 的实例口径：Enter 之后焦点交出了没有。"""
        return focus_released(self.timeline)

    def on_release(self, callback) -> bool:
        """焦点一交出就在**主线程**（信号上下文里）叫一次 `callback(fact)`。

        已经交过了就当场叫，返回 True（调用方据此**不必**再挂兜底定时器）。
        只通知一次：`_record` 拿走列表就地清空，第二次 unmap/focus_out 不会再叫。
        """
        fact = self.release_fact()
        if fact["released"]:
            callback(fact)
            return True
        self._release_waiters.append(callback)
        return False

    def probe(self) -> dict:
        """注入那一刻的**窗口状态量**（可见 / 已实化 / 还 mapped + 焦点事实）。不含任何文本。

        三个布尔分开读：一个读不到不牵连另外两个（本轮实测：`is_realized()` 这名字在
        GTK3 里**不存在**，是 `get_realized()`；写错时被 except 一口吞掉，三个状态量一起变
        `na`，诊断就白装了 —— 所以现在每个各包一次，并且判据逐字钉住它们）。
        `mapped` 直接问 GTK3 的 `get_mapped()`，同时给一份从时间线推出来的
        `mapped_by_timeline` 对照 —— 两个不一致本身就是诊断信息。
        销毁后的窗口对象只用来读这三个布尔，别的动作一个都不做（Xvfb 实测：destroy 之后
        三个 getter 都还能读，返回 False）。
        """
        fact = self.release_fact()
        fact["visible"] = None
        fact["realized"] = None
        fact["mapped"] = None
        # 时间线原样带出去（只有 kind 与 us）：诊断行要打印它，判据要断言它的顺序。
        # 反例具型：这行漏掉时界面判据看到的 `timeline` 是空的，与假件不一致 ——
        # 那种"假件绿、真窗口空"的漂移由 `TestFakeUiMatchesTheRealOne` 钉住。
        fact["timeline"] = [dict(entry) for entry in self.timeline]
        widget = self._window if self._window is not None else self._probe_widget
        if widget is None:
            return fact
        for key, getter in (("visible", "get_visible"), ("realized", "get_realized"),
                            ("mapped", "get_mapped")):
            try:
                fact[key] = bool(getattr(widget, getter)())
            except Exception:                                    # noqa: BLE001 对象已被收走
                fact[key] = None
        return fact

    def forget(self) -> None:
        """丢掉"只为诊断留着"的窗口引用（注入决策做完调用，别让一个 widget 常驻）。"""
        self._probe_widget = None
        self._release_waiters = []

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
        elif kind == PROGRESS_KIND:
            _, elapsed_ms, rms, peak, gate, max_ms = event
            if self._window is not None and self.mode == "recording":
                self.note = recording_line(elapsed_ms, rms, peak, gate, max_ms)
                if self._label is not None:
                    self._label.set_text(self.note)
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
        self._probe_widget = self._window
        self._window.set_title(self.title)
        self._window.set_default_size(REVIEW_WIDTH, REVIEW_HEIGHT)
        self._window.set_position(Gtk.WindowPosition.CENTER)
        self._window.connect("delete-event", lambda *_a: (self._finish("closed"), True)[1])
        self._window.connect("key-press-event", self._on_key)
        for kind, signal in LIFECYCLE_SIGNALS.items():
            self._window.connect(signal, partial(self._record, kind))

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
        self._scroll = scroll
        self._window.add(box)

        self._apply_mode(window_plan(self.mode))
        for event in list(self._pending):
            self._apply(event)
        self._pending.clear()
        self._window.show_all()

    def open_badge(self) -> bool:
        """录音期那块指示牌。**默认关**（`CAPSPLUS_VOICE_BADGE=1` 才开，理由见模块 docstring 第 1 条：
        Wayland 没有"别把焦点给我"这个请求，所以"绝不抢焦点"的唯一可靠实现是那时候没有窗口）。

        返回 True = 已经挂在屏幕上，调用方随后可以 `post_progress()` 刷它。
        必须在**主线程**调（建窗不是线程安全的，口径同 `open_review`）。
        """
        if not badge_enabled():
            return False
        if self._window is not None:
            return True
        if threading.current_thread() is not threading.main_thread():
            return False
        try:
            self.build()
        except Exception:                                 # noqa: BLE001 开不了牌子不该影响录音
            return False
        self.mode = "recording"
        self._apply_mode(window_plan("recording"))
        if self._scroll is not None:
            # 牌子期不要文本框：`set_no_show_all` 是 GTK3 里唯一能让 `show_all()`
            # 也跳过它的写法（`hide()` 会被后面的 show_all 覆盖回来，实测过）。
            self._scroll.set_no_show_all(True)
            self._scroll.hide()
        self._window.set_default_size(BADGE_WIDTH, BADGE_HEIGHT)
        self._window.resize(BADGE_WIDTH, BADGE_HEIGHT)
        self._window.show_all()
        return True

    def close_badge(self) -> None:
        """牌子用完就收。它只是块指示牌，**没有结局要交回**，所以不进 review 那条回调链。"""
        if self._window is not None:
            self._finish("badge-closed")

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
        self.note = self._review_hint or LABELS["review_title"]
        if not self._window:
            return
        self._apply_mode(window_plan("review"))
        self._label.set_text(self.note)
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
        # 先打点再销毁：`enter_ms` 要量的是"用户按下 Enter → 注入"这一整段，
        # 从销毁之后才开始算会把最可疑的那几十毫秒洗掉。
        self.timeline.append({"kind": "finish", "us": int(self._clock()), "action": action})
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
