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

    Caps+M → recording ─(VAD 自适应尾静音 1200ms 起 / 录满 60s / 用户再按一次)→ decoding
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
    commit/cancel 由 `voice_osd.VoiceUi.on_finish` 回调推进状态机；注入的条件是**事实**而不是
    计时 —— 必须等到窗口 `unmap` 或收到 `focus_out`（`voice_osd.RELEASE_KINDS`），
    `INJECT_FLOOR_MS` 只是**下限**、`INJECT_WAIT_CAP_MS` 只是**上限兜底**；等满上限还拿不到
    任何这类信号，就带着诊断注入并把这一次标成**未确认**（Wayland 不保证给我们这种通知），
    绝不写成"上屏结束"。等待全程由主循环计时（`GLib.timeout_add`），不是主线程 sleep ——
    否则 Ctrl+V 会贴进我们自己的窗口，而"贴进去又随窗口消失"就是用户看到的"闪一下"；
  * **剪贴板的还原同一条时序模型**（2026-10-17 补）：上屏前在后台线程读一份内存快照，
    粘贴之后由主循环 `after_ms(CLIPBOARD_SETTLE_MS, …)` 计时，到点再起一个后台线程跑
    `voice_inject.perform_restore`。快照**只在内存**，永不落盘（判据
    `tests/test_voice_clipboard.py::TestSnapshotNeverHitsDisk`）。

为什么定成这个样子（真机事故，2026-10-17）：旧写法每个 utterance 新开一个 GTK 线程 +
`GLib.MainLoop`，主线程 `join()` + `time.sleep` 轮询；mutter 对客户端的 ping 落在主线程
上没人应答，用户的 GNOME 直接弹「"voice_worker.py" 无响应（等待/强制退出）」。本仓早就
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
import struct
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from capsplus import voice_asr as ASR            # noqa: E402
from capsplus import voice_inject as INJ         # noqa: E402
from capsplus import voice_levels as L           # noqa: E402
from capsplus import voice_proto as P            # noqa: E402
from capsplus import voice_vad as V              # noqa: E402
from capsplus.voice_recorder import Recording, Recorder  # noqa: E402

SPAWN_RETRY_S = 1.5              # 自起 worker 后等多久它开始监听
SPAWN_POLL_S = 0.08
# 整条链路的兜底时限：卡住也要退出并释放 socket。
# 2026-10-20 从 90s 提到 180s：录音上限本身已经是 60s（`voice_vad.MAX_MS`），
# 90s 只够"录满 60s + 识别 5s"，一次慢识别就会把**还在弹窗里等用户按 Enter 的会话**判死。
# review 期本来就不超时（`should_abandon`），这条兜底只管卡住 —— 留一倍余量。
MAX_SESSION_S = 180.0
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


# ── 注入的触发条件（2026-10-17 改口径：从"拍脑袋计时"改成"确认自己已经不在焦点链上"）──
#
# 旧写法只有一个 `FOCUS_RETURN_MS = 120` 的定时器：窗口销毁之后固定等 120ms 就发 Ctrl+V。
# 真机现象（用户 17:19：文字像贴上去了又被删掉，"闪烁了一下"）说明**这段时间既不充分也不是
# 证据** —— 它只是"过了 120 毫秒"，不说明焦点交给了谁。现在三段分开，判据各自钉在段上：
#
#   必要条件  时间线里出现 `unmap` 或 `focus_out`（`voice_osd.RELEASE_KINDS`）：
#             我们**确实已经把窗口撤了、也已经不在焦点链上**，事实到了才动手；
#   下限      `INJECT_FLOOR_MS`：事实到得再早也不许比这更快动手。X11/GTK3 实测（Xvfb +
#             `CAPSPLUS_GUI=1`，2026-10-17 晚）：`destroy()` 里 **unmap 与销毁是同步的**，
#             Enter 之后 0.03ms 就到；`focus_out` 在没有窗口管理器的环境里**根本没来**。
#             所以"只等事件"在那个平台上等于不等。下限取 120ms = 用户一直活着、本轮真机
#             也确实在跑的那个值 —— 这次改动**不许把注入提得比现状更早**。
#   上限兜底  `INJECT_WAIT_CAP_MS`：等满了仍然一个"焦点交出"的信号都没拿到（Wayland 不保证
#             把这种通知交给客户端），**带着诊断**注入，并把这一次标成**未确认**而不是
#             "上屏结束"。取 400ms 的理由：它比已知不够用的 120ms 大一档；最坏路径
#             400 + 300（剪贴板还原）= 0.7s，用户感觉不到"卡"，离 90s 看门狗差两个数量级。
#
# 单位都是毫秒；等待一律由主循环计时（`Server.after_ms`），**主线程不许睡觉**。
INJECT_FLOOR_MS = 120
INJECT_WAIT_CAP_MS = 400
# 诊断行里 `gate=` 的取值 = "是谁让的这一步"：
#   event  焦点交出的事实晚于下限，是**事件**让的步（最理想：既快又有证据）
#   floor  事实早就到了，是**下限**到点让的步（等价于旧行为，但这一次有证据）
#   cap    到上限仍没观察到任何焦点交出的信号 ⇒ **未确认**
#   nowin  本次压根没有弹窗（降级直投），没有"贴进自己"这回事
INJECT_GATES = ("event", "floor", "cap", "nowin")


