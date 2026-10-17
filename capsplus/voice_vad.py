# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""能量门 VAD（纯函数 + 纯状态机，**不碰 gi、不碰麦克风**）。

为什么这条判据值钱（2026-10-17 本机实测）：房间底噪很轻 —— 3 秒底噪的 RMS≈19
（int16 满值 32767，峰值才 108），而 SenseVoice **会把 3 秒底噪识别成「嗯」**。
也就是说：只要能量门形同虚设，用户没说话也会得到一句凭空冒出来的字。
所以 `tests/test_voice_vad.py` 里钉死了两条实质判据：
  ① 喂 3 秒 RMS≈19 的底噪 → 必须判**无语音**；
  ② 喂真人语音（sherpa-onnx 自带的 `test_wavs/zh.wav` 的 PCM）→ 必须判**有语音**。
两条都是拿**真实音频字节**跑的，不是拿假想的正弦波糊弄。

参数一律集中在 `VadParams`（默认值就是本机实测调出来的），调用方别自己抄数字。
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

# ── 默认参数（改这里就够了，全模块没有第二处写数字的地方）─────────────────────
FRAME_MS = 30                  # 一帧 30ms：够短（端点判得不迟），够长（RMS 稳）
SAMPLE_RATE = 16000            # 与识别服务约定一致（16k 单声道 s16le）
SAMPLE_WIDTH = 2               # int16
CHANNELS = 1
NOISE_WINDOW_MS = 300          # 用开头 300ms 估底噪
NOISE_PERCENTILE = 0.25        # 开头窗口取 25 分位而不是均值：用户张口就说时，窗口里混进语音
GATE_FLOOR = 150.0             # 硬下限：底噪再低也不许把门降到 150 以下
GATE_MULTIPLE = 4.0            # 门 = 4 × 底噪 RMS
GATE_CEILING = 1000.0          # 硬上限：见下面 `energy_gate()` 的实测理由
SILENCE_STOP_MS = 700          # 尾静音 700ms 判停
MAX_MS = 20000                 # 一次最多录 20s
MIN_VOICED_MS = 200            # 有声总时长不足 200ms = 当"没说话"丢掉
TRIM_PAD_MS = 150              # 交给 ASR 前，语音段前后各留一点（辅音头尾会被切掉）


@dataclass(frozen=True)
class VadParams:
    frame_ms: int = FRAME_MS
    sample_rate: int = SAMPLE_RATE
    sample_width: int = SAMPLE_WIDTH
    channels: int = CHANNELS
    noise_window_ms: int = NOISE_WINDOW_MS
    noise_percentile: float = NOISE_PERCENTILE
    gate_floor: float = GATE_FLOOR
    gate_multiple: float = GATE_MULTIPLE
    gate_ceiling: float = GATE_CEILING
    silence_stop_ms: int = SILENCE_STOP_MS
    max_ms: int = MAX_MS
    min_voiced_ms: int = MIN_VOICED_MS
    trim_pad_ms: int = TRIM_PAD_MS

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.sample_width * self.channels

    @property
    def calib_frames(self) -> int:
        return max(1, int(self.noise_window_ms / self.frame_ms))

    @property
    def silence_stop_frames(self) -> int:
        return max(1, int(round(self.silence_stop_ms / self.frame_ms)))

    @property
    def max_frames(self) -> int:
        return max(1, int(round(self.max_ms / self.frame_ms)))

    @property
    def min_voiced_frames(self) -> int:
        return max(1, int(round(self.min_voiced_ms / self.frame_ms)))

    @property
    def trim_pad_frames(self) -> int:
        return max(0, int(round(self.trim_pad_ms / self.frame_ms)))


# 环境变量 → 字段。真机上不用改代码就能调门限（`capsplus-voice status` 会把生效值打出来）。
ENV_KEYS = {
    "CAPSPLUS_VOICE_MAX_MS": ("max_ms", int),
    "CAPSPLUS_VOICE_SILENCE_MS": ("silence_stop_ms", int),
    "CAPSPLUS_VOICE_GATE_FLOOR": ("gate_floor", float),
    "CAPSPLUS_VOICE_GATE_MULT": ("gate_multiple", float),
    "CAPSPLUS_VOICE_GATE_CEIL": ("gate_ceiling", float),
    "CAPSPLUS_VOICE_NOISE_MS": ("noise_window_ms", int),
    "CAPSPLUS_VOICE_MIN_VOICED_MS": ("min_voiced_ms", int),
}


