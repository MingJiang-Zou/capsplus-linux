# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""语音 worker 主循环模型的三层判据（2026-10-17 真机事故：GNOME 弹「"voice_worker.py" 无响应」）。

病是这么病的：`_review_with_ui()` 每说一句话就**新开一个线程，在那个线程里构造 `voice_osd.VoiceUi()`
并 `ui.show()` 跑它自己的 `GLib.MainLoop`**，主线程则 `join()` + `time.sleep(0.3)` 轮询。
mutter 对客户端的 ping 落在进程主线程 —— 主线程在睡觉，应答就不稳，桌面弹出"等待/强制退出"。
本仓 `qbar_app.py::_pump` 早就用 A/B 实测换过这条结论（主循环不转什么都发不出去），这里再犯。

三层判据，每层都带**能红**的反例：

  * **实质判据 B（静态级，本文件 `TestStaticMainloopRules`，CI 主力，headless 也拦）**：
    AST 扫 `capsplus/voice_worker.py` ——
      `UI-IN-THREAD`：`threading.Thread(target=...)` 的目标函数里构造 voice_osd 对象 /
        调用 `show()` / 引用 `voice_osd`，判失败；
      `SLEEP-OUTSIDE-EXEMPT`：全文件 `time.sleep(` 只许出现在 `wait_for_worker`
        （那是客户端一次性进程的轮询，不在 worker 主循环里）；
      `BLOCK-ON-MAIN-PATH`：主线程路径的函数（`MAIN_PATH_FUNCS`）里出现
        sleep / wait / join / wait_done 这类阻塞调用，判失败。
    反例具型 = **修前的错误代码原样内联**（`BROKEN_SNIPPET`），喂同一个检查器必须变红。
  * **实质判据 A（行为级，本文件 `TestReviewRunsOnMainThread`，CAPSPLUS_GUI=1 + Xvfb 才跑，
    无显示显式 skip）**：review 建窗点断言 `threading.current_thread() is threading.main_thread()`
    为真；且**注入前时间线里已经有 unmap/focus_out**（`voice_osd.RELEASE_KINDS`，2026-10-17
    改口径：触发条件是"焦点交出的事实"，`INJECT_FLOOR_MS` 只是下限、`INJECT_WAIT_CAP_MS`
    只是上限兜底）、`Esc` 路径不注入。
  * **进程级（脚本，交付回报里贴修前/修后对比）**：`sh tools/verify-voice-mainloop.sh` ——
    headless 起 `serve`，断言主线程 wchan 不是 `hrtimer_nanosleep`。修前它必须能复现红。

