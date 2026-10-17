#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""capsplus-voice 的心脏：socket 单实例服务端 + 「录音 → 识别 → 确认 → 上屏」状态机。

单实例语义照 `qbar/qbar_app.py`：**socket 文件在但连不上 = 陈旧，unlink 后自己起**；
第二次按键不再复制一个进程，而是往已有的 worker 发一条命令。这里比 Qbar 多一层，
因为要回话，所以线格式走 `capsplus/voice_proto.py`（换行分隔 JSON）。

一条 utterance 的完整链路（`state` 事件就按这几步广播）：

    Caps+M → recording ─(VAD 尾静音 700ms / 录满 20s / 用户再按一次)→ decoding
           → (识别 ok) review ─(弹窗里 Enter)→ 上屏 → idle
                     ↘ (识别失败 / 没说话)  提示 → idle
           → (UI 不可用) 跳过 review，**直接**按上屏后端注入 → idle

线程模型（2026-10-17 定稿，别改回去）：

  * **主线程 = 进程里唯一的 GLib/GTK 使用者**。`serve_forever()` 一启动就在主线程上
    跑**唯一的一个**主循环（`GLib.MainLoop().run()`；没有 gi 的机器退到 `select` 事件
    等待循环），进程活多久它转多久；
  * `accept` 线程与每条连接的线程只搬 socket 数据、不碰 GTK；
  * 录音在 `voice_recorder` 的线程里；识别 HTTP 在 `_decode_and_review` 的后台线程里；
    注入子进程也在后台线程 —— 完成后一律 `GLib.idle_add`（这里的出口：`Server.post`）
    回主线程；
  * **review 弹窗在主线程建窗 + 回调驱动**：后台识别完 `post(_open_review)` 回主线程建窗，
    commit/cancel 由 `voice_osd.VoiceUi.on_finish` 回调推进状态机；注入一定发生在
    窗口**销毁之后** ≥ `FOCUS_RETURN_MS`（`GLib.timeout_add` 等待，不是主线程 sleep）——
    否则 Ctrl+V 会贴进我们自己的窗口。

为什么定成这个样子（真机事故，2026-10-17）：旧写法每个 utterance 新开一个 GTK 线程 +
`GLib.MainLoop`，主线程 `join()` + `time.sleep` 轮询；mutter 对客户端的 ping 落在主线程
上没人应答，明江的 GNOME 直接弹「"voice_worker.py" 无响应（等待/强制退出）」。本仓早就
用血换过这条结论（`qbar_app.py::_pump` 的 A/B 实测：主循环不转，连 D-Bus 消息都发不出去），
语音这条链把它又犯了一遍。判据三层，都能红：
`tools/verify-voice-mainloop.sh`（进程级：主线程 wchan 不许是 hrtimer_nanosleep）、
`tests/test_voice_mainloop.py` 的静态 AST 判据、以及其中的 `CAPSPLUS_GUI=1` 行为判据 A
（建窗点必须是主线程）。