def _tri(value) -> str:
    """三态布尔 → `1 / 0 / na`：诊断行里"没观察到"与"观察到是假"必须是两个不同的字。"""
    if value is None:
        return "na"
    return "1" if value else "0"


def _num(value, missing: int = -1) -> str:
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return str(missing)


def timeline_token(timeline) -> str:
    """把时间线压成一个 token：`map@-8,focus_in@-7,finish@0,unmap@250,destroy@250`（**毫秒**，相对 Enter）。

    只有 `kind` 与时刻，别的什么都没有 —— 用户真机日志里最想要的就这一串：
    单位与 `enter_ms`/`release_ms` 一致（微秒读起来没人能对得上现象），Enter 之前的事件是负数
    （`map@-8` = 开窗后 8 毫秒才进焦点链），之后的是正数。
    """
    marks = [e for e in (timeline or []) if isinstance(e, dict) and e.get("kind") == "finish"]
    base = int(marks[-1].get("us", 0)) if marks else 0
    out = []
    for entry in (timeline or []):
        if not isinstance(entry, dict):
            continue
        us = int(entry.get("us", 0) or 0)
        out.append(f"{entry.get('kind', '?')}@{round((us - base) / 1000.0)}")
    return ",".join(out) if out else "empty"


def inject_diag_line(diag: dict) -> str:
    """`inject_diag key=value …`：**固定前缀 + key=value**，给用户拿去和真机现象对齐。

    里面只有状态量：弹窗可见性/实化/mapped、Enter→注入与 Enter→焦点交出的毫秒数、
    让这一步的是哪个 gate、时间线 token。**识别文本、剪贴板内容一个字符都不许进这一行**
    （隐私判据：`tests/test_voice_focus_gate.py::TestDiagnosticsCarryNoText`）。
    """
    parts = (f"gate={diag.get('gate') or 'none'}",
             f"unconfirmed={1 if diag.get('unconfirmed') else 0}",
             f"enter_ms={_num(diag.get('enter_ms'))}",
             f"release_ms={_num(diag.get('release_ms'))}",
             f"visible={_tri(diag.get('visible'))}",
             f"realized={_tri(diag.get('realized'))}",
             f"mapped={_tri(diag.get('mapped'))}",
             f"unmap={_tri(diag.get('unmap_seen'))}",
             f"focus_out={_tri(diag.get('focus_out_seen'))}",
             f"destroy={_tri(diag.get('destroy_seen'))}",
             f"entries={_num(diag.get('entries'), 0)}",
             f"tl={timeline_token(diag.get('timeline'))}")
    return "inject_diag " + " ".join(parts)


def inject_done_line(diag: dict) -> str:
    """注入**回来之后**的那一行：后端、成不成、代码、步数、写入毫秒数。同样只有状态量。

    `copy_ms=`（2026-10-19 补）是"写剪贴板那步的父进程**几毫秒退的**"——事故当天的形状是
    等不到管道 EOF、整整 `STEP_TIMEOUT_S` 5 秒判死；修好后这一列该是毫秒级的小数字。
    没跑到写入步（manual / 前置失败）记 -1；`code=owner_rejected` 时它是"rc=0 但所有权没拿到"
    那次尝试的耗时。
    """
    parts = (f"backend={diag.get('backend') or 'none'}",
             f"ok={1 if diag.get('ok') else 0}",
             f"code={diag.get('code') or '-'}",
             f"steps={_num(diag.get('steps'), 0)}",
             f"gate={diag.get('gate') or 'none'}",
             f"unconfirmed={1 if diag.get('unconfirmed') else 0}",
             f"copy_ms={_num(diag.get('copy_ms'), -1)}")
    return "inject_done " + " ".join(parts)


def restore_diag_line(branch: str, still_ours=None, delay_ms=None) -> str:
    """剪贴板还原的作业最终落进哪个分支：`armed → restored/not_ours/verify_mismatch/…`，
    没作业就是 `skip`。`still_ours` 就是"注入后回读剪贴板是否仍等于我们写的那段"。"""
    parts = [f"branch={branch or 'none'}", f"still_ours={_tri(still_ours)}"]
    if delay_ms is not None:
        parts.append(f"delay_ms={_num(delay_ms, 0)}")
    return "restore_diag " + " ".join(parts)


def build_diag(ui, gate: str, inject_us: int, finish_us: int) -> dict:
    """把"注入那一刻"的状态量攒成一个**纯数据** dict（诊断行与 status 那行都从它出）。

    `ui` 为 None = 这次根本没有弹窗（降级直投），状态量全 `na`，只有 gate 与间隔。
    """
    diag: dict = {"gate": gate, "unconfirmed": gate == "cap"}
    probe = ui.probe() if ui is not None else {}
    probe = probe or {}
    diag.update(probe)
    base = int(finish_us or probe.get("finish_us") or inject_us)
    diag["finish_us"] = base
    diag["inject_us"] = int(inject_us)
    diag["enter_ms"] = round((int(inject_us) - base) / 1000.0, 1)
    released = bool(probe.get("released"))
    diag["released"] = released
    diag["release_ms"] = (round((int(probe.get("at_us", 0)) - base) / 1000.0, 1)
                          if released else None)
    diag.setdefault("entries", 0)
    diag.setdefault("timeline", [])
    for key in ("visible", "realized", "mapped", "unmap_seen", "focus_out_seen",
                "destroy_seen"):
        diag.setdefault(key, None)
    return diag