无头也能验的门岗行为在 `TestReviewDegradation`：`_open_review` 不在主线程/建窗失败/没显示时
都退回直接上屏，文本绝不静默丢掉。
"""

import ast
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import test_voice_portability as TP                 # noqa: E402  复用同一套 GUI 闸门（别造第二份）

WORKER_PATH = REPO / "capsplus" / "voice_worker.py"

# ── 判据 B：静态 AST 检查器（对字符串源码工作，反例才能内联进来）────────────────
MAIN_PATH_FUNCS = frozenset({
    "serve_forever", "_watchdog_tick", "_run_fallback_loop", "_open_review",
    "_on_review_outcome", "_review_failed", "_arm_inject", "_inject_with_diagnosis",
    "_fire_clipboard_restore",
    "post", "after_ms", "request_quit", "main",
})
SLEEP_EXEMPT = frozenset({"wait_for_worker"})
BLOCKING_NAMES = frozenset({"sleep", "wait", "join", "wait_done"})


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _enclosing_map(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def static_violations(source: str) -> list[str]:
    """返回 `capsplus/voice_worker.py` 形状源码违反的主循环模型判据（空 = 合规）。"""
    tree = ast.parse(source)
    parents = _enclosing_map(tree)
    funcs: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            funcs.setdefault(node.name, node)

    bad: list[str] = []

    # 1) UI-IN-THREAD：`Thread(target=fn)` 的 fn 里碰 voice_osd / VoiceUi / show()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node) == "Thread"):
            continue
        target = next((kw.value for kw in node.keywords if kw.arg == "target"), None)
        if target is None:
            continue
        body: list = []
        fname = ""
        if isinstance(target, ast.Lambda):
            body = [target]
        elif isinstance(target, (ast.Name, ast.Attribute)):
            fname = target.id if isinstance(target, ast.Name) else target.attr
            found = funcs.get(fname)
            if found is not None:
                body = [found]
        for sub in body:
            for inner in ast.walk(sub):
                why = ""
                if isinstance(inner, ast.Name) and inner.id == "voice_osd":
                    why = f"目标 `{fname or 'lambda'}` 引用 voice_osd"
                elif isinstance(inner, ast.Call):
                    cname = _call_name(inner)
                    if cname == "VoiceUi":
                        why = f"目标 `{fname or 'lambda'}` 构造 VoiceUi"
                    elif cname == "show":
                        why = f"目标 `{fname or 'lambda'}` 调用 show()"
                if why:
                    bad.append(f"UI-IN-THREAD:{node.lineno}: {why}"
                               " —— GTK 只能住主线程（真机事故 2026-10-17）")

    # 2) SLEEP-OUTSIDE-EXEMPT：除 wait_for_worker（客户端进程）外不许 time.sleep
    def enclosing_fn(node):
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.FunctionDef):
                return cur
            cur = parents.get(cur)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "sleep":
            fn = enclosing_fn(node)
            fname = fn.name if fn else "<模块顶层>"
            if fname in SLEEP_EXEMPT:
                continue
            bad.append(f"SLEEP-OUTSIDE-EXEMPT:{node.lineno}: `{fname}()` 里出现 `sleep(...)`"
                       " —— 等待一律走 GLib 定时器/事件；主线程睡觉就是「无响应」弹窗")

    # 3) BLOCK-ON-MAIN-PATH：主线程路径函数里的阻塞调用
    for fname in sorted(MAIN_PATH_FUNCS):
        fn = funcs.get(fname)
        if fn is None:
            continue
        for inner in ast.walk(fn):
            if isinstance(inner, ast.Call):
                cname = _call_name(inner)
                if cname in BLOCKING_NAMES:
                    bad.append(f"BLOCK-ON-MAIN-PATH:{inner.lineno}: 主线程函数 `{fname}()`"
                               f" 调用阻塞的 `{cname}(...)`")
    return bad


# ── 反例具型：修前（2026-10-17 之前）的错误代码，摘其形状、语义原样 ──────────────
# 这就是用户真机上弹「无响应」的那份实现；喂给 static_violations 必须变红，
# 否则上面的检查器就是永远绿的空门。
BROKEN_SNIPPET = '''
class Session:
    def _review_with_ui(self):
        """弹窗跑在本进程唯一的 GTK 线程里；线程退出后主循环早已把窗口销毁。"""
        from capsplus import voice_osd

        holder = {}
        ready = threading.Event()

        def worker():
            ui = voice_osd.VoiceUi()
            holder["ui"] = ui
            ready.set()
            holder["outcome"] = ui.show()

        self._set_state("review", note="待确认")
        self._ui_thread = threading.Thread(target=worker, daemon=True)
        self._ui_thread.start()
        self._ui_thread.join()
        outcome = holder.get("outcome")
        if outcome.action == "commit":
            self._inject_directly(outcome.text, "", delay_s=FOCUS_RETURN_MS / 1000.0)

    def _inject_directly(self, text, note, delay_s=0.0):
        if delay_s:
            time.sleep(delay_s)


class Server:
    def serve_forever(self):
        while not self._quit.is_set():
            session = self.session
            if session is None or session.done():
                time.sleep(0.3)                    # 空闲：睡一跳，别把 CPU 吃满
                continue
            if not session.wait_done(timeout=1.0):
                session.abandon("超时")

    def wait_for_worker(self):
        while time.monotonic() < 0:
            time.sleep(0.08)
'''


class TestStaticMainloopRules(unittest.TestCase):
    """判据 B：静态级，headless 也拦，CI 主力。"""

    def test_current_worker_is_clean(self):
        src = WORKER_PATH.read_text(encoding="utf-8")
        self.assertEqual(static_violations(src), [],
                         "capsplus/voice_worker.py 违反了主循环模型（详见各条前缀）")

    def test_counterexample_broken_code_goes_red(self):
        """反例具型：修前的真·错误代码必须被同一个检查器抓住（三种形状都要红）。"""
        rules = {v.split(":", 1)[0] for v in static_violations(BROKEN_SNIPPET)}
        self.assertIn("UI-IN-THREAD", rules,
                      "「线程目标里建 VoiceUi/调 show」的形状没抓到 = 判据是摆设")
        self.assertIn("SLEEP-OUTSIDE-EXEMPT", rules,
                      "「主线程/注入前 time.sleep」的形状没抓到 = 判据是摆设")
        self.assertIn("BLOCK-ON-MAIN-PATH", rules,
                      "「serve_forever 里 wait_done/sleep」的形状没抓到 = 判据是摆设")

    def test_counterexample_hits_the_named_functions(self):
        hits = static_violations(BROKEN_SNIPPET)
        joined = "\n".join(hits)
        self.assertIn("worker", joined)                      # 那个 per-utterance 的 UI 线程
        self.assertIn("serve_forever", joined)               # 主线程的 sleep+wait_done 轮询
        self.assertIn("_inject_directly", joined)            # 注入前的 time.sleep(delay_s)

    def test_exempt_and_good_shapes_pass(self):
        """检查器别长成"见 Thread 就杀"：后台线程干正事的写法必须绿。"""
        good = '''
class Server:
    def serve_forever(self):
        threading.Thread(target=self._accept_loop, args=(s,), daemon=True).start()
        self._loop.run()

    def _accept_loop(self, server):
        conn, _ = server.accept()

    def _decode_and_review(self, rec):
        ASR.recognize(rec.pcm)

    def wait_for_worker(self):
        time.sleep(0.08)


def wait_for_worker():
    time.sleep(0.08)
'''
        self.assertEqual(static_violations(good), [])

    def test_checker_scans_lambdas_too(self):
        """target 写成 lambda 直接摸 show() 也要红（反例的第二种具型）。"""
        src = "threading.Thread(target=lambda: voice_osd.VoiceUi().show(), daemon=True)"
        rules = {v.split(":", 1)[0] for v in static_violations(src)}
        self.assertIn("UI-IN-THREAD", rules)

    def test_clipboard_restore_callback_must_not_block(self):
        """反例具型（2026-10-17 剪贴板还原这一轮加）：主循环到点的那个回调里一旦有阻塞调用，
        `BLOCK-ON-MAIN-PATH` 必须红 —— 它是 `after_ms` 直接派发的，睡过去就是又一轮「无响应」。"""
        src = '''
class Server:
    def _fire_clipboard_restore(self):
        self._idle_evt.wait(1.0)          # 等子进程读写完（错误写法）
        threading.Thread(target=self.run_clipboard_restore, daemon=True).start()
'''
        hits = static_violations(src)
        self.assertTrue(any(v.startswith("BLOCK-ON-MAIN-PATH") for v in hits), hits)
        self.assertIn("_fire_clipboard_restore", "\n".join(hits))

    def test_the_shipped_callback_only_hands_the_work_over(self):
        """正例具型：现在交付的 `_fire_clipboard_restore` 只起线程，必须在绿的那一侧。"""
        src = '''
class Server:
    def _fire_clipboard_restore(self):
        threading.Thread(target=self.run_clipboard_restore, daemon=True).start()
'''
        self.assertEqual(static_violations(src), [])

    def test_the_injection_gate_callbacks_must_not_block(self):
        """反例具型（2026-10-17 焦点门这一轮加）：**等事件**这件事只许由主循环做。

        把注入改成"在到点回调里 `wait()` 直到窗口交出焦点"，功能上像是更严，实际是拿
        主线程去赌合成器理不理你 —— 就是那个「voice_worker.py 无响应」弹窗的形状，
        `BLOCK-ON-MAIN-PATH` 必须红。下限/上限定时器与 `_inject_with_diagnosis` 同理。
        """
        src = '''
class Session:
    def _arm_inject(self, text, note, ui):
        def try_fire():
            ui.released.wait(1.0)          # 等"焦点交出"等到主循环不转（错误写法）
            self._inject_with_diagnosis(text, note, ui)

    def _inject_with_diagnosis(self, text, note, ui, gate):
        self.server.done.wait(0.5)         # 等子进程注入完（同样堵死主线程）
        threading.Thread(target=self._inject_directly, args=(text, note), daemon=True).start()
'''
        hits = static_violations(src)
        joined = "\n".join(hits)
        self.assertTrue(any(v.startswith("BLOCK-ON-MAIN-PATH") for v in hits), hits)
        self.assertIn("_arm_inject", joined)
        self.assertIn("_inject_with_diagnosis", joined)

    def test_the_named_main_path_functions_actually_exist(self):
        """名册不许变成死名：`MAIN_PATH_FUNCS` 里每一个名字都必须在交付的 worker 里真存在。

        为什么单独钉一条：门 #3 是 `funcs.get(fname)` 取不到就 `continue` —— 一旦有人把
        `_schedule_inject` 改名而忘了同步这里，检查器会**悄无声息地**不再管那条路径
        （本仓 2026-10-17 已经为"没人跑过的分支把判据照成假绿"栽过一次）。
        """
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        have = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        missing = sorted(MAIN_PATH_FUNCS - have)
        self.assertEqual(missing, [], f"名册里有名字在 worker 里已经不存在：{missing}")


# ── 无头行为：主线程门岗与三条降级（所有机器都跑，不开窗）───────────────────────
class TestReviewDegradation(unittest.TestCase):
    """`_open_review` 的三条降级路径：不在主线程 / 建窗抛 / 没显示 —— 都要**直接上屏**。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": self._tmp.name})
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _server(self, calls: list):
        from capsplus import voice_inject as INJ
        from capsplus import voice_worker as W

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return type("R", (), {"returncode": 0, "stderr": b""})()

        probe = INJ.Probe(wl_copy="/bin/true", wl_copy_primary=True, ydotool="/bin/true",
                          syntax=("keycode", "click-bitmask"),
                          keycodes={"KEY_LEFTCTRL": 29, "KEY_V": 47}, ydotool_daemon=True)
        srv = W.Server(run_command=fake_run, review_ui=True)
        srv.backend = "clipboard"
        srv.probe = lambda: probe
        srv._events = []
        srv.broadcast = srv._events.append
        return W, srv

    def _session_with_text(self, srv):
        session = srv._ensure_session()
        session.text = "九点开会"
        return session

    def _finish_and_assert_injected(self, session, srv, calls, expect_why: str):
        from capsplus import voice_proto as P

        self.assertTrue(session.wait_done(10), "降级后会话没走完")
        self.assertEqual(session.state, "idle")
        self.assertTrue(calls, f"{expect_why}：一个上屏命令都没发 = 用户的字被吞了")
        notes = [e.get("note", "") for e in srv._events if e.get("kind") == "text"]
        self.assertTrue(any(f"弹窗没走成（{expect_why}）" in n for n in notes),
                        f"降级说明缺 {expect_why}：{notes}")
        self.assertNotIn("九点开会", P.log_path().read_text(encoding="utf-8"))

    def test_off_main_thread_refuses_gtk_and_injects(self):
        W, srv = self._server([])
        calls = []
        srv.run_command = lambda argv, **kw: (calls.append(list(argv)) or
                                              type("R", (), {"returncode": 0, "stderr": b""})())
        session = self._session_with_text(srv)
        worker = threading.Thread(target=session._open_review)
        worker.start()
        worker.join(5)
        self._finish_and_assert_injected(session, srv, calls, "不在主线程")

    def test_build_failure_on_main_thread_injects(self):
        import capsplus.voice_osd as OSD

        W, srv = self._server([])
        calls = []
        srv.run_command = lambda argv, **kw: (calls.append(list(argv)) or
                                              type("R", (), {"returncode": 0, "stderr": b""})())
        session = self._session_with_text(srv)
        with mock.patch.object(OSD.VoiceUi, "available", return_value=True), \
             mock.patch.object(OSD.VoiceUi, "build",
                               side_effect=RuntimeError("假建窗失败")):
            session._open_review()                  # 测试主线程 = 进程主线程
        self._finish_and_assert_injected(session, srv, calls, "failed")

    def test_unavailable_on_main_thread_injects(self):
        import capsplus.voice_osd as OSD

        W, srv = self._server([])
        calls = []
        srv.run_command = lambda argv, **kw: (calls.append(list(argv)) or
                                              type("R", (), {"returncode": 0, "stderr": b""})())
        session = self._session_with_text(srv)
        with mock.patch.object(OSD.VoiceUi, "available", return_value=False):
            session._open_review()
        self._finish_and_assert_injected(session, srv, calls, "unavailable")

    def test_open_review_broadcasts_review_before_touching_gtk(self):
        """review 状态帧要先出去（`should_abandon` 的「review 永不超时」靠它）。"""
        import capsplus.voice_osd as OSD

        W, srv = self._server([])
        calls = []
        srv.run_command = lambda argv, **kw: (calls.append(list(argv)) or
                                              type("R", (), {"returncode": 0, "stderr": b""})())
        session = self._session_with_text(srv)
        with mock.patch.object(OSD.VoiceUi, "available", return_value=False):
            session._open_review()
        self.assertTrue(session.wait_done(10), "降级没走完")
        states = [e.get("state") for e in srv._events if e.get("kind") == "state"]
        self.assertIn("review", states)
        self.assertEqual(states[-1], "idle")
        self.assertLess(states.index("review"), len(states) - 1,
                        "review 帧必须**先**出去，降级帧在后（看门狗的「review 永不超时」靠它）")
        self.assertTrue(calls, "review 出去了却没上屏 = 文本被丢掉")


