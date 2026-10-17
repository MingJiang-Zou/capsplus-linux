# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""录音：起 `arecord` 采 16k 单声道 s16le → 边采边喂 VAD → 回调式交出去。

**不碰 gi**（判据：`tests/test_voice_portability.py`），也不碰界面 —— 谁想显示什么就挂回调：
`on_state` / `on_result` / `on_error` 三个口子，`voice_worker.py` 就是这么把录音串到
识别与弹窗上的。这样切的原因：`KNOWN-LIMITS.md` 第 8 条已经写明"界面行为只能靠单测 + 人眼"，
所以采集这条链必须在**没有显示服务器**的机器上也能跑、也能测。

状态机只有四个状态，全在 `voice_proto.STATES` 里：

    idle → recording →（有声→静音 700ms / 录满 20s / 用户再按一次）→ decoding → review → idle

采集口径（本机 2026-10-17 实测，别改回别的写法）：

    arecord -f S16_LE -r 16000 -c 1        # 走 PipeWire 默认源，实测可用
    parecord 的同参数写法报「参数过多」⇒ 不用它
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass

from . import voice_vad as V
from .voice_proto import STATES, log_line

ARECORD = "arecord"
# 服务不在时的提示：与 voice_asr.HINTS 同一口径（状态必须带动作）
HINTS = {
    "no-arecord": "没装 arecord：`sudo apt install alsa-utils`（PipeWire 机器上它走 PipeWire 兼容层，"
                  "不需要额外配 ALSA）",
    "device-busy": "arecord 打不开默认源：`pactl list short sources` 看源在不在，"
                   "或给 CAPSPLUS_VOICE_DEVICE 指定一个具体的 source",
    "spawn-failed": "起不了 arecord：{detail}",
    "no-data": "录到 0 字节：麦克风没被系统授权，或者默认源是空设备 —— "
               "`pactl set-default-source <你的 Mic source>` 再试",
}

# 录音期最多再等这么多秒：VAD 的 20s 上限只按"收到的帧"计时，
# 管道一直不给数据（设备被占、权限没给）时靠这条兜底，别把线程吊死。
DEADLINE_GRACE_S = 3.0


@dataclass
class Recording:
    """一次录音的产物。`pcm` 已经过 VAD 裁剪；判不成语音时 `pcm` 是空串。

    `error` 非空 = 采集本身没成（没装 arecord / 设备打不开），与"录到了但没说话"是两回事，
    上层的提示文案要分开写，别把"没说话"报成"麦克风坏了"。
    """
    pcm: bytes = b""
    vad: V.VadResult | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.pcm) and self.vad is not None and self.vad.has_speech


def arecord_argv(params: V.VadParams = V.VadParams(), device: str | None = None) -> list[str]:
    """采集命令。`-t raw`：不要 WAV 头（头由 `voice_asr.pcm_to_wav` 自己拼，见那里的实测理由）。"""
    argv = [ARECORD, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(params.sample_rate),
            "-c", str(params.channels)]
    picked = device or os.environ.get("CAPSPLUS_VOICE_DEVICE")
    if picked:
        argv += ["-D", picked]
    argv.append("-")
    return argv