def idle_note(diag: dict) -> str:
    """回 idle 的那句话：**没证实的事不许写成成功**。"""
    if diag and diag.get("code") == "owner_rejected":
        # 2026-10-19：写入"rc=0"但回读自证没过 ⇒ 连粘贴都没发，这不是"结束"，是**失败**。
        return "上屏失败：wl-copy 没拿到剪贴板所有权（rc=0 但回读不是我们的文本）"
    if diag and diag.get("gate") == "cap":
        return "上屏未确认：未观察到焦点交出，注入可能落进自己"
    return "上屏结束"


def verdict_line(diag: dict, clip_diag: dict | None = None) -> str:
    """`status` 里"上次上屏：结论 + 依据"那**一行**（用户只要贴这一行给我，不必贴整页）。

    结论放在最前面，第二个词就是依据；后面跟"凭什么"的状态量。控制在一行内 ——
    这行是他复制粘贴的唯一入口，长了就等于没有。
    """
    if not diag:
        return ""
    gate = diag.get("gate") or "none"
    head = {
        "cap": "结论=未确认 依据=未观察到焦点交出，注入可能落进自己",
        "nowin": "结论=已上屏 依据=本次没有弹窗（降级直投）",
    }.get(gate, f"结论=已上屏 依据=已确认窗口撤走")
    window = (f"vis={_tri(diag.get('visible'))}/real={_tri(diag.get('realized'))}"
              f"/map={_tri(diag.get('mapped'))}")
    clip = clip_diag or {}
    still = clip.get("still_ours")
    readback = "回读=还是我们写的" if still is True else (
        "回读=已被别人改" if still is False else "回读=没读到")
    parts = [head, f"gate={gate}",
             f"Enter→注入={_num(diag.get('enter_ms'))}ms",
             f"Enter→焦点交出={_num(diag.get('release_ms'))}ms",
             f"窗口={window}",
             f"后端={diag.get('backend') or 'none'}",
             f"ok={_tri(diag.get('ok'))}",
             f"code={diag.get('code') or '-'}",
             f"还原={clip.get('branch') or '还没到点'}",
             readback]
    return " ".join(parts)


def truncation_hint(vad) -> str:
    """"录满上限被截断"的提示文案（空串 = 没截断）。

    为什么要一条**专门**的文案：旧行为是到了 `MAX_MS` 就静默收尾，用户只知道"我后半句没了"，
    查不出是设备、服务还是这个上限（用户 2026-10-20 的原话就是「只可以是短语音」）。
    现在这句话会同时出现在：`decoding` 事件、review 弹窗顶部、`voice.log`、`status` 的能量门行。
    """
    if vad is None or vad.stop_reason != "max_duration":
        return ""
    return (f"录到 {int(vad.total_ms / 1000)}s 上限就截断了，后半句没录上"
            f" —— 说完一句就再按一次 Caps+M")