def params_from_env(base: VadParams | None = None) -> VadParams:
    """按 `ENV_KEYS` 覆盖默认参数；值不合法（非数字）就**忽略它**而不是崩。

    为什么不抛：这条路是现场调参用的，一个打错的数字不该让语音输入整个失灵。
    """
    from dataclasses import replace

    params = base or VadParams()
    for name, (field_name, caster) in ENV_KEYS.items():
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            params = replace(params, **{field_name: caster(raw)})
        except (TypeError, ValueError):
            continue
    return params


def env_notes() -> list[str]:
    """当前生效的覆盖（给 status 打印用，让人看见自己到底改了什么）。"""
    return [f"{name}={os.environ[name]}" for name in ENV_KEYS if os.environ.get(name)]


def frame_rms(chunk: bytes, params: VadParams = VadParams()) -> float:
    """一帧 int16 PCM 的 RMS（满值 32767）。半帧/空帧返回 0.0，不抛。

    小端解包写死 `<h`：`arecord` 给的 s16le 与 `wave` 里的 PCM 都是小端，
    用 `array('h')` 会跟着机器字节序走，换台大端机就悄悄算错了。
    """
    n = len(chunk) // params.sample_width
    if n <= 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, chunk[:n * params.sample_width])
    return (sum(v * v for v in samples) / n) ** 0.5


