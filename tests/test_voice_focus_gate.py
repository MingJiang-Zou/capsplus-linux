# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""上屏注入的**触发条件**判据（2026-10-17，用户真机：文字"贴上又被删掉，闪烁了一下"）。

修前只有一个定时器：窗口销毁之后固定等 `FOCUS_RETURN_MS = 120ms` 就发 Ctrl+V。
"过了 120 毫秒"不等于"焦点已经回到原文档" —— 而 Ctrl+V 落进我们**自己**那个正在消失的弹窗，
用户看到的就是"字进去了、又随窗口没了"。这次把触发条件改成事实：

    必要条件  时间线里出现 `unmap` 或 `focus_out`（`voice_osd.RELEASE_KINDS`）
    下限      `INJECT_FLOOR_MS`（事实到得再早也不许更快，别把现状反而提早）
    上限兜底  `INJECT_WAIT_CAP_MS`（等不到事实也要带诊断注入，并把这一次标成**未确认**）

四条判据，每条都带**能红的反例具型**：

  1. `TestTimelineIsPureData` —— 时间线是纯数据（喂 list 就能断言，不需要 GTK）：
     反例具型 = "焦点交出的信号在 Enter 之前就来过" 不许被算成交出。
  2. `TestGateWaitsForTheFact` —— 事件路径：事实来了才注入、且**早于**注入那一刻；
     反例具型 = 修前的"固定 120ms 立即发"（`legacy_arm_inject` 原样内联），同一个判据函数必须红。
  3. `TestCapFallbackIsNotInfinite` —— 兜底路径：永远收不到信号的窗口也要在上限内注入，
     诊断里写明"未观察到焦点交出"，状态是**未确认**而不是"上屏结束"；
     反例具型 = 修前那行 `note="上屏结束"`（`legacy_idle_note`）必须红。
  4. `TestDiagnosticsCarryNoText` —— 隐私：新加的诊断字段（诊断行 / verdict / 状态帧）
     照本仓「念一句银行卡号」那条写法，一个字都不许漏识别文本与剪贴板内容。

真实窗口那条（GTK 信号到底会不会来、来得多快）在 `tests/test_voice_mainloop.py` 的
`CAPSPLUS_GUI=1` 界面判据里，本文件只用假窗口——CI 没 gi，而且合成器给不出可控的时刻。
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import test_voice_portability as TP            # noqa: E402  复用同一套 GUI 闸门（别造第二份）
from capsplus import voice_inject as INJ         # noqa: E402
from capsplus import voice_osd as OSD            # noqa: E402
from capsplus import voice_proto as P            # noqa: E402
from capsplus import voice_worker as W           # noqa: E402

SECRET = "我把银行卡号念出来了"          # 识别文本探针（照 test_voice_proto 那条判据）
CARD = "6222021234567890123"             # 躺在剪贴板里的卡号探针（照 test_voice_clipboard）
PROBE = INJ.Probe(wl_copy="/usr/bin/wl-copy", wl_paste="/usr/bin/wl-paste",
                  wl_copy_clear=True, wl_copy_primary=True, ydotool="/usr/bin/ydotool",
                  syntax=("keycode", "click-bitmask"),
                  keycodes={"KEY_LEFTCTRL": 29, "KEY_V": 47}, ydotool_daemon=True,
                  keycodes_source="/tmp/h")


class FakeClock:
    """可推进的假单调时钟（微秒，和 `voice_osd.now_us()` 同单位）。"""

    def __init__(self, start_us: int = 1_000_000):
        self.us = start_us

    def __call__(self) -> int:
        return self.us

    def advance_ms(self, ms: float) -> None:
        self.us += int(ms * 1000)


class FakeUi:
    """形状与真 `voice_osd.VoiceUi` 一致的**假窗口**：只回答 worker 会问的那几个问题。

    `schedule` = [(Enter 之后多少毫秒, 事件)] —— 到点由 `pump()` 补进时间线并通知等待者，
    所以"事实来得比下限晚""事实永远不来"这两种时序都能精确造出来（真合成器造不出）。
    """

    def __init__(self, clock: FakeClock, base_us: int, schedule=(), keep_window=True):
        self.clock = clock
        self.base_us = base_us
        # `keep_window` = 假装的"合成器还没把窗口收走"：可见/实化/mapped 全都还是真的
        self.visible = self.realized = self.mapped = keep_window
        self.timeline = [{"kind": "map", "us": base_us - 8_000},
                         {"kind": "focus_in", "us": base_us - 7_000},
                         {"kind": "finish", "us": base_us, "action": "commit"}]
        # 按毫秒排序但**保持给定顺序**（destroy 与 unmap 同一时刻时，先 unmap 后 destroy 才是
        # GTK 的真实形状；按元组字母序排会变成 destroy 在前，判据就验不到东西了）
        self._schedule = [(base_us + int(ms * 1000), kind)
                          for ms, kind in sorted(schedule, key=lambda item: item[0])]
        self._waiters: list = []

    # -- 测试侧的手：把到点的信号补进时间线（等价于 GTK 发信号）--------------------
    def pump(self) -> None:
        while self._schedule and self._schedule[0][0] <= self.clock.us:
            us, kind = self._schedule.pop(0)
            self.timeline.append({"kind": kind, "us": us})
            if kind in OSD.RELEASE_KINDS:
                self.mapped = False
                self.visible = False
                self.realized = False
                waiters, self._waiters = self._waiters, []
                for callback in waiters:
                    callback(self.release_fact())

    # -- worker 会问的四个问题（口径与真 VoiceUi 一致）---------------------------
    def release_fact(self) -> dict:
        return OSD.focus_released(self.timeline)

    def on_release(self, callback) -> bool:
        fact = self.release_fact()
        if fact["released"]:
            callback(fact)
            return True
        self._waiters.append(callback)
        return False

    def probe(self) -> dict:
        fact = self.release_fact()
        fact.update(visible=self.visible, realized=self.realized, mapped=self.mapped,
                    timeline=[dict(e) for e in self.timeline])
        return fact

    def forget(self) -> None:
        self._waiters = []