class Session:
    """一次 utterance 的现场。`state` 的写入只有一个出口：`_set_state`（顺手广播 + 记历史）。"""

    def __init__(self, server: "Server"):
        self.server = server
        self.state = "idle"
        self.recorder: Recorder | None = None
        self.vad_summary = ""
        self.level_summary = ""        # 本次语音段的 RMS/峰值/增益（只有状态量，无内容）
        self.truncation = ""           # 录满上限时的提示（空串 = 没截断）
        self.asr_summary = ""
        self.inject_summary = ""
        self.inject_diag: dict = {}       # 注入那一刻的状态量（纯数据，无文本）
        self.text = ""
        self._ui = None
        self.badge_ui = None           # 录音期那块指示牌（默认关，见 voice_osd.badge_enabled）
        self.live_summary = ""         # 录音**进行中**的那行读数（只有状态量，无内容）
        self.progress_logged_ms = 0    # 上一次把心跳写进日志时的时长（限流用）
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
        # 牌子必须在**主线程**建（`handle` 跑在每连接的后台线程，GTK3 不许跨线程建窗 ——
        # 2026-10-17 那次「无响应」的病根就是这一条），所以走 post 回主线程。
        self.server.post(self._open_badge)
        return {"ignored": False, "state": self.state}

    # -- 录音期指示牌（默认关）--------------------------------------------------
    def _open_badge(self) -> None:
        """主线程：录音期那块指示牌。**默认关**，`CAPSPLUS_VOICE_BADGE=1` 才真弹
        （理由：Wayland 没有"别把焦点给我"，见 `voice_osd` 模块 docstring 第 1 条）。"""
        if self.state != "recording" or not self._alive():
            return
        from capsplus import voice_osd
        if not voice_osd.badge_enabled():
            return
        ui = voice_osd.VoiceUi()
        if not ui.open_badge():
            ui.close_badge()
            return
        self.badge_ui = ui

    def _close_badge(self) -> None:
        """主线程：录音结束就把牌子收掉（它没有结局要交回，不进 review 那条回调链）。"""
        ui, self.badge_ui = self.badge_ui, None
        if ui is not None:
            ui.close_badge()

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
        self.server.post(self._close_badge)
        self._set_state("idle", note=note)

    def _alive(self) -> bool:
        return not self.discarded

    # -- 回调（来自录音线程）--------------------------------------------------
    def _on_recorder_state(self, state: str) -> None:
        if state == "recording":
            return                       # 我们在 start_recording 里已经报过了

    def _on_recorder_error(self, code: str, message: str, hint: str) -> None:
        self.server.post(self._close_badge)      # 牌子不能留在屏幕上（这条回调在录音线程）
        self.server.broadcast(P.error_event(code, message, hint))

    def _on_recording_done(self, rec: Recording) -> None:
        self.server.post(self._close_badge)
        if not self._alive():
            return
        if rec.vad is not None:
            self.vad_summary = rec.vad.summary()
        if rec.levels is not None:
            # 电平进日志与 status：下次抱怨"识别不准"时，这一行能直接回答"是不是说得太轻"。
            self.level_summary = rec.levels.describe()
            P.log_line(f"voice_level {self.level_summary}")
        if not rec.ok:
            # 采集/判定没过：**不冒充"识别中"**，直接报错回 idle
            code = rec.error or (rec.vad.drop_reason if rec.vad is not None else "no_audio")
            note = "没检测到说话" if code in ("no_speech", "too_short") else f"采集失败 {code}"
            self.server.broadcast(P.error_event(str(code), note, self._drop_hint(code)))
            self._set_state("idle", note=note)
            return
        self.truncation = truncation_hint(rec.vad)
        note = f"送识别 {len(rec.pcm)} 字节"
        if self.truncation:
            P.log_line(f"truncated {self.truncation}")
            note += f"（{self.truncation}）"
        self._set_state("decoding", note=note)
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
        if self.truncation:
            # 必须**在** post_text 之前：`_show_review` 建窗时才去读那行提示（晚设就被默认标题盖掉）
            ui.set_review_hint(self.truncation)
        ui.post_text(self.text)                    # 建窗前的事件进排队，build 时补上
        outcome = ui.open_review()
        if outcome is None:
            self._ui = ui                          # 结局稍后从 on_finish 回调（主线程）
            return
        self._ui = None
        self._review_failed(outcome.action)

    def _review_failed(self, why: str) -> None:
        self._arm_inject(self.text, f"弹窗没走成（{why}），已直接上屏", None)

    def _on_review_outcome(self, outcome) -> None:
        """弹窗结局（主线程；`voice_osd` 保证回调发生在**窗口销毁之后**）。

        `ui` 交回给注入那条路：诊断要问的"窗口此刻还可见吗/还 mapped 吗"只有它答得了，
        而 `probe()` 里读 widget ⇒ **只能在主线程做**，所以这里把对象传下去、不传结论。
        """
        ui, self._ui = self._ui, None
        action = outcome.action
        if action == "commit":
            self._arm_inject(outcome.text, "", ui)
            return
        if action in ("failed", "unavailable"):
            # 弹窗这条路走不通 ≠ 用户没说完：退回直接上屏，别把文本静默丢掉
            self._arm_inject(self.text, f"弹窗没走成（{action}），已直接上屏", None)
            return
        if ui is not None:
            ui.forget()
        self._set_state("idle", note=f"弹窗 {action}，已丢弃")

    def _arm_inject(self, text: str, note: str, ui) -> None:
        """**注入的触发条件**：等到"窗口已 unmap 或已收到 focus-out"这个**事实**才动手。

        三段（必要条件 / 下限 / 上限兜底）写在文件头的常量上。形状是"一次武装、三个问讯"：
        下限定时器、上限定时器、焦点交出回调 —— 谁先到都只干同一件事，问一次
        `try_fire()`；一次性闸门保证**只注入一次**。等待全部由主循环计时，
        本函数与它挂上的每个回调都只搬数据 + 起后台线程，不睡、不 join、不 `wait()`
        （`BLOCK-ON-MAIN-PATH` 把这几个函数名钉在主线程名册里）。
        """
        from capsplus import voice_osd as OSD

        armed_us = OSD.now_us()
        # 起点是"用户按下 Enter"那一刻（`_finish` 打的点）。取不到就退化成"此刻"：
        # 绝不能拿 0 当起点 —— 那会让 `span` 变成一个巨大的数，兜底与下限当场全部误判成立。
        mark = int(ui.release_fact()["finish_us"]) if ui is not None else 0
        finish_us = mark or armed_us
        once = threading.Lock()
        state = {"fired": False}

        def fire(gate: str) -> None:
            with once:
                if state["fired"]:
                    return
                state["fired"] = True
            self._inject_with_diagnosis(text, note, ui, gate, OSD.now_us(), finish_us)

        def try_fire() -> None:
            """每次有人问（事件到 / 下限到 / 上限到）都问同一句：**条件齐了吗**。

            只在 `ui` 存在时会被叫到 —— 没有弹窗那条路在上面已经 `fire("nowin")` 走掉了。
            """
            fact = ui.release_fact()
            span = OSD.now_us() - finish_us
            if fact["released"]:
                if span < INJECT_FLOOR_MS * 1000:
                    return                       # 下限还没到：等下限定时器再问一次
                fire("event" if int(fact["at_us"]) - finish_us >= INJECT_FLOOR_MS * 1000
                     else "floor")
                return
            if span >= INJECT_WAIT_CAP_MS * 1000:
                fire("cap")                      # 到上限仍没有任何焦点交出的信号

        if ui is None:
            fire("nowin")
            return
        ui.on_release(lambda _fact: try_fire())
        self.server.after_ms(INJECT_FLOOR_MS, try_fire)
        self.server.after_ms(INJECT_WAIT_CAP_MS, try_fire)

    def _inject_with_diagnosis(self, text: str, note: str, ui, gate: str,
                               inject_us: int, finish_us: int) -> None:
        """在**主线程**把状态量取齐（读 widget 只能在这儿），再把注入交给后台线程。

        交出去的是 `diag` 那份纯数据 —— `_inject_directly` 里再碰一次 GTK 就是
        `UI-IN-THREAD` 那个 AST 门要红的地方。
        """
        diag = build_diag(ui, gate, inject_us, finish_us)
        P.log_line(inject_diag_line(diag))
        self.server.last_inject_diag = diag
        self.inject_diag = diag
        if ui is not None:
            ui.forget()
        threading.Thread(target=self._inject_directly, args=(text, note, diag),
                         daemon=True).start()

    def _inject_directly(self, text: str, note: str, diag: dict | None = None) -> None:
        """把文本交给上屏后端。**只在后台线程调用**：主线程路径一律经 `_arm_inject`。
        弹窗场景下必然发生在窗口销毁**并且焦点交出的事实被观察到**之后（见文件头三段）。"""
        if not self._alive():
            return
        probe = self.server.probe()
        result = INJ.inject(text, backend=self.server.backend, probe=probe,
                            runner=self.server.run_command)
        self.inject_summary = result.describe()
        done = dict(diag or {})
        done.update(backend=result.backend or "", ok=bool(result.ok),
                    code=result.code or "", steps=int(result.steps or 0),
                    copy_ms=result.copy_ms)
        P.log_line(inject_done_line(done))
        self.server.last_inject_diag = done
        self.inject_diag = done
        # 快照已经在 `inject` 里读进内存了：这里只负责把"过一会儿条件还原"的等待挂到主循环。
        # 顺序是判据：snapshot（在 inject 内，先于 wl-copy）→ 写 → 粘贴 → 还原（在这里挂出去）。
        self.server.arm_clipboard_restore(result.restore_job, result.clipboard_note)
        self.server.broadcast(P.make_event("text", text_len=len(text),
                                           backend=result.backend, ok=result.ok,
                                           code=result.code, note=note))
        if not result.ok and result.code != "manual":
            self.server.broadcast(P.error_event(result.code, result.message, result.hint))
        self._set_state("idle", note=idle_note(done))


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
        self._clip_job: INJ.RestoreJob | None = None   # 待还原的剪贴板快照（**只在内存**）
        self._clip_lock = threading.Lock()
        self.clipboard_summary = ""              # 进 status 的那句话：只有动作 + 类型/字节数
        self.clipboard_diag: dict = {}           # 还原作业最终落进哪个分支（结构化，可解析）
        self.last_inject_diag: dict = {}         # 上一次注入的状态量（进 status 的 verdict）
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
        —— 把第一次的"未验证"钉死，用户装了 daemon 之后还会一直得到 manual 的结果。
        """
        with self._probe_lock:
            if self._probe is None or not self._probe.verified:
                self._probe = INJ.run_probe()
            return self._probe

    # -- 剪贴板快照的还原（等待走主循环，读写走后台线程）------------------------
    def arm_clipboard_restore(self, job, note: str = "") -> None:
        """收下这次上屏的快照，把"过一会儿再还原"的**等待**挂到主循环上。

        三件事都在这一个方法里定死，顺序不许调：
          1. **为什么要等**：`wl-copy` 退出码 0 只代表选区所有权挂上了，目标应用收到 Ctrl+V
             之后还要异步来取数据；这之前就回写等于把刚递出去的东西抽走
             （`INJ.CLIPBOARD_SETTLE_MS` 的注释）。
          2. **为什么用 `after_ms` 而不是 sleep**：本进程的主线程一睡，mutter 的 ping 就没人应答
             —— 真机「无响应」弹窗的病根，判据三层见模块 docstring。`after_ms` 到点只做一件事：
             起后台线程，主循环继续转。
          3. **单槽**：同时只留**最早**那份快照，那才是用户原来的东西。第二次上屏赶上第一次还没
             到点，就只把自己的文本追加进 `ours`（"还是不是我们写的"要认这两段），新快照丢弃 ——
             拿上一次的中间态当原物还原是错的。
        """
        if note:
            self._set_clipboard_summary(note)
        if job is None:
            # 没有作业 = 这次**不会**有任何东西回来（开关关着 / 备份没成 / 降级 / manual）。
            # 这条分支必须落进日志：用户看到的"闪一下"如果正是 300ms 后的还原，这里会是 armed。
            self.clipboard_diag = {"branch": "skip", "still_ours": None}
            P.log_line(restore_diag_line("skip"))
            return
        self.clipboard_diag = {"branch": "armed", "still_ours": None,
                               "delay_ms": int(job.delay_ms)}
        P.log_line(restore_diag_line("armed", None, job.delay_ms))
        with self._clip_lock:
            pending = self._clip_job
            if pending is None:
                self._clip_job = job
            else:
                pending.ours.extend(job.ours)
                job.snapshot.data = None          # 丢弃中间态，顺手把这份字节从内存里放掉
                self.clipboard_diag = {"branch": "merged", "still_ours": None,
                                       "delay_ms": int(pending.delay_ms)}
                P.log_line(restore_diag_line("merged", None, pending.delay_ms))
        self.after_ms(job.delay_ms, self._fire_clipboard_restore)

    def _fire_clipboard_restore(self) -> None:
        """主循环到点后的动作：**只把活交给后台线程**，自己不碰子进程（形状同 `_arm_inject`）。"""
        threading.Thread(target=self.run_clipboard_restore, daemon=True).start()

    def run_clipboard_restore(self) -> None:
        """**后台线程**：查"还是不是我们写的那段"→ 条件还原 → 结论写进摘要。

        取不到作业就是已经被前一次到点做掉了（幂等，绝不重复盖写用户的剪贴板）。
        摘要与日志里只许出现**动作、类型、字节数**，一个内容字符都不许。
        """
        with self._clip_lock:
            job, self._clip_job = self._clip_job, None
        if job is None:
            return
        result = INJ.perform_restore(job, self.run_command)
        self._set_clipboard_summary(result.describe())
        self.clipboard_diag = {"branch": result.code, "still_ours": result.still_ours}
        P.log_line(restore_diag_line(result.code, result.still_ours))   # 只有代码与三态布尔

    def _set_clipboard_summary(self, text: str) -> None:
        with self._clip_lock:
            self.clipboard_summary = text

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
                             level=session.level_summary if session else "",
                             live=session.live_summary if session else "",
                             truncated=session.truncation if session else "",
                             asr=session.asr_summary if session else "",
                             inject=session.inject_summary if session else "",
                             diag=dict(self.last_inject_diag),
                             verdict=verdict_line(self.last_inject_diag, self.clipboard_diag),
                             clipboard=self.clipboard_summary,
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
        （判据在 `should_abandon`）。返回 False（quit 了）主循环就把这个定时器摘掉。

        录音期顺手把心跳刷一次（`_record_progress`）。**刻意不再开第二个 timer**：
        本仓为"主线程只有一个周期任务"这条定过口径（`tools/verify-voice-mainloop.sh`
        钉的是主线程不许睡觉），第二个定时器迟早会和这个咬在一起。1 Hz 对"还在录／录了多久／
        现在多响"完全够 —— 要更细的连续波形用 `capsplus-voice meter --live`。
        """
        session = self.session
        if session is not None and not session.done() and should_abandon(session):
            session.abandon(f"超过 {int(MAX_SESSION_S)}s 没走完")
            self.session = None
        elif session is not None and session.state == "recording" and session.recorder is not None:
            self._record_progress(session)
        return not self._quit.is_set()

    PROGRESS_LOG_EVERY_MS = 5000      # 心跳进日志的限流：首拍一次，之后每 5s 一次

    def _record_progress(self, session: "Session") -> None:
        """主线程：把录音当前读数刷到牌子上、写进 status，并按 5s 限流记一行日志。

        为什么要有（2026-10-20）：`voice.log` 过去只有录完那一行 `voice_level`，
        于是"他到底有没有在说话"在**录的时候**是黑箱；端点判定偏偏是按静音时长下刀的，
        用户看不见就会以为"它自己断了"。
        """
        from capsplus import voice_osd                      # 只用到纯函数，不建窗
        live = session.recorder.live()
        session.live_summary = voice_osd.recording_line(
            live["elapsed_ms"], live["rms"], live["peak"], live["gate"], self.params.max_ms)
        if session.badge_ui is not None:
            session.badge_ui.post_progress(live["elapsed_ms"], live["rms"], live["peak"],
                                           live["gate"], self.params.max_ms)
        logged = session.progress_logged_ms
        if logged == 0 or live["elapsed_ms"] - logged >= self.PROGRESS_LOG_EVERY_MS:
            session.progress_logged_ms = live["elapsed_ms"]
            P.log_line(f"voice_progress elapsed_ms={live['elapsed_ms']} "
                       f"rms={live['rms']:.0f} peak={live['peak']} gate={live['gate']:.0f} "
                       f"speaking={int(bool(live['speaking']))}")

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
    """一页看全五条链路：worker / 识别服务 / 上屏探测 / 剪贴板还原 / 弹窗。出问题照这一页查。"""
    alive, reason = P.ping()
    print(f"worker     : {reason}")
    frame = _first(send("status")) if alive else {}
    if frame.get("kind") == "state":
        print(f"  状态     : {frame.get('state')}  {frame.get('note', '')}")
        if frame.get("vad"):
            print(f"  能量门   : {frame['vad']}")
        if frame.get("level"):
            # 只有 RMS/峰值/增益：回答"这次说话有多响"，一个内容字符都没有
            print(f"  电平增益 : {frame['level']}")
        if frame.get("truncated"):
            print(f"  截断提示 : {frame['truncated']}")
        if frame.get("asr"):
            print(f"  上次识别 : {frame['asr']}")
        if frame.get("inject"):
            print(f"  上次上屏 : {frame['inject']}")
        if frame.get("verdict"):
            # 一行看全"结论 + 依据"：出问题只要把这一行贴过来就够定位，不必贴整页 status。
            print(f"  上屏结论 : {frame['verdict']}")
        if frame.get("clipboard"):
            # 只报动作 + 类型 + 字节数；剪贴板里的内容一个字都不到这里（隐私判据钉这行）
            print(f"  剪贴板   : {frame['clipboard']}")
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
    print("剪贴板还原 : " + ("开（粘完条件还原，只在剪贴板还是我们写的那段时动手）"
                            if INJ.restore_from_env() else
                            "关（CAPSPLUS_VOICE_RESTORE_CLIPBOARD=0）⇒ 上屏会覆盖且不还原"))

    from capsplus import voice_osd                       # 只用到纯函数，不建窗
    print(f"确认弹窗   : " + ("可用（review 期会夺焦点）" if voice_osd.ui_available()
                              else "不可用 ⇒ 识别完直接走上屏后端"))
    print(f"录音期弹窗 : " + ("开（CAPSPLUS_VOICE_BADGE=1，可能抢焦点）"
                              if voice_osd.badge_enabled() else "关（Wayland 下不抢焦点的唯一做法）"))
    p_ = V.params_from_env()
    print(f"能量门参数 : frame={p_.frame_ms}ms gate=max({p_.gate_floor:.0f}, "
          f"{p_.gate_multiple:.0f}×底噪) 尾静音={p_.silence_stop_ms}~"
          f"{p_.silence_stop_max_ms}ms（每说满 1s 多容忍 {p_.silence_grow_per_s}ms）"
          f" 最短有声={p_.min_voiced_ms}ms")
    # 上限单独一行：它是"只能录短语音"那个症状的直接答案，别混在上一条里被看漏
    print(f"录音上限   : {p_.max_ms}ms（{p_.max_ms / 1000:.0f}s）—— 到点强制收尾，"
          f"弹窗与日志里都会写明「截断」，不再静默；要改就 CAPSPLUS_VOICE_MAX_MS")
    print(f"前后留手   : 句首 {p_.trim_pad_head_ms}ms / 句尾 {p_.trim_pad_tail_ms}ms"
          f"（句首留得多是因为弱起音容易不过门，实测见 voice_vad.TRIM_PAD_HEAD_MS）")
    print(f"电平与增益 : 增益 {L.gain_from_env():.2f}×（{L.GAIN_ENV}）"
          f" —— 量一遍用 `capsplus-voice meter`，要**边看边调**用 `meter --live`")
    from capsplus import voice_glossary as G
    print(f"术语替换表 : {G.describe()}（整词匹配、改完即止；=off 关掉）")
    # 还在录就把当前读数摆出来 —— 这是"停顿多久会被判停"那个问题的现场答案
    live = frame.get("live") or ""
    if frame.get("state") == "recording" and live:
        print(f"录音中读数 : {live}")
    notes = V.env_notes()
    if notes:
        print(f"  环境变量覆盖: {' '.join(notes)}")
    return 0