**录音期不映射任何窗口**（默认）：xdg-shell 没有"别给我焦点"的请求，真弹了就可能在用户
打字的中间把键盘抢走。要看现场指示：`capsplus-voice status`，或 `CAPSPLUS_VOICE_BADGE=1`
（自担风险，需要真桌面人眼确认）。
"""

from __future__ import annotations

import os
import select
import socket
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from capsplus import voice_asr as ASR            # noqa: E402
from capsplus import voice_inject as INJ         # noqa: E402
from capsplus import voice_proto as P            # noqa: E402
from capsplus import voice_vad as V              # noqa: E402
from capsplus.voice_recorder import Recording, Recorder  # noqa: E402

SPAWN_RETRY_S = 1.5              # 自起 worker 后等多久它开始监听
SPAWN_POLL_S = 0.08
MAX_SESSION_S = 90.0             # 整条链路的兜底时限：卡住也要退出并释放 socket
HISTORY_KEEP = 12
SESSION_TICK_MS = 1000           # 主循环看门狗的节拍（`should_abandon` 每秒问一次）

_GLIB: object | None = None
_GLIB_SEEN = False


def _glib():
    """GLib 模块，或 None（这台机没有 PyGObject）。懒加载 + 进程内缓存。

    顶层 import gi 是禁止的（判据：`tests/test_voice_proto.py` / `test_no_gi_smoke`）；
    GLib 本身不需要显示服务，所以**没有桌面也能建主循环** —— 没有 gi 才退到
    `_run_fallback_loop` 的 `select` 等待。
    """
    global _GLIB, _GLIB_SEEN
    if not _GLIB_SEEN:
        _GLIB_SEEN = True
        try:
            from gi.repository import GLib
            _GLIB = GLib
        except Exception:                                # noqa: BLE001  ImportError / 坏 typelib
            _GLIB = None
    return _GLIB


def should_abandon(session, now: float | None = None, limit: float = MAX_SESSION_S) -> bool:
    """服务端要不要强行丢掉这个会话（纯函数，单测可锁）。

    **review 期永不超时**：弹窗开着的时候是用户在决定，超时把会话丢掉会让
    他按 Enter 之后**文本被静默扔掉**（90s 的兜底时限是给"卡住"用的，不是给人用的）。
    """
    if session is None or session.done():
        return False
    if session.state == "review":
        return False
    return (now if now is not None else time.monotonic()) - session.started_at > limit


# 上屏注入之前给 WM 一点时间把焦点还给原文档（窗口销毁 → 焦点回退是异步的）。
# 单位是毫秒：主线程用 `GLib.timeout_add(FOCUS_RETURN_MS, ...)` 等它，**不睡觉**。
FOCUS_RETURN_MS = 120


class Session:
    """一次 utterance 的现场。`state` 的写入只有一个出口：`_set_state`（顺手广播 + 记历史）。"""

    def __init__(self, server: "Server"):
        self.server = server
        self.state = "idle"
        self.recorder: Recorder | None = None
        self.vad_summary = ""
        self.asr_summary = ""
        self.inject_summary = ""
        self.text = ""
        self._ui = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.discarded = False          # 被服务端丢掉的会话不再发任何事件
        self.started_at = time.monotonic()

    # -- 状态 --------------------------------------------------------------
    def _set_state(self, state: str, **fields) -> None:
        """状态唯一的写入口：先记日志 + 广播，**最后**才改 `self.state` / 置 done。

        顺序是有意的：`wait_done()` 一返回，调用方（单测、客户端）就能读到这条状态的
        广播；反过来先置 done 会出现"会话结束了、事件还差一条没发"的竞态（单测真撞到过一次）。
        """
        if self.discarded and state != "idle":
            return          # 被放弃的会话只许回到 idle，迟到的回调不许再广播别的状态
        note = fields.get("note", "")
        stamp = time.monotonic() - self.started_at
        self.server.history.append(f"{stamp:5.1f}s {state}{' ' + note if note else ''}")
        del self.server.history[:-HISTORY_KEEP]
        P.log_line(f"{state} {note}".strip())
        self.server.broadcast(P.state_event(state, **fields))
        self.state = state
        if state == "idle":
            self._done.set()
            self.server._idle_evt.set()       # 看连接的线程：别再等了（见 `_serve_conn`）

    # -- 三个入口 ----------------------------------------------------------
    def start_recording(self) -> dict:
        """开始录音。已经在录就只回当前状态（幂等，防按键抖动开出两个 arecord）。"""
        with self._lock:
            if self.state != "idle":
                return {"ignored": True, "state": self.state}
            self._done.clear()
            self.server._idle_evt.clear()
            params = self.server.params
            self.recorder = Recorder(
                params, on_state=self._on_recorder_state,
                on_result=self._on_recording_done, on_error=self._on_recorder_error,
                popener=self.server.popen)
            self._set_state("recording", note="开始采集")
            self.recorder.start()
        return {"ignored": False, "state": self.state}

    def stop_current(self) -> dict:
        """再按一次键：录音中就收尾送识别；识别中就等它出结果；弹窗中就丢弃。"""
        with self._lock:
            state = self.state
        if state == "recording" and self.recorder is not None:
            self.recorder.stop("user_stop")
        elif state == "review" and self._ui is not None:
            self._ui.close()
        elif state == "idle":
            # 空闲时再按一次 = 关掉常驻 worker（下次按键会用**当前**代码重新起一个）
            self.server.request_quit()
            return {"ignored": False, "state": "idle", "note": "worker 退出"}
        return {"ignored": False, "state": state}

    def wait_done(self, timeout: float = MAX_SESSION_S) -> bool:
        return self._done.wait(timeout)

    def done(self) -> bool:
        return self._done.is_set()

    def abandon(self, note: str = "超时放弃") -> None:
        """卡住的会话被服务端丢掉：**先停录音**，再回 idle，socket 才能继续接单。

        标 `discarded` 的原因：录音线程可能还在跑，它回头发的 `idle` / `decoding` 会把
        新一轮按键的状态搅乱（实测过一次：1.3s 被误判超时放弃，2.7s 录音自己又回了 idle）。
        """
        self.discarded = True
        if self.recorder is not None:
            self.recorder.stop("abandoned")
        self._set_state("idle", note=note)

    def _alive(self) -> bool:
        return not self.discarded

    # -- 回调（来自录音线程）--------------------------------------------------
    def _on_recorder_state(self, state: str) -> None:
        if state == "recording":
            return                       # 我们在 start_recording 里已经报过了

    def _on_recorder_error(self, code: str, message: str, hint: str) -> None:
        self.server.broadcast(P.error_event(code, message, hint))

    def _on_recording_done(self, rec: Recording) -> None:
        if not self._alive():
            return
        if rec.vad is not None:
            self.vad_summary = rec.vad.summary()
        if not rec.ok:
            # 采集/判定没过：**不冒充"识别中"**，直接报错回 idle
            code = rec.error or (rec.vad.drop_reason if rec.vad is not None else "no_audio")
            note = "没检测到说话" if code in ("no_speech", "too_short") else f"采集失败 {code}"
            self.server.broadcast(P.error_event(str(code), note, self._drop_hint(code)))
            self._set_state("idle", note=note)
            return
        self._set_state("decoding", note=f"送识别 {len(rec.pcm)} 字节")
        # 识别 HTTP 走后台线程（判据 B 锁死：这个 target 里从此不许出现任何 GTK）
        threading.Thread(target=self._decode_and_review, args=(rec,), daemon=True).start()

    @staticmethod
    def _drop_hint(code: str) -> str:
        if code in ("no_speech", "too_short"):
            return ("能量门判定为没说话（本机底噪 RMS≈19，门限下限 150）—— "
                    "对着麦克风说完整一句，或按 Caps+M 后等一秒再开口")
        if code == "no-arecord":
            return "装 alsa-utils"
        return "看 `capsplus-voice status` 里的探测段"

    # -- 识别 + 确认 + 上屏 ---------------------------------------------------
    def _decode_and_review(self, rec: Recording) -> None:
        """**后台线程**：识别 → 交回主线程开窗（或无头直投）。全程不碰 GTK。"""
        result = ASR.recognize(rec.pcm)
        if not self._alive():
            return
        self.asr_summary = result.describe()
        if not result.ok:
            self.server.broadcast(P.error_event(result.code, result.message, result.hint))
            self._set_state("idle", note=f"识别失败 {result.code}")
            return
        self.text = result.text
        if self.server.review_ui and self._ui_available():
            self.server.post(self._open_review)
        else:
            self._inject_directly(self.text, "没有可用的弹窗（无 gi 或无显示），已直接上屏")

    def _ui_available(self) -> bool:
        if os.environ.get("CAPSPLUS_VOICE_UI") == "0":
            return False
        from capsplus import voice_osd                       # 懒导入：无头路径不碰 gi
        return voice_osd.ui_available()

    def _open_review(self) -> None:
        """**主线程**建 review 窗口 + 登记结局回调 —— 整个 worker 唯一的 GTK 使用点。

        `Server.post` 只在主循环活着时才 idle_add；循环没在跑（单测直调、无 gi 降级）
        就原地调用，那会儿它在后台线程 —— 下面的门岗拒绝碰 GTK，走和"弹窗没起来"
        同一条降级，文本绝不静默丢掉。判据 A 验的就是"建窗点在主线程"。
        """
        if threading.current_thread() is not threading.main_thread():
            self._review_failed("不在主线程")
            return
        from capsplus import voice_osd
        self._set_state("review", note=f"待确认 {len(self.text)} 字")
        ui = voice_osd.VoiceUi(on_finish=self._on_review_outcome)
        ui.post_text(self.text)                    # 建窗前的事件进排队，build 时补上
        outcome = ui.open_review()
        if outcome is None:
            self._ui = ui                          # 结局稍后从 on_finish 回调（主线程）
            return
        self._ui = None
        self._review_failed(outcome.action)

    def _review_failed(self, why: str) -> None:
        self._schedule_inject(self.text, f"弹窗没走成（{why}），已直接上屏")

    def _on_review_outcome(self, outcome) -> None:
        """弹窗结局（主线程；`voice_osd` 保证回调发生在**窗口销毁之后**）。"""
        self._ui = None
        action = outcome.action
        if action == "commit":
            self._schedule_inject(outcome.text, "", delay_ms=FOCUS_RETURN_MS)
            return
        if action in ("failed", "unavailable"):
            # 弹窗这条路走不通 ≠ 用户没说完：退回直接上屏，别把文本静默丢掉
            self._schedule_inject(self.text, f"弹窗没走成（{action}），已直接上屏")
            return
        self._set_state("idle", note=f"弹窗 {action}，已丢弃")

    def _schedule_inject(self, text: str, note: str, delay_ms: int = 0) -> None:
        """注入永远**不在主线程跑**：先让主循环等 `delay_ms`（窗口销毁→焦点回退是异步的），
        再交给后台线程去起子进程。旧写法在这里 `time.sleep(delay_s)`，主线程一睡
        mutter 的 ping 就没人应答 —— 真机弹窗事故的半个病根。"""
        def fire() -> None:
            threading.Thread(target=self._inject_directly, args=(text, note),
                             daemon=True).start()
        self.server.after_ms(delay_ms, fire)

    def _inject_directly(self, text: str, note: str) -> None:
        """把文本交给上屏后端。**只在后台线程调用**：主线程路径一律经 `_schedule_inject`。
        弹窗场景下必然发生在窗口销毁之后（见模块 docstring 的时序）。"""
        if not self._alive():
            return
        probe = self.server.probe()
        result = INJ.inject(text, backend=self.server.backend, probe=probe,
                            runner=self.server.run_command)
        self.inject_summary = result.describe()
        self.server.broadcast(P.make_event("text", text_len=len(text),
                                           backend=result.backend, ok=result.ok,
                                           code=result.code, note=note))
        if not result.ok and result.code != "manual":
            self.server.broadcast(P.error_event(result.code, result.message, result.hint))
        self._set_state("idle", note="上屏结束")


class Server:
    """socket 服务端 + 现场。同一时刻只跑一条 utterance（麦克风本来就只有一个）。

    主循环、看门狗、`post`/`after_ms` 都住在这里：`serve_forever()` 起的那个循环是
    **本进程唯一**的事件循环，别处不许再造第二个（判据 B：`tests/test_voice_mainloop.py`）。
    """

    def __init__(self, params: V.VadParams | None = None, *, popener=None,
                 run_command=None, review_ui: bool | None = None):
        self.params = params or V.params_from_env()
        self._popen = popener
        self._run_command = run_command
        self.review_ui = True if review_ui is None else review_ui
        self.history: list[str] = []
        self.backend = INJ.backend_from_env()
        self.session: Session | None = None
        self.watchers: list = []
        self._watch_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._probe: INJ.Probe | None = None
        self._probe_lock = threading.Lock()
        self._quit = threading.Event()
        self._idle_evt = threading.Event()
        self._idle_evt.set()                 # 没有会话 = 就是 idle
        self._loop = None                    # 进程唯一的 GLib.MainLoop（serve 时建）
        self._wake: tuple | None = None      # 无 gi 降级循环的 self-pipe

    # -- 复用两件昂贵的事 ----------------------------------------------------
    def popen(self, argv, **kw):
        import subprocess
        return (self._popen or subprocess.Popen)(argv, **kw)

    def run_command(self, argv, **kw):
        import subprocess
        return (self._run_command or subprocess.run)(argv, **kw)

    def request_quit(self) -> None:
        """任何线程可调。置旗子，然后**叫醒主循环**；收尾（release_server）在
        `serve_forever` 的 finally 里 —— 不在信号上下文里直接 unlink socket 文件。"""
        self._quit.set()
        if self._loop is not None:
            glib = _glib()
            try:
                if glib is not None:
                    glib.idle_add(self._loop.quit)
                    return
            except Exception:                # noqa: BLE001 循环已经没了：退回 self-pipe
                pass
        if self._wake is not None:
            try:
                self._wake[1].send(b"q")
            except OSError:
                pass

    # -- 回主线程 / 主循环定时器（跨线程只搬数据，不碰 GTK）--------------------
    def loop_is_live(self) -> bool:
        """主循环是否在跑（`serve_forever` 在主线程 `run()` 期间才为真）。"""
        return self._loop is not None and self._loop.is_running()

    @staticmethod
    def _guarded(fn):
        """回调异常**只记日志不许掀翻主循环**（PyGI 里回调抛异常能把 loop 打死）。"""
        def inner(*args):
            try:
                return fn(*args)
            except Exception as exc:         # noqa: BLE001
                P.log_line(f"主循环回调异常：{type(exc).__name__}: {exc}")
                return False
        return inner

    def post(self, fn) -> None:
        """把 `fn` 交回主线程（`GLib.idle_add`，照 `qbar_app.serve_socket`）。

        主循环没在跑（CI 直调 / 无 gi 机器）就原地调用 —— 那些场景 GTK 本来就不可能
        可用，`fn` 里的门岗（`_open_review` 的主线程检查）负责拒碰窗口并走降级。
        """
        glib = _glib() if self.loop_is_live() else None
        if glib is not None:
            glib.idle_add(self._guarded(fn))
        else:
            fn()

    def after_ms(self, ms: int, fn) -> None:
        """ms 毫秒后在主线程跑一次 `fn`：首选 `GLib.timeout_add` —— 主线程要"等"
        就只许用循环等；主循环没跑才退 `threading.Timer`（不在循环里，不算睡主线程）。"""
        ms = max(1, int(ms))
        glib = _glib() if self.loop_is_live() else None
        if glib is not None:
            def fire() -> bool:
                fn()
                return False
            glib.timeout_add(ms, self._guarded(fire))
            return
        timer = threading.Timer(ms / 1000.0, fn)
        timer.daemon = True
        timer.start()

    def probe(self) -> INJ.Probe:
        """只缓存**通过**的探测。

        为什么没通过的不缓存：worker 是常驻的（照 Qbar），而 `ydotoold` 常常是**后来**才起来的
        —— 把第一次的"未验证"钉死，明江装了 daemon 之后还会一直得到 manual 的结果。
        """
        with self._probe_lock:
            if self._probe is None or not self._probe.verified:
                self._probe = INJ.run_probe()
            return self._probe

    # -- 事件广播 ------------------------------------------------------------
    def add_watcher(self, conn) -> None:
        with self._watch_lock:
            self.watchers.append(conn)

    def remove_watcher(self, conn) -> None:
        with self._watch_lock:
            if conn in self.watchers:
                self.watchers.remove(conn)

    def broadcast(self, frame: dict) -> None:
        """发给所有在听的连接；写失败的直接摘掉，**不抛**（照 extcheck 的口径）。"""
        dead = []
        data = P.encode(frame)
        with self._watch_lock:
            targets = list(self.watchers)
        for conn in targets:
            try:
                conn.sendall(data)
            except OSError:
                dead.append(conn)
        if dead:
            with self._watch_lock:
                for conn in dead:
                    if conn in self.watchers:
                        self.watchers.remove(conn)

    # -- 命令分派 ------------------------------------------------------------
    def handle(self, frame: dict) -> dict:
        """命令 → 应答。跑在**每连接的后台线程**：只动状态机与 socket，不碰 GTK
        （需要碰窗口的后续动作都从 `Session` 里 `post()` 回主线程）。"""
        if frame.get("kind") == "error":
            return frame
        cmd = frame.get("cmd")
        if cmd == "ping":
            return self._status_frame(pong=True)
        if cmd == "status":
            return self._status_frame()
        if cmd == "start":
            self._ensure_session().start_recording()
            reply = self._status_frame()
            if frame.get("watch"):
                reply["watch"] = True
            return reply
        if cmd == "stop":
            self._ensure_session().stop_current()
            return self._status_frame()
        return P.error_event("bad_command", f"未知命令 {cmd!r}")

    def _ensure_session(self) -> Session:
        with self._session_lock:
            if self.session is None or self.session.state == "idle":
                self.session = Session(self)
            return self.session

    def _status_frame(self, **extra) -> dict:
        session = self.session
        state = session.state if session else "idle"
        note = ""
        if state == "review":
            note = f"待确认 {len(session.text)} 字"
        elif state == "decoding":
            note = session.vad_summary or ""
        return P.state_event(state, note=note, pid=os.getpid(),
                             vad=session.vad_summary if session else "",
                             asr=session.asr_summary if session else "",
                             inject=session.inject_summary if session else "",
                             backend=self.backend,
                             history=list(self.history[-6:]), **extra)

    # -- 主循环 --------------------------------------------------------------
    def serve_forever(self) -> int:
        try:
            server = P.bind_server()
        except OSError as exc:
            print(f"worker 起不来（已有别的 worker？）：{exc}", file=sys.stderr)
            return 1
        print(f"[voice-worker] 监听 {P.socket_path()} pid={os.getpid()}", file=sys.stderr)
        glib = _glib()
        if glib is not None:
            self._loop = glib.MainLoop()       # 进程唯一主循环：**主线程**，活一辈子
        else:
            self._wake = socket.socketpair()   # 无 gi：select + self-pipe 的降级循环
        self._install_signal_handlers()
        threading.Thread(target=self._accept_loop, args=(server,), daemon=True).start()
        try:
            if self._loop is not None:
                glib.timeout_add(SESSION_TICK_MS, self._guarded(self._watchdog_tick))
                self._loop.run()               # 一直到 request_quit 把它停掉
            else:
                self._run_fallback_loop()
        except KeyboardInterrupt:
            pass
        finally:
            P.release_server(server)
        return 0

    def _watchdog_tick(self) -> bool:
        """主线程**唯一**的周期任务（每秒）：90s 兜底时限；review 期永不咬人
        （判据在 `should_abandon`）。返回 False（quit 了）主循环就把这个定时器摘掉。"""
        session = self.session
        if session is not None and not session.done() and should_abandon(session):
            session.abandon(f"超过 {int(MAX_SESSION_S)}s 没走完")
            self.session = None
        return not self._quit.is_set()

    def _run_fallback_loop(self) -> None:
        """没有 GLib 的机器的降级主循环：**阻塞在 select 等 self-pipe**，每轮最多
        空等一个看门狗节拍。注意这是**事件等待**，不是定时器睡觉 —— 主线程一旦
        `clock_nanosleep`，mutter 的 ping 就没人应答（真机事故，`tools/verify-voice-mainloop.sh`
        对这条进程级判据负责，它在任何机器上都断言主线程 wchan 不是 hrtimer_nanosleep）。"""
        while not self._quit.is_set():
            try:
                readable, _, _ = select.select([self._wake[0]], [], [],
                                               SESSION_TICK_MS / 1000.0)
            except (OSError, ValueError):
                return
            if readable:
                try:
                    self._wake[0].recv(64)
                except OSError:
                    return
            self._watchdog_tick()

    def _accept_loop(self, server) -> None:
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn) -> None:
        """一条客户端连接一个线程（本仓里它**永远不是**主线程，可以名正言顺地等）：
        收一帧命令 → 主循环外的状态机处理 → 应答；watch 连接等到会话回 idle 或到点。"""
        watch = False
        with conn:
            try:
                conn.settimeout(P.CMD_TIMEOUT_S)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                line, _rest = buf.split(b"\n", 1)
                frame = P.parse_frame(line.decode("utf-8", "replace"))
                watch = bool(frame.get("watch"))
                reply = self.handle(frame)
                conn.sendall(P.encode(reply))
                if watch and frame.get("cmd") in ("start",):
                    self.add_watcher(conn)
                    conn.settimeout(P.EVENT_TIMEOUT_S)
                    deadline = time.monotonic() + MAX_SESSION_S
                    # 旧写法在这里 `time.sleep(0.2)` 以 5Hz 轮询到 90s；改成等
                    # `_set_state` 置的事件 —— 这里在**后台线程**，不在主循环里睡。
                    self._idle_evt.wait(max(0.0, deadline - time.monotonic()))
            except OSError:
                pass
            finally:
                if watch:
                    self.remove_watcher(conn)

    def _install_signal_handlers(self) -> None:
        """照 `qbar_app._install_signal_handlers`：处理器只 `request_quit()`，
        释放 socket 走主循环退出后的正常路径，不在信号上下文里 unlink 正在用的文件。"""
        import signal

        def bye(_sig, _frame):
            self.request_quit()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, bye)
            except (ValueError, OSError):
                pass                                  # 非主线程装不上就算了，socket 由下次启动清理


# ── 客户端（`capsplus-voice` 走的就是这一半）──────────────────────────────────
def spawn_worker(extra: list[str] | None = None) -> bool:
    """后台起一个 worker（照 Qbar 的"第二次启动只发一个字节"的反面：第一次得自己拉进程）。"""
    import subprocess

    entry = Path(__file__).resolve()
    log = P.log_path()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as sink:
            subprocess.Popen([sys.executable, str(entry), "serve", *(extra or [])],
                             stdout=sink, stderr=sink, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(REPO))
        return True
    except OSError as exc:
        print(f"起不了 worker：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def wait_for_worker(timeout: float = SPAWN_RETRY_S) -> tuple[bool, str]:
    # 此处跑在 `capsplus-voice` **客户端进程**里（一次性命令，退出即散），不在 worker 的
    # 主循环里睡 —— 所以这里的 `time.sleep` 轮询是豁免的（判据 B 只放行这一处）。
    deadline = time.monotonic() + timeout
    alive, reason = False, "没试"
    while time.monotonic() < deadline:
        alive, reason = P.ping()
        if alive:
            return True, reason
        time.sleep(SPAWN_POLL_S)
    return alive, reason


def send(cmd: str, watch: bool = False) -> list[dict]:
    extra = {"watch": True} if watch else {}
    return P.request(cmd, extra=extra)


def ensure_started(cmd: str) -> list[dict]:
    """发命令；worker 不在就先把它拉起来再发（按键路径只有这一条，别让人先"启动服务"）。"""
    frames = send(cmd)
    if frames and frames[0].get("code") == "unreachable":
        if not spawn_worker():
            return [P.error_event("spawn_failed", "拉不起 worker 进程",
                                  hint="前台跑一次看报什么：python3 capsplus/voice_worker.py serve")]
        alive, reason = wait_for_worker()
        if not alive:
            return [P.error_event("worker_not_listening", reason,
                                  hint=f"日志：{P.log_path()}")]
        frames = send(cmd)
    return frames


def _first(frames: list[dict]) -> dict:
    return frames[0] if frames else P.error_event("no_reply", "worker 没答话")


def cmd_toggle() -> int:
    """按键入口。语义定在 `voice_proto.toggle_plan`：空闲→开录，别的→收/取消。"""
    alive, _reason = P.ping()
    state = P.current_state() if alive else None
    cmd = P.toggle_plan(state)
    frames = ensure_started(cmd) if cmd == "start" else send(cmd)
    frame = _first(frames)
    if frame.get("kind") == "error":
        print(f"语音输入：{frame.get('code')} — {frame.get('message', '')}")
        if frame.get("hint"):
            print(f"  下一步：{frame['hint']}")
        return 1
    print(f"[{cmd}] state={frame.get('state', '?')}")
    return 0


def cmd_start() -> int:
    frame = _first(ensure_started("start"))
    print(f"start: state={frame.get('state', frame.get('code', '?'))}")
    return 0 if frame.get("kind") == "state" else 1


def cmd_stop() -> int:
    frame = _first(send("stop"))
    print(f"stop: state={frame.get('state', frame.get('code', '?'))}")
    return 0 if frame.get("kind") == "state" else 1


def cmd_status() -> int:
    """一页看全四条链路：worker / 识别服务 / 上屏探测 / 弹窗。出问题就照这一页查。"""
    alive, reason = P.ping()
    print(f"worker     : {reason}")
    frame = _first(send("status")) if alive else {}
    if frame.get("kind") == "state":
        print(f"  状态     : {frame.get('state')}  {frame.get('note', '')}")
        if frame.get("vad"):
            print(f"  能量门   : {frame['vad']}")
        if frame.get("asr"):
            print(f"  上次识别 : {frame['asr']}")
        if frame.get("inject"):
            print(f"  上次上屏 : {frame['inject']}")
        for line in frame.get("history") or []:
            print(f"  · {line}")
    else:
        print(f"  状态     : 问不到（{frame.get('code', '没在跑')}）")

    health = ASR.health()
    print(f"识别服务   : " + (f"http://{ASR.host()}:{ASR.port()} {health.text}"
                             if health.ok else
                             f"不可用 {health.code} — {health.message}"))
    if not health.ok:
        print(f"  下一步   : {health.hint}")

    probe = INJ.run_probe()
    print(f"上屏后端   : {frame.get('backend') or INJ.backend_from_env()}"
          + ("（探测通过，可自动注入）" if probe.verified else "（未验证，见下）"))
    for line in probe.report_lines():
        print(f"  {line}")
    if not probe.verified:
        print(f"  怎么办   : {INJ.unverified_reason(probe)}")

    from capsplus import voice_osd                       # 只用到纯函数，不建窗
    print(f"确认弹窗   : " + ("可用（review 期会夺焦点）" if voice_osd.ui_available()
                              else "不可用 ⇒ 识别完直接走上屏后端"))
    print(f"录音期弹窗 : " + ("开（CAPSPLUS_VOICE_BADGE=1，可能抢焦点）"
                              if voice_osd.badge_enabled() else "关（Wayland 下不抢焦点的唯一做法）"))
    p_ = V.params_from_env()
    print(f"能量门参数 : frame={p_.frame_ms}ms gate=max({p_.gate_floor:.0f}, "
          f"{p_.gate_multiple:.0f}×底噪) 尾静音={p_.silence_stop_ms}ms "
          f"最长={p_.max_ms}ms 最短有声={p_.min_voiced_ms}ms")
    notes = V.env_notes()
    if notes:
        print(f"  环境变量覆盖: {' '.join(notes)}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "serve":
        return Server().serve_forever()
    if cmd == "toggle":
        return cmd_toggle()
    if cmd == "start":
        return cmd_start()
    if cmd == "stop":
        return cmd_stop()
    if cmd == "status":
        return cmd_status()
    print(f"用法：capsplus-voice toggle|start|stop|status（serve 是内部用的）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