def gate_is_confirmed(diag: dict) -> bool:
    """**判据本体**（纯函数）：这一次注入有证据吗 —— 焦点交出的事实存在，且**早于**注入。

    交付的代码走这条，反例具型（修前的"固定 120ms 立即发"）也走这条；后者必须红。
    """
    gate = diag.get("gate")
    if gate == "nowin":                       # 本次没有弹窗：没有"贴进自己"这回事
        return True
    if gate == "cap":                         # 到上限仍没观察到 ⇒ 定义上就是未确认
        return False
    if not diag.get("released"):
        return False
    inject_us = int(diag.get("inject_us") or 0)
    return any(entry.get("kind") in OSD.RELEASE_KINDS and int(entry.get("us", 0) or 0) <= inject_us
               for entry in (diag.get("timeline") or []))


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """等后台线程把活干完。**只有测试侧在轮询**（被测代码里一处 sleep 都不许有）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return bool(predicate())


class ThreadTracker:
    """代理 `voice_worker` 里那个 `threading`：记下每个被创建的线程，收工时**全部 join**。

    为什么必须 join：`P.log_line` 是"读全文件→改→写回"，而状态目录是从**当时生效的**环境
    变量算的。别的用例遗留的后台线程会写进下一个用例正在用的那份日志里，一次读-改-写就把
    别人的行冲掉（本轮实测：`restore_diag branch=restored` 就是这么丢的 —— 那种假红比没判据
    更糟，因为它看起来像被测代码的错）。
    """

    def __init__(self):
        self.threads: list = []

    def Thread(self, *args, **kw):
        thread = threading.Thread(*args, **kw)
        self.threads.append(thread)
        return thread

    def __getattr__(self, name):
        return getattr(threading, name)

    def join_all(self, timeout: float = 5.0) -> None:
        for thread in list(self.threads):
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout)


class Harness:
    """一台假 worker：假状态目录（**绝不碰用户的真目录**）+ 假剪贴板 + 手动跑的时间轴。"""

    def __init__(self, text=SECRET, clipboard=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = self.tmp.name
        self.clock = FakeClock()
        self.base_us = self.clock.us
        self.text = text
        self.clipboard = dict(clipboard or {})
        self.calls: list = []
        self.events: list = []
        self.armed: list = []          # after_ms 挂出去的 (到点, 回调)：不被 run_ms 吃掉的那份账
        self.tracker = ThreadTracker()
        self._patch_threading = None
        self.srv = None
        self.session = None
        self.ui = None

    # -- 假 Wayland 剪贴板：`wl-paste` 回读得到我们写进去的那段 -----------------
    def run(self, argv, **kw):
        argv = list(argv)
        payload = kw.get("input")
        self.calls.append({"argv": argv, "input": payload, "t": self.clock.us,
                           "thread_main": threading.current_thread() is threading.main_thread()})
        tool = os.path.basename(argv[0])
        rc, out = 0, b""
        if tool == "wl-copy":
            if "--clear" in argv and not payload:
                self.clipboard.clear()
            elif "--type" in argv:
                self.clipboard[argv[argv.index("--type") + 1]] = payload or b""
            else:
                # 真 wl-copy 写纯文本会一次注册齐这两种目标（`restore_steps` 的注释）
                self.clipboard["text/plain;charset=utf-8"] = payload or b""
                self.clipboard["text/plain"] = payload or b""
        elif tool == "wl-paste":
            if "--list-types" in argv:
                out = "\n".join(self.clipboard.keys()).encode("utf-8")
            else:
                want = argv[argv.index("--type") + 1] if "--type" in argv else None
                key = want if want in self.clipboard else next(iter(self.clipboard), None)
                if key is None:
                    rc = 1                       # 假件不许什么都答应：空的就报错
                else:
                    out = self.clipboard.get(key, b"")
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": b""})()

    def server(self):
        srv = W.Server(run_command=self.run, review_ui=True)
        srv.backend = "clipboard"
        srv.probe = lambda: PROBE
        srv.broadcast = self.events.append
        srv._deferred = []

        def after_ms(ms, fn):                 # 假主循环：只记账，**由测试按时间轴放行**
            due = (self.clock.us + int(ms * 1000), fn)
            srv._deferred.append(due)
            self.armed.append(due)

        srv.after_ms = after_ms
        if self._patch_threading is None:            # 同一个 harness 可以建两次 server()
            self._patch_threading = mock.patch.object(W, "threading", self.tracker)
            self._patch_threading.start()
        self.srv = srv
        self.session = srv._ensure_session()
        self.session.text = self.text
        return srv

    def ui_with(self, schedule=(), keep_window=True):
        self.ui = FakeUi(self.clock, self.base_us, schedule, keep_window=keep_window)
        return self.ui

    # -- 手动跑时间轴：到点就先补窗口信号、再跑定时器 ---------------------------
    def run_ms(self, ms: float) -> None:
        """把"主循环到点"按时间顺序演一遍。**被测代码里没有真等待**，所以这里也不睡。"""
        end = self.base_us + int(ms * 1000)
        guard = 0
        while guard < 500:
            guard += 1
            timers = [(us, i) for i, (us, _fn) in enumerate(self.srv._deferred) if us <= end]
            next_ui = self.ui._schedule[0][0] if self.ui._schedule else None
            if not timers and (next_ui is None or next_ui > end):
                break
            if next_ui is not None and next_ui <= end and (not timers or next_ui <= timers[0][0]):
                self.clock.us = max(self.clock.us, next_ui)
                self.ui.pump()
                continue
            us, i = min(timers)
            self.clock.us = max(self.clock.us, us)
            self.ui.pump()
            self.srv._deferred.pop(i)[1]()
        self.clock.us = max(self.clock.us, end)
        if self.ui is not None:
            self.ui.pump()

    def commit_round(self, schedule=(), keep_window=True, run_ms=600):
        """按真机的顺序走"Enter 之后"那一跳：先置 review，再把结局交给 worker，然后跑时间轴。

        为什么必须先置 review：`Session` 建出来就是 `idle`，直接调 `_on_review_outcome` 的话
        `state == "idle"` 从一开始就成立 —— 本轮判据差点就把"会话走完了"验成一句空话。
        """
        self.server()
        self.ui_with(schedule, keep_window=keep_window)
        self.session._set_state("review", note=f"待确认 {len(self.text)} 字")
        self.session._ui = self.ui
        self.session._on_review_outcome(OSD.UiOutcome(action="commit", text=self.text))
        self.run_ms(run_ms)
        return self.session

    def drain(self, rounds: int = 3) -> None:
        """把挂在账上的定时器一律放行，并收回它们派出去的线程。

        剪贴板还原那一跳是**注入线程**调 `arm_clipboard_restore` 时才挂上去的，
        时间轴跑到那个时刻之前它还不存在 —— 所以必须 settle → drain → settle 两轮。
        """
        for _ in range(rounds):
            due = sorted(self.srv._deferred, key=lambda item: item[0]) if self.srv else []
            if self.srv:
                self.srv._deferred.clear()
            if not due:
                return
            for _us, fn in due:
                fn()
            self.settle()

    def settle(self, timeout: float = 8.0) -> bool:
        """把派出去的后台线程全部收回来。

        `P.log_line` 是"读全文→改→写回"，并发写会**丢行**；线程没收回就读日志，读到的
        是半份现场（本轮实测撞过一次：`restore_diag` 那行明明写了却被别的线程冲掉）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.tracker.join_all(max(0.05, deadline - time.monotonic()))
            if not any(t.is_alive() for t in self.tracker.threads):
                return True
        return False

    # -- 收尾 -----------------------------------------------------------------
    def log_text(self) -> str:
        path = P.log_path()
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def cleanup(self):
        # 顺序要紧：先等自己派出去的线程把日志写完，再撤 `threading` 代理，最后才是删目录。
        self.settle()
        patch = getattr(self, "_patch_threading", None)
        if patch is not None:
            patch.stop()
            self._patch_threading = None
        self.tmp.cleanup()