def meter_source(seconds: float, params: V.VadParams) -> tuple[bytes, str]:
    """`meter` 的音频从哪来 →（裸 PCM，来源说明）。

    默认真读麦克风（**不经过录音会话**：不建 `Recorder`、不碰 socket、不弹窗、不送识别）。
    `CAPSPLUS_VOICE_METER_PCM=<文件>` 改成读文件：CI 没有麦克风也要能跑同一套统计，
    另外拿 `CAPSPLUS_VOICE_KEEP_LAST=1` 留下的 wav 复盘时也走这条路。
    """
    import subprocess

    path = os.environ.get("CAPSPLUS_VOICE_METER_PCM")
    if path:
        return ASR.read_audio_file(path), f"文件 {path}"
    from capsplus.voice_recorder import ARECORD, capture_argv
    import shutil
    if shutil.which(ARECORD) is None:
        raise FileNotFoundError(f"PATH 里没有 {ARECORD}：sudo apt install alsa-utils")
    argv = capture_argv(seconds, params)
    proc = subprocess.run(argv, capture_output=True, timeout=seconds + 10)
    if not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200] or f"rc={proc.returncode}"
        raise OSError(f"没采到数据（{detail}）："
                      f"`pactl list short sources` 看默认源在不在，或 CAPSPLUS_VOICE_DEVICE 指定一个")
    return proc.stdout, f"麦克风（{argv[0]}，{seconds:g}s）"