# ── 判据 A：行为级，真开窗（CAPSPLUS_GUI=1 + Xvfb；无显示显式 skip）───────────────
@unittest.skipUnless(TP.RUN_GUI and not TP.real_desktop_risk(),
                     "界面判据只在 CAPSPLUS_GUI=1 + 虚拟显示下跑；口径照 TestOsdRealWindow")
class TestReviewRunsOnMainThread(unittest.TestCase):
    """实质判据 A：整条「识别完 → 回主线程建窗 → Enter → 销毁 → **等到焦点交出的事实** → 注入」
    链上，建窗必须发生在**进程主线程**；注入必须发生在窗口 `unmap`/`focus_out` **之后**，
    且不早于下限、不晚于上限兜底。"""

    @classmethod
    def setUpClass(cls):
        if TP.gi is None:
            raise AssertionError(f"CAPSPLUS_GUI=1 但 import gi 失败：{TP.GI_ERROR}")
        if not TP.display_works():
            raise AssertionError("CAPSPLUS_GUI=1 但连不上显示服务器（没在 xvfb-run 下跑？）")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(os.environ, {
            "CAPSPLUS_STATE_DIR": self._tmp.name,
            "CAPSPLUS_VOICE_UI": "1",
        })
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _round(self, keyval, shift=False):
        import types as _t

        from gi.repository import GLib
        from capsplus import voice_inject as INJ
        from capsplus import voice_osd as OSD
        from capsplus import voice_worker as W
        from capsplus.voice_recorder import Recording

        calls: list = []

        def fake_run(argv, **kw):
            calls.append({"argv": list(argv), "t": time.monotonic(),
                          "ui": session._ui if session else None,
                          "diag": dict(session.inject_diag)})
            return type("R", (), {"returncode": 0, "stderr": b""})()

        probe = INJ.Probe(wl_copy="/bin/true", wl_copy_primary=True, ydotool="/bin/true",
                          syntax=("keycode", "click-bitmask"),
                          keycodes={"KEY_LEFTCTRL": 29, "KEY_V": 47}, ydotool_daemon=True)
        srv = W.Server(run_command=fake_run, review_ui=True)
        srv.backend = "clipboard"
        srv.probe = lambda: probe
        events: list = []
        session = srv._ensure_session()

        def broadcast(frame):
            events.append(frame)
            if frame.get("state") == "idle":
                srv.request_quit()              # 会话走完就收主循环，用例不许挂死
        srv.broadcast = broadcast

        build_threads: list = []
        orig_open = OSD.VoiceUi.open_review

        def spy_open(self_ui):
            build_threads.append(threading.current_thread() is threading.main_thread())
            return orig_open(self_ui)

        fake_result = _t.SimpleNamespace(ok=True, text="九点开会", code="", message="",
                                         hint="", describe=lambda: "假识别")
        rec = Recording(pcm=b"\x01\x00" * 1600,
                        vad=_t.SimpleNamespace(summary=lambda: "假 VAD", has_speech=True,
                                               drop_reason=""))
        key_times: list = []
        srv._loop = GLib.MainLoop()
        with mock.patch.object(W.Session, "_ui_available", return_value=True), \
             mock.patch.object(W.ASR, "recognize", return_value=fake_result), \
             mock.patch.object(OSD.VoiceUi, "open_review", spy_open):
            def kick():                          # 后台线程：完整走「识别完 → post 回主线程」
                session._decode_and_review(rec)

            def press():
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and session._ui is None:
                    time.sleep(0.02)             # 测试辅助线程，不在被测主循环里
                if session._ui is None:
                    return
                ui = session._ui

                def do_key():
                    ev = TP.Gdk.Event.new(TP.Gdk.EventType.KEY_PRESS)
                    ev.keyval = keyval
                    ev.state = (TP.Gdk.ModifierType.SHIFT_MASK if shift
                                else TP.Gdk.ModifierType(0))
                    ui._on_key(None, ev)
                    key_times.append(time.monotonic())
                    return False
                GLib.idle_add(do_key)

            t1 = threading.Timer(0.05, kick)
            t1.daemon = True
            t1.start()
            t2 = threading.Timer(0.30, press)
            t2.daemon = True
            t2.start()
            GLib.timeout_add_seconds(20, srv.request_quit)   # 兜底：绝不把测试挂住
            srv._loop.run()                                   # **主线程**跑那唯一的主循环
        return session, calls, events, build_threads, key_times

    def test_commit_path_builds_on_main_thread_and_injects_after_destroy(self):
        """判据 A 本体：Enter → commit → 窗口销毁**并且时间线里出现焦点交出的事实** → 注入。

        2026-10-17 改口径：原来这条只断言"隔了 ≥100ms"，那只是"过了那么久"，不说明焦点
        给了谁 —— 用户真机看到的"贴上又被删"就是从这种假绿里漏过去的。现在断言的是**顺序**：
        时间线里 `unmap`/`focus_out` 至少一条，且它发生在注入之前。
        """
        from capsplus import voice_worker as W

        session, calls, events, build_threads, key_times = self._round(TP.Gdk.KEY_Return)
        self.assertEqual(build_threads, [True],
                         f"建窗点不在进程主线程（{build_threads}）—— 真机「无响应」弹窗的形状")
        states = [e.get("state") for e in events if e.get("kind") == "state"]
        self.assertIn("review", states)
        self.assertEqual(states[-1], "idle")
        self.assertTrue(calls, "commit 之后没发上屏命令")
        for call in calls:
            self.assertIsNone(call["ui"],
                              "注入时 _ui 还在 = 窗口没销毁就动手，Ctrl+V 会贴进自家弹窗")
        self.assertTrue(key_times)
        gap = calls[0]["t"] - key_times[0]
        self.assertGreaterEqual(gap, W.INJECT_FLOOR_MS / 1000.0 - 0.02,
                                f"注入的下限被跳过：{gap:.3f}s < INJECT_FLOOR_MS")
        self.assertLess(gap, (W.INJECT_WAIT_CAP_MS + 400) / 1000.0,
                        f"注入拖到上限之外去了：{gap:.3f}s")
        diag = calls[0]["diag"]
        self.assertIn(diag.get("gate"), ("event", "floor"),
                      f"真窗口下本该由焦点交出的事实放行，结果 gate={diag.get('gate')}")
        self.assertTrue(diag.get("released"), "注入前没观察到任何 unmap/focus_out")
        self.assertTrue(diag.get("unmap_seen") or diag.get("focus_out_seen"),
                        f"时间线里没有焦点交出的条目：{diag.get('timeline')}")
        self.assertFalse(diag.get("unconfirmed"), "真窗口路径不该被判成未确认")
        self.assertGreaterEqual(diag.get("release_ms", -1), 0,
                                "焦点交出的时刻早于 Enter？时间线的口径坏了")
        self.assertLessEqual(diag.get("release_ms", 1e9), diag.get("enter_ms", -1),
                             "注入排在焦点交出之前 = 本轮改动要防的那件事")
        kinds = [e.get("kind") for e in diag.get("timeline") or []]
        self.assertIn("map", kinds, "时间线里连 map 都没有 = 生命周期信号根本没接上")
        self.assertIn("destroy", kinds, "时间线里没有 destroy：销毁这件事没被记下来")
        self.assertIn("finish", kinds, "Enter 的打点没记 = enter_ms 是编出来的")
        self.assertEqual(session.state, "idle")
        from capsplus import voice_proto as P
        log = P.log_path().read_text(encoding="utf-8")
        self.assertIn("inject_diag ", log, "注入那一刻的状态量必须落进日志")
        self.assertNotIn("九点开会", log, "隐私语义不许回退：识别文本不进日志")

    def test_cancel_path_never_injects_and_still_builds_on_main_thread(self):
        """Esc：同样是主线程建窗，但一个字都不注入。"""
        _session, calls, events, build_threads, _keys = self._round(TP.Gdk.KEY_Escape)
        self.assertEqual(build_threads, [True])
        self.assertEqual(calls, [], "Esc 丢弃还注入 = 用户不要的话被贴上屏")
        notes = " ".join(str(e.get("note", "")) for e in events if e.get("kind") == "state")
        self.assertIn("已丢弃", notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