# ── 假件与真窗口不许漂（本轮实测撞过：真 `probe()` 少带一个 `timeline`）──────────
class TestFakeUiMatchesTheRealOne(unittest.TestCase):
    """假窗口与真窗口的 `probe()` / `release_fact()` **字段名册必须一致**。

    为什么单独钉一条：本文件的时序判据全跑在假窗口上，真窗口那条只在 `CAPSPLUS_GUI=1` 跑；
    两边形状一漂就会出现"假件绿、真机什么都没记到"—— 本轮真撞过一次（真 `VoiceUi.probe()`
    漏了 `timeline`，界面判据立刻红在 `kinds == []` 上，那是它第一次替我抓 bug）。
    """

    def test_probe_key_sets_match(self):
        real = OSD.VoiceUi().probe()
        fake = FakeUi(FakeClock(), 1000, [(5, "unmap")])
        fake.pump()
        self.assertEqual(set(real), set(fake.probe()),
                         "真窗口与假窗口的诊断形状漂了：两边有一边会绿得没有意义")

    def test_release_fact_key_sets_match(self):
        self.assertEqual(set(OSD.VoiceUi().release_fact()),
                         set(FakeUi(FakeClock(), 1000).release_fact()))

    def test_the_real_ui_records_lifecycle_without_a_window(self):
        """真 `VoiceUi._record` 不依赖窗口存在：时间线是**纯数据**，先记再说。"""
        ui = OSD.VoiceUi()
        ui._record("map", object())                 # 信号处理器形状：kind 在最前，其余全忽略
        ui._record("focus_in", object())
        self.assertFalse(ui.release_fact()["released"])
        ui._record("unmap", object())
        fact = ui.release_fact()
        self.assertTrue(fact["released"])
        self.assertEqual(fact["release_kind"], "unmap")
        self.assertEqual([e["kind"] for e in ui.timeline], ["map", "focus_in", "unmap"])
        self.assertIs(ui.probe()["mapped_by_timeline"], False)

    def test_on_release_notifies_once_and_only_once(self):
        ui = OSD.VoiceUi()
        seen = []
        ui.on_release(lambda fact: seen.append(fact["release_kind"]))
        ui._record("map", object())
        self.assertEqual(seen, [], "map 不是焦点交出，不许叫醒等待者")
        ui._record("focus_out", object())
        ui._record("unmap", object())
        self.assertEqual(seen, ["focus_out"], f"焦点交出只许通知一次，实际：{seen}")
        self.assertTrue(ui.on_release(lambda fact: seen.append("late")),
                        "已经交出了就该当场回调")
        self.assertEqual(seen, ["focus_out", "late"])