def cmd_meter(seconds: float = 3.0) -> int:
    """`capsplus-voice meter [秒数]`：只量电平，不动会话、不写 socket、不弹窗。

    存在的理由（2026-10-20）："识别率低"这句话可以指三件完全不同的事 ——
    ① 说话太轻（语音段 RMS 低）；② 底噪太高（门限被抬得很高，弱起音过不去）；
    ③ 模型/音频格式问题。没有这个命令，三种只能靠猜；有了它，用户对着麦克风说一句就知道是哪一种。
    """
    params = V.params_from_env()
    try:
        pcm, source = meter_source(seconds, params)
    except (OSError, ValueError) as exc:
        print(f"meter: {exc}", file=sys.stderr)
        return 1
    levels = L.measure(pcm, params)
    print(f"麦克风电平（{source}，只读：不动会话、不写 socket、不弹窗）")
    for line in L.report_lines(levels, params):
        print(line)
    print(f"  对照     : 本机真人语音基准 RMS≈{L.TARGET_VOICED_RMS:.0f}、"
          f"底噪 RMS≈11~19（2026-10-20 实测）")
    return 0


LIVE_MIN_S = 1.0                # `meter --live` 的时长上下限
LIVE_MAX_S = 60.0
LIVE_REFRESH_MS = 100           # 同一行刷新的节拍：比 30ms 帧粗，够跟手又不刷屏
LIVE_BAR_WIDTH = 20