def percentile(values: list[float], q: float) -> float:
    """线性索引取分位（纯函数，不引 statistics 的量化口径 —— 要的就是可手算复现）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    low = int(idx)
    high = min(low + 1, len(ordered) - 1)
    frac = idx - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def energy_gate(noise_rms: float, params: VadParams = VadParams()) -> float:
    """能量门 = `max(下限, min(上限, 倍数 × 底噪 RMS))`。

    两个边界各管一件事，**顺序不许反**：上限只夹"跟着底噪爬"的那一项，
    下限是硬地板 —— 反过来写会让"把下限调到 5000"这种更严的配置失效（单测抓到过）。

    下限（150）挡住"底噪太轻 → 门形同虚设"：实测底噪 RMS≈19，`4×19=76` 连门槛都摸不到，
    没有下限就会把整段房间噪声当说话送去识别，而服务会把 3 秒底噪识别成一个字。

    上限（1000）挡住**另一头**的失真，这条是单测里抓出来的（2026-10-17）：
    按键之后**立刻**开口时，"前 300ms 底噪"里装的全是人声，门被抬到语音自己的 4 倍
    —— 实测一段平稳 4200 RMS 的音频会被自己抬高的门在 960ms 处判成"说完"、截成碎片。
    底噪与语音之间有三个数量级的余量（19 vs 数千），把门封顶在 1000 两头都照顾到。
    """
    adaptive = min(params.gate_ceiling, params.gate_multiple * max(0.0, noise_rms))
    return max(params.gate_floor, adaptive)


@dataclass
class VadResult:
    """一次判定的结论。`has_speech=False` 时看 `drop_reason`，为 True 时看 `stop_reason`。"""
    has_speech: bool = False
    stop_reason: str = ""          # endpoint / max_duration / user_stop / source_end / ""
    drop_reason: str = ""          # no_audio / no_speech / too_short / ""
    total_ms: int = 0
    voiced_ms: int = 0
    speech_start_ms: int = -1
    speech_end_ms: int = -1
    noise_rms: float = 0.0
    gate: float = 0.0
    frames: int = 0
    rms_trace: list[float] = field(default_factory=list)

    def summary(self) -> str:
        """给人看的一行（进日志用；不含任何语音内容）。"""
        return (f"speech={self.has_speech} stop={self.stop_reason or '-'} "
                f"drop={self.drop_reason or '-'} voiced_ms={self.voiced_ms} "
                f"total_ms={self.total_ms} noise_rms={self.noise_rms:.0f} gate={self.gate:.0f}")


class Vad:
    """流式判定：一帧一帧喂，返回事件名。状态转移只有四个出口，便于录音器挂回调。

    事件（`push()` 的返回值）：
      ""             还在录，没到边界
      "speech"       刚跨过能量门（第一帧有声）
      "endpoint"     有声之后静音连续够 `silence_stop_ms` → 该停了
      "max_duration" 录到 `max_ms` → 硬停
    """

    def __init__(self, params: VadParams = VadParams()):
        self.p = params
        self._buf = bytearray()
        self.rms: list[float] = []
        self.frames = 0
        self.voiced_frames = 0
        self.silence_run = 0
        self.speech_start = -1            # 帧号
        self.speech_end = -1              # 帧号（最后一个有声帧）
        self.noise_rms = 0.0
        self.gate = params.gate_floor     # 校准窗口没满之前只用硬下限
        self.stop_reason = ""
        self.ended = False

    # -- 内部 ---------------------------------------------------------------
    def _calibrate(self) -> None:
        if self.frames >= self.p.calib_frames:
            self.noise_rms = percentile(self.rms[:self.p.calib_frames], self.p.noise_percentile)
            self.gate = energy_gate(self.noise_rms, self.p)

    def _feed_frame(self, frame: bytes) -> str:
        rms = frame_rms(frame, self.p)
        self.rms.append(rms)
        self.frames += 1
        self._calibrate()
        voiced = rms > self.gate
        event = ""
        if voiced:
            if self.speech_start < 0:
                self.speech_start = self.frames - 1
                event = "speech"
            self.speech_end = self.frames - 1
            self.voiced_frames += 1
            self.silence_run = 0
        elif self.speech_start >= 0:
            self.silence_run += 1
            if self.silence_run >= self.p.silence_stop_frames:
                event = "endpoint"
        if event != "endpoint" and self.frames >= self.p.max_frames:
            event = "max_duration"
        return event

    # -- 对外 ---------------------------------------------------------------
    def push(self, chunk: bytes) -> str:
        """喂任意长度的 PCM（内部按帧切）；返回**本批里最靠后的那个事件**。已结束就只攒字节。"""
        if not chunk:
            return ""
        self._buf += chunk
        event = ""
        size = self.p.frame_bytes
        while len(self._buf) >= size:
            frame = bytes(self._buf[:size])
            del self._buf[:size]
            if self.ended:
                continue                       # 已经判停：只攒着，别把"停"之后的帧再算进结论
            got = self._feed_frame(frame)
            if got:
                event = got
                if got in ("endpoint", "max_duration"):
                    self.stop_reason = got
                    self.ended = True
        return event

    def stop(self, reason: str = "user_stop") -> VadResult:
        """外部提前收尾（用户又按了一次键 / arecord 断了）。已经判停过就不覆盖原因。"""
        if not self.ended:
            self.stop_reason = reason
            self.ended = True
        return self.result()

    def flush(self) -> VadResult:
        """数据喂完了（录音结束）：把不满一帧的尾巴补零算完，再出结论。"""
        size = self.p.frame_bytes
        if self._buf and not self.ended:
            tail = bytes(self._buf) + b"\x00" * (size - len(self._buf))
            self._buf = bytearray()
            got = self._feed_frame(tail)
            if got in ("endpoint", "max_duration"):
                self.stop_reason = got
                self.ended = True
        elif self._buf:
            self._buf = bytearray()
        if not self.ended and not self.stop_reason:
            self.stop_reason = "source_end"
            self.ended = True
        return self.result()

    def result(self) -> VadResult:
        total_ms = int(self.frames * self.p.frame_ms)
        voiced_ms = int(self.voiced_frames * self.p.frame_ms)
        start_ms = int(self.speech_start * self.p.frame_ms) if self.speech_start >= 0 else -1
        end_ms = int((self.speech_end + 1) * self.p.frame_ms) if self.speech_end >= 0 else -1
        if self.frames == 0:
            has_speech, drop = False, "no_audio"
        elif self.speech_start < 0:
            has_speech, drop = False, "no_speech"
        elif voiced_ms < self.p.min_voiced_ms:
            has_speech, drop = False, "too_short"
        else:
            has_speech, drop = True, ""
        return VadResult(has_speech=has_speech, stop_reason=self.stop_reason,
                         drop_reason=drop, total_ms=total_ms, voiced_ms=voiced_ms,
                         speech_start_ms=start_ms, speech_end_ms=end_ms,
                         noise_rms=round(self.noise_rms, 1), gate=round(self.gate, 1),
                         frames=self.frames, rms_trace=list(self.rms))

    # 语音段（含 pad）的帧区间，给 `trim_pcm` 用
    def speech_span_frames(self) -> tuple[int, int] | None:
        if self.speech_start < 0 or self.speech_end < 0:
            return None
        pad = self.p.trim_pad_frames
        return (max(0, self.speech_start - pad),
                min(self.frames, self.speech_end + 1 + pad))


def trim_pcm(pcm: bytes, span: tuple[int, int] | None,
             params: VadParams = VadParams()) -> bytes:
    """按帧区间切 PCM。`span=None` 原样返回（没判出语音段就别乱切）。"""
    if span is None:
        return pcm
    size = params.frame_bytes
    lo = max(0, min(span[0], len(pcm) // size))
    hi = max(lo, min(span[1], len(pcm) // size))
    return pcm[lo * size:hi * size]


def analyze(pcm: bytes, params: VadParams = VadParams()) -> VadResult:
    """一次性判定（`Vad` 的薄封装 —— 判定逻辑只有一份，别在这儿写第二套）。"""
    vad = Vad(params)
    vad.push(pcm)
    return vad.flush()