# ── 判据 1：时间线是纯数据，读取口径不许糊 ──────────────────────────────────────
class TestTimelineIsPureData(unittest.TestCase):
    """`voice_osd.focus_released()` 只吃 list，所以没有 gi 也能把口径钉死。"""

    def test_unmap_alone_counts_as_released(self):
        fact = OSD.focus_released([{"kind": "finish", "us": 1000},
                                   {"kind": "unmap", "us": 2000}])
        self.assertTrue(fact["released"])
        self.assertEqual(fact["release_kind"], "unmap")
        self.assertEqual(fact["at_us"], 2000)
        self.assertEqual(fact["enter_ms"], 1.0)
        self.assertTrue(fact["unmap_seen"])

    def test_focus_out_alone_counts_as_released(self):
        fact = OSD.focus_released([{"kind": "finish", "us": 1000},
                                   {"kind": "focus_out", "us": 1500}])
        self.assertTrue(fact["released"])
        self.assertEqual(fact["release_kind"], "focus_out")
        self.assertTrue(fact["focus_out_seen"])
        self.assertIsNone(fact.get("mapped_by_timeline"),
                          "focus_out 不说明窗口被 unmap：两个事实不许混成一个")

    def test_counterexample_release_before_enter_does_not_count(self):
        """反例具型：Enter **之前**来过的 focus_out（用户自己点了别处）不许算成"我们撤了"。"""
        fact = OSD.focus_released([{"kind": "focus_out", "us": 500},
                                   {"kind": "finish", "us": 1000}])
        self.assertFalse(fact["released"], "把 Enter 之前的 focus_out 算成交出 = 判据是摆设")
        self.assertFalse(fact["focus_out_seen"], "seen 的口径也必须是「自 Enter 以来」")
        self.assertIsNone(fact["enter_ms"])

    def test_counterexample_empty_timeline_is_not_released(self):
        for timeline in ([], None, [{"kind": "map", "us": 1}, {"kind": "destroy", "us": 2}]):
            fact = OSD.focus_released(timeline)
            self.assertFalse(fact["released"], f"{timeline} 不该被判成焦点已交出")
            self.assertFalse(gate_is_confirmed(dict(fact, gate="event", inject_us=10**9)),
                             "没有事实还放行 = 判据是摆设")

    def test_map_unmap_track_the_mapped_state(self):
        timeline = [{"kind": "map", "us": 1}, {"kind": "finish", "us": 2},
                    {"kind": "unmap", "us": 3}]
        self.assertIs(OSD.focus_released(timeline)["mapped_by_timeline"], False)
        self.assertIs(OSD.focus_released(timeline[:1])["mapped_by_timeline"], True)

    def test_entries_only_carry_kind_and_timestamp(self):
        """时间线条目是**纯数据**：只有 kind 与 us（外加 finish 的 action），别的什么都没有。"""
        clock = FakeClock()
        ui = FakeUi(clock, clock.us, [(0, "unmap"), (1, "destroy")])
        clock.advance_ms(5)
        ui.pump()
        for entry in ui.timeline:
            self.assertEqual(set(entry), {"kind", "us"} | ({"action"} if entry["kind"] == "finish"
                                                           else set()),
                             f"时间线里混进了不该有的字段：{entry}")
            self.assertIn(entry["kind"], OSD.LIFECYCLE_KINDS + OSD.MARKER_KINDS)
        self.assertNotIn(SECRET, json.dumps(ui.timeline, ensure_ascii=False))

    def test_clock_prefers_glib_and_never_mixes_epochs(self):
        """时基只选一次：混用 `GLib.get_monotonic_time` 与 `time.monotonic` 算出的间隔是垃圾。"""
        first = OSD.now_us()
        chosen = OSD._CLOCK
        self.assertIsNotNone(chosen)
        self.assertLessEqual(abs(OSD.now_us() - first), 5_000_000)
        self.assertIs(OSD._CLOCK, chosen, "时基在进程里被换过 = 前后两个纪元在比")