def meter_live_line(elapsed_ms: int, rms: float, peak: int, gate: float,
                    width: int = LIVE_BAR_WIDTH) -> str:
    """`meter --live` 那一行（纯函数：CI 没有麦克风也能把渲染钉住）。

    条子按**当前门限**标定，`gate` 那一格正好是半程 —— 满格的判读与 VAD 的门限是同一个数，
    不会出现"条子满了但 VAD 说没说话"这种自相矛盾（门限的第二份真相本仓付过两次代价）。
    """
    from capsplus import voice_osd as OSD
    if gate > 0:
        filled = int(round(width * min(1.0, rms / (2 * gate))))
    else:
        filled = 0
    bar = "█" * filled + "·" * (width - filled)
    return (f"{elapsed_ms / 1000.0:5.1f}s  RMS {rms:7.0f}  峰 {peak:6d}  "
            f"[{bar}]  {OSD.level_verdict(rms, gate)}")


def run_meter_live(reader, seconds: float, params: V.VadParams, out=None,
                   refresh_ms: int = LIVE_REFRESH_MS) -> bytes:
    """边读边刷那一行。`reader` 是个 `read(n)` 回来的字节流（真机是 arecord 的 stdout，测试给 BytesIO）。

    返回采到的全部 PCM —— 收尾时可以直接喂 `voice_levels.measure()` 出那份"事后统计"，
    于是 `meter` 与 `meter --live` 的结论必然一致（同一段字节、同一个测量函数）。
    """
    out = out or sys.stdout
    frame_bytes = params.frame_bytes
    total = max(1, int(round(seconds * params.sample_rate)) * params.sample_width)
    collected = bytearray()
    frames: list[float] = []
    next_paint = 0
    peak = 0
    while len(collected) < total:
        try:
            # 只读到够用为止：多读一帧会让"采了 N 秒"变成 N+0.03 秒，
            # 而这条命令的用途正是"量一段固定时长的电平"（判据钉着这条，见 test_voice_progress）。
            chunk = reader.read(min(frame_bytes, total - len(collected)))
        except (OSError, ValueError):
            break
        if not chunk:
            break
        collected += chunk
        frames.append(V.frame_rms(chunk, params))
        n = len(chunk) // params.sample_width
        if n:
            peak = max(peak, max(abs(v) for v in
                                 struct.unpack("<%dh" % n, chunk[:n * params.sample_width])))
        elapsed_ms = len(frames) * params.frame_ms
        if elapsed_ms >= next_paint:
            next_paint = elapsed_ms + max(params.frame_ms, refresh_ms)
            gate = V.energy_gate(V.percentile(frames[:params.calib_frames],
                                              params.noise_percentile), params)
            out.write("\r" + meter_live_line(elapsed_ms, frames[-1], peak, gate))
            out.flush()
    out.write("\n")
    return bytes(collected)