class Recorder:
    """一次一个录音会话（照 Qbar 的 worker 线程写法：线程只负责读管道，UI 靠回调）。

    回调签名：
      `on_state(state)`            state ∈ voice_proto.STATES
      `on_result(Recording)`       采完就调（含"没说话"的情况，`rec.ok` 为假）
      `on_error(code, message, hint)`
    """

    def __init__(self, params: V.VadParams | None = None, *, on_state=None,
                 on_result=None, on_error=None, popener=None, argv=None,
                 device: str | None = None):
        self.p = params or V.VadParams()
        self.on_state = on_state
        self.on_result = on_result
        self.on_error = on_error
        self._popener = popener or subprocess.Popen
        # 注入过 popener 就**不再查 arecord 在不在**：调用方自己决定跑什么（CI 没有 alsa-utils，
        # 单测全靠这个注入口跑真字节流的判定，见 tests/test_voice_proto.py 的整链路用例）。
        self._owns_popen = popener is None
        self._argv = argv or arecord_argv(self.p, device)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._proc = None
        self._stop_request = ""
        self._watchdog: threading.Timer | None = None
        self.state = "idle"
        self.pcm = bytearray()
        self.vad = V.Vad(self.p)

    # -- 对外 ---------------------------------------------------------------
    def is_recording(self) -> bool:
        return self.state == "recording"

    def start(self) -> bool:
        """开始录音。已经在录就返回 False（幂等，按键连点两次不会开出两个 arecord）。"""
        with self._lock:
            if self.state != "idle":
                return False
            self._stop_request = ""
            self.pcm = bytearray()
            self.vad = V.Vad(self.p)
            self._set_state("recording")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self, reason: str = "user_stop") -> None:
        """用户又按了一次键 / 上层要取消。真正的收尾在 `_run` 的退出路径里做。"""
        with self._lock:
            self._stop_request = reason
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def wait(self, timeout: float = 0.0) -> None:
        if self._thread is not None and timeout:
            self._thread.join(timeout)

    # -- 内部 ---------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"未知状态 {state!r}")
        self.state = state
        if self.on_state:
            try:
                self.on_state(state)
            except Exception as exc:                    # noqa: BLE001 回调崩了不许带崩录音
                log_line(f"on_state 回调异常：{type(exc).__name__}: {exc}")

    def _fail(self, code: str, message: str) -> None:
        """采集失败：报错误 + 把状态推回 idle + 给 on_result 一个空产物（上层只有一条出口）。"""
        self._emit_error(code, message)
        with self._lock:
            self._cancel_watchdog()
        self._deliver(Recording(pcm=b"", error=code))

    def _emit_error(self, code: str, message: str) -> None:
        hint = HINTS.get(code, "").format(detail=message) if code in HINTS else ""
        log_line(f"录音错误 {code}: {message[:120]}")   # 只记错误，不记内容
        if self.on_error:
            try:
                self.on_error(code, message, hint)
            except Exception as exc:                    # noqa: BLE001
                log_line(f"on_error 回调异常：{type(exc).__name__}: {exc}")

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _run(self) -> None:
        if self._owns_popen and shutil.which(ARECORD) is None and self._argv[0] == ARECORD:
            self._fail("no-arecord", f"PATH 里没有 {ARECORD}")
            return
        try:
            proc = self._popener(self._argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            self._fail("spawn-failed", f"{type(exc).__name__}: {exc}")
            return
        self._proc = proc
        errs: list[str] = []
        drainer = threading.Thread(target=self._drain_stderr, args=(proc, errs), daemon=True)
        drainer.start()
        # 看门狗：帧不来（设备被占/权限没给）时也得出结果
        self._watchdog = threading.Timer(self.p.max_ms / 1000.0 + DEADLINE_GRACE_S,
                                         lambda: self.stop("deadline"))
        self._watchdog.daemon = True
        self._watchdog.start()

        size = self.p.frame_bytes
        event = ""
        try:
            while True:
                with self._lock:
                    reason = self._stop_request
                if reason:
                    break
                try:
                    chunk = proc.stdout.read(size)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break                             # EOF：进程退了或被终止
                self.pcm += chunk
                event = self.vad.push(chunk)
                if event in ("endpoint", "max_duration"):
                    break
        finally:
            self._close_proc(proc, reason=self._stop_request or event)
            drainer.join(timeout=0.5)
            with self._lock:
                self._cancel_watchdog()

        pcm = bytes(self.pcm)
        if not pcm:
            why = " ".join(errs).strip() or f"arecord 退出码 {getattr(proc, 'returncode', None)}"
            self._fail("no-data", why)
            return
        result = self._finalize(event)
        self._deliver(Recording(pcm=self._trim(pcm, result), vad=result))

    def _drain_stderr(self, proc, sink: list[str]) -> None:
        try:
            data = proc.stderr.read() if proc.stderr else b""
        except (OSError, ValueError):
            return
        if data:
            sink.append(data.decode("utf-8", "replace").strip())

    def _close_proc(self, proc, reason: str) -> None:
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:                             # noqa: BLE001 没装 signal 分支也别卡住
            try:
                proc.kill()
            except OSError:
                pass
        log_line(f"采集结束（{reason}）")

    def _finalize(self, event: str) -> V.VadResult:
        if event in ("endpoint", "max_duration"):
            return self.vad.result()
        return self.vad.stop(self._stop_request or "source_end")

    def _trim(self, pcm: bytes, result: V.VadResult) -> bytes:
        if not result.has_speech:
            return b""
        return V.trim_pcm(pcm, self.vad.speech_span_frames(), self.p)

    def _deliver(self, rec: Recording) -> None:
        """回 idle 并交产物。回调里抛的异常只记日志 —— 录音链不许因为 UI 而断。"""
        self.state = "idle"
        if self.on_result:
            try:
                self.on_result(rec)
            except Exception as exc:                  # noqa: BLE001
                log_line(f"on_result 回调异常：{type(exc).__name__}: {exc}")