# ── 判据 2：事件路径 —— 事实来了才注入，且事实早于注入 ─────────────────────────
class TestGateWaitsForTheFact(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.cleanup)
        self._env = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": self.h.state_dir})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._clock = mock.patch.object(OSD, "now_us", self.h.clock)
        self._clock.start()
        self.addCleanup(self._clock.stop)

    def _round(self, schedule, keep_window=True, run_ms=600):
        self.h.commit_round(schedule, keep_window=keep_window, run_ms=run_ms)
        self.assertTrue(self.h.settle(), "后台线程没收回来，下面的断言读的是半份现场")
        self.assertEqual(self.h.session.state, "idle", "会话没走完（注入没发生？）")
        return self.h.session.inject_diag

    def test_event_after_the_floor_is_what_lets_the_injection_through(self):
        """焦点交出的事实来得比下限晚 ⇒ 由**事件**放行，注入排在事实之后。"""
        diag = self._round([(250, "unmap"), (251, "destroy")])
        self.assertEqual(diag["gate"], "event")
        self.assertTrue(gate_is_confirmed(diag), f"有事实却被判未确认：{diag}")
        self.assertGreaterEqual(diag["enter_ms"], 250,
                                f"注入早于焦点交出的事实：{diag['enter_ms']}ms")
        self.assertLessEqual(diag["release_ms"], diag["enter_ms"],
                             "时间线的顺序错了：事实必须早于注入")
        self.assertTrue(self.h.calls, "一个上屏命令都没发 = 用户的字被吞了")
        self.assertEqual(diag["visible"], False, "注入时窗口还可见 = Ctrl+V 可能贴进自己")
        self.assertEqual(diag["mapped"], False)
        self.assertIn("inject_diag gate=event", self.h.log_text())

    def test_instant_event_still_honours_the_floor(self):
        """事实与销毁同时到（X11/GTK3 下 `destroy()` 就是**同步**发 unmap 的形状）⇒
        不许把注入提得比现状更早：由下限放行，间隔仍 ≥ `INJECT_FLOOR_MS`。"""
        diag = self._round([(0, "unmap"), (0, "destroy")])
        self.assertEqual(diag["gate"], "floor")
        self.assertTrue(gate_is_confirmed(diag))
        self.assertGreaterEqual(diag["enter_ms"], W.INJECT_FLOOR_MS - 1,
                                f"下限被跳过：{diag['enter_ms']}ms < {W.INJECT_FLOOR_MS}ms")
        self.assertLess(diag["enter_ms"], W.INJECT_FLOOR_MS + 50)

    def test_the_wait_is_expressed_as_two_named_timers_not_a_block(self):
        """等待必须是"主循环计时"而不是"主线程堵着"：挂出去的就是那两个具名常量。"""
        self._round([(250, "unmap")], run_ms=600)
        armed = sorted((us - self.h.base_us) // 1000 for us, _fn in self.h.armed)
        self.assertEqual(armed[:2], [W.INJECT_FLOOR_MS, W.INJECT_WAIT_CAP_MS],
                         f"注入的等待不是那两个具名定时器：{armed}")
        self.assertEqual(self.h.tracker.threads and True, True, "没起过任何后台线程？")
        self.assertTrue(all(c["thread_main"] is False for c in self.h.calls
                            if os.path.basename(c["argv"][0]) in ("wl-copy", "ydotool")),
                        "子进程在主线程上跑 = 又一个「无响应」的形状")

    def test_counterexample_fixed_120ms_timer_goes_red(self):
        """**反例具型**：修前的"窗口销毁后固定等 120ms 就发 Ctrl+V"必须被同一个判据判红。

        同一台假窗口（焦点交出的事实在 250ms 才来）、同一把时钟、同一个判据函数：
        修前那份实现在 120ms 就动了手 ⇒ `gate_is_confirmed` 为 False，
        而交付的实现是 True。抓不住这个反例的判据等于没有判据。
        """
        self.h.server()
        ui = self.h.ui_with([(250, "unmap"), (251, "destroy")])

        # ↓↓↓ 修前的形状（原样内联，不是"看着像"）：只看定时器，从不问时间线 ↓↓↓
        def legacy_fire():
            legacy_diag = W.build_diag(ui, "legacy", self.h.clock.us, ui.base_us)
            legacy_diag["gate"] = "timer"
            self.h.tracker.Thread(target=self.h.session._inject_directly,
                                  args=(self.h.text, "", legacy_diag), daemon=True).start()

        self.h.srv.after_ms(120, legacy_fire)          # == 旧 FOCUS_RETURN_MS = 120
        # ↑↑↑ 修前的形状 ↑↑↑
        self.h.run_ms(150)
        self.assertTrue(self.h.settle(), "反例具型那次的线程没收回")
        legacy = self.h.session.inject_diag
        self.assertFalse(gate_is_confirmed(legacy),
                         "固定 120ms 立即发居然被判成『有证据』 = 判据是摆设")
        self.assertFalse(legacy["released"], "反例那一刻时间线里本该还没有事实")
        self.assertEqual(legacy["visible"], True,
                         "反例具型不成立：那一刻窗口还可见，Ctrl+V 就该贴进自己")
        self.assertLess(legacy["enter_ms"], 250, "反例具型不成立：它比事实来得更早")

        # 交付的实现：同一台假窗口、同一把时钟 ⇒ 必须绿
        self.h.clock.us = self.h.base_us
        shipped = self._round([(250, "unmap"), (251, "destroy")])
        self.assertTrue(gate_is_confirmed(shipped), f"交付实现反而没通过：{shipped}")
        self.assertEqual(shipped["gate"], "event")


    def test_missing_finish_mark_falls_back_to_arm_time_not_zero(self):
        """时间线里取不到 `finish` 打点时，起点退化成"此刻"，**不许拿 0 当起点**。

        拿 0 的话 `span` 就成了一个几十亿的微秒数 ⇒ 下限与上限当场双双"已满足"，
        注入会在 Enter 的那一刻直接发出去（窗口还开着）—— 正是要防的那件事，
        而且它只会以"偶发闪一下"的样子出现，最难查。
        """
        self.h.server()
        ui = self.h.ui_with([(250, "unmap")])
        ui.timeline = [e for e in ui.timeline if e.get("kind") != "finish"]   # 拆掉打点
        self.assertEqual(ui.release_fact()["finish_us"], 0, "先确认假件真的没有打点")
        self.h.session._set_state("review", note="待确认")
        self.h.session._ui = ui
        self.h.session._on_review_outcome(OSD.UiOutcome(action="commit", text=self.h.text))
        self.assertEqual(self.h.calls, [], "连时间轴都还没走过就注入 = 起点算成了 0")
        self.h.run_ms(10)
        self.assertEqual(self.h.calls, [], "下限之内就发了：这次改动的核心条件没生效")
        self.h.run_ms(300)
        self.assertTrue(self.h.settle())
        self.assertTrue(self.h.calls, "300ms 之后仍没注入？")
        diag = self.h.session.inject_diag
        self.assertGreaterEqual(diag["enter_ms"], W.INJECT_FLOOR_MS - 1,
                                f"间隔是从 0 起点算出来的：{diag}")


# ── 判据 3：兜底 —— 等不到事实也要注入，但**必须标未确认** ─────────────────────
def legacy_idle_note(diag: dict) -> str:
    """修前的收尾话术：**一律**报"上屏结束"，哪怕焦点根本没交出去。"""
    return "上屏结束"


class TestCapFallbackIsNotInfinite(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.cleanup)
        self._env = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": self.h.state_dir})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._clock = mock.patch.object(OSD, "now_us", self.h.clock)
        self._clock.start()
        self.addCleanup(self._clock.stop)

    def test_never_released_window_still_injects_within_the_cap(self):
        """伪造"永远收不到 unmap/focus_out"的窗口：上限内必须注入，不许无限等。"""
        # 只有 finish 打点，之后**什么都不来**（Wayland 不保证给我们这种通知）
        self.h.commit_round([], run_ms=W.INJECT_WAIT_CAP_MS)
        self.assertTrue(self.h.settle(), "后台线程没收回")
        self.assertEqual(self.h.session.state, "idle",
                         "等不到焦点交出就无限等 = 用户按了 Enter 什么也没发生")
        self.assertTrue(self.h.calls, "兜底路径上一个上屏命令都没发 = 字被吞了")
        diag = self.h.session.inject_diag
        self.assertEqual(diag["gate"], "cap")
        self.assertTrue(diag["unconfirmed"])
        self.assertLessEqual(diag["enter_ms"], W.INJECT_WAIT_CAP_MS + 50,
                             f"注入超出上限：{diag['enter_ms']}ms")
        self.assertFalse(gate_is_confirmed(diag))

    def test_cap_path_says_unconfirmed_not_finished(self):
        """不许把没证实的写成成功：状态、日志、verdict 三处都要说"未确认"。"""
        self.h.commit_round([], run_ms=W.INJECT_WAIT_CAP_MS + 50)
        self.assertTrue(self.h.settle(), "后台线程没收回")
        self.assertEqual(self.h.session.state, "idle")
        notes = [e.get("note", "") for e in self.h.events if e.get("kind") == "state"]
        verdict = W.verdict_line(self.h.srv.last_inject_diag, self.h.srv.clipboard_diag)
        for blob, what in ((notes, "状态帧的 note"), (verdict, "status 那一行"),
                           (self.h.log_text(), "voice.log")):
            text = "\n".join(blob) if isinstance(blob, list) else blob
            self.assertIn("未观察到焦点交出", text, f"{what} 没把话说清：{text}")
            self.assertNotIn("上屏结束", text, f"{what} 把没证实的写成了成功")
        self.assertIn("结论=未确认", verdict)
        self.assertIn("inject_diag gate=cap unconfirmed=1", self.h.log_text())
        # 反例具型：修前那句一律"上屏结束"的话术，在这条路径上必须红
        diag = self.h.srv.last_inject_diag
        self.assertNotEqual(W.idle_note(diag), legacy_idle_note(diag),
                            "修前的话术与交付的一样 = 本轮改动没有把话说清")

    def test_unconfirmed_still_reports_the_window_facts(self):
        """兜底路径的诊断要能自证"注入可能落进自己"：可见/实化/mapped 都还是真的。"""
        self.h.commit_round([], run_ms=W.INJECT_WAIT_CAP_MS + 50)
        self.assertTrue(self.h.settle(), "后台线程没收回")
        self.assertEqual(self.h.session.state, "idle")
        diag = self.h.srv.last_inject_diag
        self.assertEqual((diag["visible"], diag["realized"], diag["mapped"]), (True, True, True),
                         "假窗口说它还可见/还 mapped，诊断必须照实报（这正是用户现象的形状）")
        line = W.inject_diag_line(diag)
        self.assertTrue(line.startswith("inject_diag "), line)
        for token in ("gate=cap", "enter_ms=", "visible=1", "realized=1", "mapped=1",
                      "unmap=0", "focus_out=0", "tl=", "finish@0"):
            self.assertIn(token, line, f"诊断行缺 {token}：{line}")
        self.assertIn("=", line.split(" ", 1)[1])

    def test_diag_line_flags_are_three_valued(self):
        """`na` 与 `0` 必须是两个不同的字："没观察到"不等于"观察到是假"。"""
        diag = W.build_diag(None, "nowin", 5_000, 1_000)
        line = W.inject_diag_line(diag)
        self.assertIn("gate=nowin", line)
        self.assertIn("visible=na", line)
        self.assertIn("unmap=na", line)
        self.assertIn("enter_ms=4", line)
        self.assertIn("unconfirmed=0", line)


# ── 判据 4：隐私 —— 新加的诊断字段一个字都不许漏 ────────────────────────────────
class TestDiagnosticsCarryNoText(unittest.TestCase):
    """照本仓「念一句银行卡号」那条判据的写法，扩到本轮新加的诊断字段。"""

    def setUp(self):
        self.h = Harness(clipboard={"text/plain;charset=utf-8": CARD.encode("utf-8")})
        self.addCleanup(self.h.cleanup)
        self._env = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": self.h.state_dir})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._clock = mock.patch.object(OSD, "now_us", self.h.clock)
        self._clock.start()
        self.addCleanup(self._clock.stop)

    def test_no_text_in_the_new_diagnostic_surfaces(self):
        self.h.commit_round([(250, "unmap"), (251, "destroy")],
                             run_ms=W.INJECT_WAIT_CAP_MS + 600)   # 连还原那一跳也跑完
        self.assertTrue(self.h.settle(), "后台线程没收回")
        self.h.drain()
        self.assertEqual(self.h.srv.clipboard_diag["branch"], "restored",
                         self.h.srv.clipboard_summary)
        self.assertEqual(self.h.session.state, "idle")
        diag = self.h.srv.last_inject_diag
        verdict = W.verdict_line(diag, self.h.srv.clipboard_diag)
        blobs = {
            "voice.log": self.h.log_text(),
            "inject_diag 行": W.inject_diag_line(diag),
            "inject_done 行": W.inject_done_line(diag),
            "restore_diag 行": W.restore_diag_line("restored", True, 300),
            "status 那一行": verdict,
            "诊断 dict": json.dumps(diag, ensure_ascii=False, default=str),
            "事件帧": json.dumps(self.events_json(), ensure_ascii=False),
            "剪贴板摘要": self.h.srv.clipboard_summary,
            "命令行": " ".join(" ".join(c["argv"]) for c in self.h.calls),
        }
        for what, blob in blobs.items():
            self.assertNotIn(SECRET, blob, f"识别文本漏进 {what}：{blob}")
            self.assertNotIn(CARD, blob, f"剪贴板内容漏进 {what}：{blob}")
        # 沙箱护栏：这些用例写的是临时状态目录，不是用户的真目录
        self.assertTrue(str(P.log_path()).startswith(self.h.state_dir), P.log_path())
        self.assertNotEqual(P.log_path(), Path.home() / ".local" / "state" / "capsplus" / "voice.log")

    def events_json(self):
        out = []
        for frame in self.h.events:
            out.append({k: v for k, v in frame.items() if k != "timeline"})
        return out

    def test_timeline_entries_are_the_only_things_carried(self):
        """诊断 dict 里只许有状态量：时间线条目就是 `{kind, us}`，别的都不该出现。"""
        self.h.commit_round([(250, "unmap")], run_ms=300)
        self.assertTrue(self.h.settle(), "后台线程没收回")
        self.assertEqual(self.h.session.state, "idle")
        diag = self.h.session.inject_diag
        allowed = {"gate", "unconfirmed", "enter_ms", "release_ms", "released", "release_kind",
                   "at_us", "finish_us", "inject_us", "visible", "realized", "mapped", "mapped_by_timeline",
                   "unmap_seen", "focus_out_seen", "destroy_seen", "entries", "timeline",
                   "backend", "ok", "code", "steps",
                   # 2026-10-19 补：写剪贴板那步（守护步"父进程退出"）的毫秒数。它是**状态量**
                   # （一个整数耗时），不是内容；`inject_done … copy_ms=` 靠它自证
                   # "父退是毫秒级、不是被 5 秒超时判死"。登记在这里等于承认它是诊断面的一部分。
                   "copy_ms"}
        self.assertEqual(set(diag) - allowed, set(), f"诊断里混进了没登记过的字段：{set(diag)}")
        self.assertNotIn("text", diag, "诊断 dict 里出现了 text 字段 = 文本可能被带出去")


# ── 顺带：`status` 那一行与还原分支的可解析形状 ────────────────────────────────
class TestStatusLineAndRestoreBranch(unittest.TestCase):
    def test_verdict_is_one_short_line_with_conclusion_first(self):
        diag = W.build_diag(None, "nowin", 1_200_000, 1_000_000)
        diag.update(backend="clipboard", ok=True, code="")
        line = W.verdict_line(diag, {"branch": "restored", "still_ours": True})
        self.assertEqual(len(line.splitlines()), 1, f"verdict 不是一行：{line!r}")
        self.assertTrue(line.startswith("结论="), line)
        self.assertLess(len(line), 220, f"一行太长，用户贴起来还是难受：{len(line)}")
        for token in ("gate=nowin", "Enter→注入=200ms", "后端=clipboard", "ok=1",
                      "还原=restored", "回读=还是我们写的"):
            self.assertIn(token, line, f"verdict 缺 {token}：{line}")

    def test_verdict_for_the_unconfirmed_case_says_so_first(self):
        clock = FakeClock()
        ui = FakeUi(clock, clock.us, [], keep_window=True)
        diag = W.build_diag(ui, "cap", clock.us + 400_000, clock.us)
        line = W.verdict_line(diag, {"branch": "not_ours", "still_ours": False})
        self.assertTrue(line.startswith("结论=未确认"), line)
        self.assertIn("未观察到焦点交出", line)
        self.assertIn("还原=not_ours", line)
        self.assertIn("回读=已被别人改", line)

    def test_timeline_token_is_milliseconds_relative_to_enter(self):
        """`tl=` 的单位必须是**毫秒**、以 Enter 为零点：负数=Enter 之前，正数=之后。

        反例具型 = 拿微秒直接减（真机上一串 `unmap@250000` 没人能对得上"闪一下"的时刻）。
        """
        timeline = [{"kind": "map", "us": 992_000}, {"kind": "focus_in", "us": 997_500},
                    {"kind": "finish", "us": 1_000_000}, {"kind": "unmap", "us": 1_250_000}]
        self.assertEqual(W.timeline_token(timeline),
                         "map@-8,focus_in@-2,finish@0,unmap@250")   # -2.5ms 走四舍六入
        self.assertEqual(W.timeline_token([]), "empty")
        diag = {"gate": "floor", "enter_ms": 120, "timeline": timeline}
        self.assertIn("tl=map@-8,focus_in@-2,finish@0,unmap@250", W.inject_diag_line(diag))

    def test_restore_branch_tokens_cover_the_documented_vocabulary(self):
        """`restore_diag branch=` 的取值必须落在文档写的那套词表里（不许发明新词）。"""
        for branch in ("armed", "merged", "skip", "restored", "restored_unverified",
                       "cleared", "not_ours", "verify_mismatch", "check_failed",
                       "step_failed", "no_backup"):
            line = W.restore_diag_line(branch, None if branch == "skip" else True, 300)
            self.assertTrue(line.startswith("restore_diag "), line)
            self.assertIn(f"branch={branch}", line)
            self.assertNotIn(CARD, line)
        self.assertIn("branch=skip", W.restore_diag_line("skip"))
        self.assertIn("still_ours=na", W.restore_diag_line("skip"))

    def test_worker_records_which_branch_the_restore_landed_in(self):
        """还原作业最终落进哪个分支要进 `clipboard_diag`（status 与日志都读它）。"""
        h = Harness(clipboard={"text/plain;charset=utf-8": b"old"})
        env = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": h.state_dir})
        env.start()
        clock = mock.patch.object(OSD, "now_us", h.clock)
        clock.start()
        try:
            h.server()
            result = INJ.inject("新的一段", backend="clipboard", probe=PROBE, runner=h.run)
            h.srv.arm_clipboard_restore(result.restore_job, result.clipboard_note)
            self.assertEqual(h.srv.clipboard_diag["branch"], "armed")
            self.assertIn("restore_diag branch=armed", h.log_text())
            h.srv.run_clipboard_restore()
            self.assertTrue(wait_for(lambda: h.srv.clipboard_diag["branch"] != "armed"))
            self.assertEqual(h.srv.clipboard_diag["branch"], "restored")
            self.assertIs(h.srv.clipboard_diag["still_ours"], True)
            self.assertIn("restore_diag branch=restored still_ours=1", h.log_text())

            # 开关关掉 / 没有作业 ⇒ branch=skip，这条分支也必须落进日志
            h2 = Harness()
            env2 = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": h2.state_dir,
                                                INJ.RESTORE_ENV: "0"})
            env2.start()
            try:
                h2.server()
                result2 = INJ.inject("又一段", backend="clipboard", probe=PROBE, runner=h2.run)
                self.assertIsNone(result2.restore_job)
                h2.srv.arm_clipboard_restore(result2.restore_job, result2.clipboard_note)
                self.assertEqual(h2.srv.clipboard_diag["branch"], "skip")
                self.assertIn("restore_diag branch=skip", h2.log_text())
            finally:
                env2.stop()
                h2.cleanup()
        finally:
            clock.stop()
            env.stop()
            h.cleanup()