def cmd_meter_live(seconds: float = 10.0) -> int:
    """`capsplus-voice meter --live [秒数]`：把 RMS/峰值打在**同一行**上，跟着说话变。

    与 `meter` 的分工：那个是"采完给一份统计"，这个是"看着它动" ——
    调麦克风增益、判断"到底是我没说话还是它没听见"时，一份事后统计不如一条会动的条子。
    采样口径与 `meter` **完全相同**（同一条 arecord 命令、同一个 `voice_levels.measure`），
    所以两者对同一段声音给出的结论必须一致。
    """
    import shutil
    import subprocess

    from capsplus.voice_recorder import ARECORD, arecord_argv
    params = V.params_from_env()
    if shutil.which(ARECORD) is None:
        print(f"meter: PATH 里没有 {ARECORD}：sudo apt install alsa-utils", file=sys.stderr)
        return 1
    print(f"实时电平（{seconds:g}s，Ctrl+C 可中断；条子半程 = 当前门限）")
    proc = subprocess.Popen(arecord_argv(params), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        pcm = run_meter_live(proc.stdout, seconds, params)
    except KeyboardInterrupt:
        pcm = b""
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:                                # noqa: BLE001
            proc.kill()
    if not pcm:
        detail = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:200]
        print(f"meter: 没采到数据（{detail or '空流'}）："
              f"`pactl list short sources` 看默认源在不在，或 CAPSPLUS_VOICE_DEVICE 指定一个",
              file=sys.stderr)
        return 1
    levels = L.measure(pcm, params)
    print(f"—— 这一段的小结（与 `meter` 用同一个测量函数）——")
    for line in L.report_lines(levels, params):
        print(line)
    return 0


def meter_args(argv: list[str]) -> tuple[bool, float]:
    """`meter [秒数]` / `meter --live [秒数]` →（是不是实时模式，秒数）。

    参数不合法就**报错退出**而不是偷偷按默认值跑 —— 一个打错的参数不该看起来像量成功。
    """
    rest = list(argv[1:])
    live = False
    for flag in ("--live", "-l"):
        if flag in rest:
            live = True
            rest.remove(flag)
    if len(rest) > 1:
        raise ValueError(f"meter 只收一个秒数，收到 {rest!r}")
    raw = rest[0] if rest else ""
    lo, hi = (LIVE_MIN_S, LIVE_MAX_S) if live else (1.0, 30.0)
    default = 10.0 if live else 3.0
    if not raw:
        return live, default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"秒数要是个数字，收到 {raw!r}")
    if not lo <= value <= hi:
        raise ValueError(f"秒数要在 {lo:g}~{hi:g} 之间（收到 {value:g}）")
    return live, value


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
    if cmd == "meter":
        try:
            live, seconds = meter_args(argv)
            return cmd_meter_live(seconds) if live else cmd_meter(seconds)
        except ValueError as exc:
            print(f"meter: {exc}", file=sys.stderr)
            return 2
    print(f"用法：capsplus-voice toggle|start|stop|status|"
          f"meter [--live] [秒数]（serve 是内部用的）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