# ── 真窗口那三个状态量必须是**真布尔**（打字错误会变成 na，诊断就白装）───────────
@unittest.skipUnless(TP.RUN_GUI and not TP.real_desktop_risk(),
                     "界面判据只在 CAPSPLUS_GUI=1 + 虚拟显示下跑；口径照 TestOsdRealWindow")
class TestProbeAgainstARealWindow(unittest.TestCase):
    """`probe()` 里那三个 getter 的名字本轮真写错过一次（`is_realized()` 在 GTK3 里不存在），
    被 `except` 一口吞掉 ⇒ visible/realized/mapped 一起变 `na` ⇒ 诊断行全空但一片绿。
    这条只在有真窗口的时候才量得出来，所以它必须真的开窗（Xvfb）。
    """

    @classmethod
    def setUpClass(cls):
        if TP.gi is None:
            raise AssertionError(f"CAPSPLUS_GUI=1 但 import gi 失败：{TP.GI_ERROR}")
        if not TP.display_works():
            raise AssertionError("CAPSPLUS_GUI=1 但连不上显示服务器（没在 xvfb-run 下跑？）")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": self.tmp.name,
                                                 "CAPSPLUS_VOICE_UI": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)

    def pump(self, seconds: float = 0.25) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while TP.Gtk.events_pending():
                TP.Gtk.main_iteration_do(False)
            time.sleep(0.01)

    def test_window_up_reports_three_true_booleans(self):
        ui = OSD.VoiceUi()
        ui.post_text("量状态量用的假句子")
        self.assertIsNone(ui.open_review())
        self.pump()
        probe = ui.probe()
        try:
            self.assertIs(probe["visible"], True, f"窗口开着却说 visible={probe['visible']}")
            self.assertIs(probe["realized"], True, f"窗口开着却说 realized={probe['realized']}")
            self.assertIs(probe["mapped"], True, f"窗口开着却说 mapped={probe['mapped']}")
            kinds = [e["kind"] for e in probe["timeline"]]
            self.assertIn("map", kinds, f"窗口真开了却没记到 map：{kinds}")
            self.assertFalse(probe["destroy_seen"], "窗口还开着，不许提前记下 destroy")
            # 还没按 Enter ⇒ 时间线里没有 `finish` 打点，"焦点交出"这件事**还不该成立**
            self.assertFalse(probe["unmap_seen"], "窗口没撤，unmap 不该被记成发生过")
        finally:
            if ui._window is not None:
                ui._window.destroy()
            self.pump(0.05)

    def test_after_destroy_all_three_are_false_not_na(self):
        ui = OSD.VoiceUi()
        ui.post_text("量状态量用的假句子")
        self.assertIsNone(ui.open_review())
        self.pump()
        ui._finish("commit")                       # Enter 的动作：commit → 销毁窗口
        self.pump(0.05)
        probe = ui.probe()
        self.assertIs(probe["visible"], False, probe)
        self.assertIs(probe["realized"], False, probe)
        self.assertIs(probe["mapped"], False, probe)
        self.assertTrue(probe["released"], f"销毁之后时间线里竟没有焦点交出的事实：{probe}")
        line = W.inject_diag_line(W.build_diag(ui, "floor", OSD.now_us(),
                                               ui.release_fact()["finish_us"]))
        self.assertIn("visible=0 realized=0 mapped=0", line, line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
