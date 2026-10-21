# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""电平与增益：**只测量、只建议，不参与判定**（纯函数，不碰 gi、不碰麦克风）。

为什么单独一层（2026-10-20）：用户抱怨「识别率有点低」时，我们能拿出来的只有
`voice.log` 里那行 `noise_rms=… gate=…` —— 它说的是"底噪多少"，**没说"这次说话有多响"**。
于是"是不是麦克风太轻"这个问题从来没被量过，只能靠感觉。这一层补的就是那个量：
每次送识别前把语音段的 RMS / 峰值 / 分位记下来，另开一个 `capsplus-voice meter`
让用户随时能对着麦克风量一遍。

门限一律**复用** `voice_vad.energy_gate()`：这里再写一份公式就成了第二份真相，
早晚会和 VAD 实际用的门限漂开（本仓为这类漂移付过两次代价）。断言在
`tests/test_voice_levels.py::TestGuardHasTeeth::test_module_does_not_reimplement_the_gate_formula`（用 AST 扫本模块）。

增益为什么默认 **1.0（不加）**，以及上限定成 4×（2026-10-20 实测，别凭感觉改）：

  * 本机真语音电平**不低**：sherpa 参考 `test_wavs/zh.wav`（5.59s 中文句子）
    整段 RMS=4330、峰值 27611 —— 峰值已经是 int16 满量程的 **84%**，只剩 1.14× 余量。
    也就是说"语音太轻"在这个麦克风链路上不成立，**任何 ≥1.2× 的增益都会削顶**，
    而削顶引入的高次谐波正是识别率的杀手（这比"声音小"严重得多）。
  * 底噪侧：本机房间底噪 RMS≈11.5~19（实测两次：11.8 / 11.5，另有 2026-10-17 记的 19）。
    4× 之后 46~76，仍稳稳在门限硬下限 150 之下；8× 会到 92~152，**跨过 150**，
    底噪自己就能反复顶开门 —— 那时用户没说话也会出字（比没反应更难被发现）。
  * 所以：能自动做的只有"建议"，不能自动开。真有人在安静房间里轻声说话（RMS<500）
    才值得开，那时 `meter` 会把建议值打出来，照 `CAPSPLUS_VOICE_GAIN` 手工设。
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from . import voice_vad as V

# 一说话就该到的量级（RMS，int16 满值 32767）。取 3000 的理由：本机实测真人语音
# 帧 RMS 的中位数 3006（sherpa 参考音频 147 个过门帧的 p50），也就是"正常说话"的本机基准。
TARGET_VOICED_RMS = 3000.0
# 增益上限。两条夹出来的：① 削顶 —— 参考音频峰值已用掉 84% 量程；② 底噪被抬过门限下限
# （见模块 docstring 的算术）。真要超过 4× 的情况应该先换麦克风/调 PipeWire 音量，而不是在软件里放大。
MAX_GAIN = 4.0
# 低于这个 RMS 才算"说得很轻、值得开增益"：本机正常说话是 3000 量级，差一个数量级才叫轻。
QUIET_RMS = 500.0
GAIN_ENV = "CAPSPLUS_VOICE_GAIN"
CLIP_HEADROOM = 0.9             # 增益后峰值不许超过满量程的 90%
FULL_SCALE = 32767.0


@dataclass
class Levels:
    """一段 PCM 的电平画像（全是状态量，**不含任何内容**）。"""
    frames: int = 0
    rms: float = 0.0             # 整段 RMS
    peak: int = 0                # 整段峰值（绝对值）
    p10: float = 0.0
    p25: float = 0.0
    p50: float = 0.0
    p90: float = 0.0
    voiced_frames: int = 0
    voiced_rms: float = 0.0      # 过门那些帧的 RMS 中位数：这才是"说话有多响"
    noise_rms: float = 0.0       # 开头 300ms 的 25 分位（口径同 VAD）
    gate: float = 0.0            # VAD 本次实际用的门限
    gain: float = 1.0            # 已经/将要施加的增益

    def describe(self) -> str:
        """一行进日志 / status（形状照 `VadResult.summary()`：全 key=value，可 grep 可解析）。"""
        return (f"rms={self.rms:.0f} peak={self.peak} voiced_rms={self.voiced_rms:.0f} "
                f"gain={self.gain:.2f} frames={self.frames} gate={self.gate:.0f} "
                f"noise_rms={self.noise_rms:.0f}")


def measure(pcm: bytes, params: V.VadParams | None = None,
            vad: "V.Vad | None" = None) -> Levels:
    """量一段 PCM 的电平。传 `vad` 就沿用它的底噪与门限（录音链就是这么用的）。

    **电平按传进来的 `pcm` 现算，门限从 `vad` 继承**，两边不混：录音器拿到的是
    "已裁剪的语音段"，而门限必须来自"裁剪前那段音频的校准窗口"——拿裁剪后的开头 300ms
    去重估底噪会把门限算成"语音的 25 分位"，那就不是判定时用的那个门了。
    """
    p = params or (vad.p if vad is not None else V.VadParams())
    frame_bytes = p.frame_bytes
    frames = [V.frame_rms(pcm[i:i + frame_bytes], p)
              for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes)]
    if vad is None:
        vad = V.Vad(p)
        vad.push(pcm)
        vad.flush()
    if not pcm:
        return Levels(gate=round(vad.gate, 1), noise_rms=round(vad.noise_rms, 1))
    voiced = [value for value in frames if value > vad.gate]
    n = len(pcm) // p.sample_width
    peak = max((abs(v) for v in struct.unpack("<%dh" % n, pcm[:n * p.sample_width])), default=0)
    return Levels(
        frames=len(frames),
        rms=round(V.frame_rms(pcm, p), 1),
        peak=int(peak),
        p10=round(V.percentile(frames, 0.10), 1),
        p25=round(V.percentile(frames, 0.25), 1),
        p50=round(V.percentile(frames, 0.50), 1),
        p90=round(V.percentile(frames, 0.90), 1),
        voiced_frames=len(voiced),
        voiced_rms=round(V.percentile(voiced, 0.50), 1) if voiced else 0.0,
        noise_rms=round(vad.noise_rms, 1),
        gate=round(vad.gate, 1),
    )


def suggest_gain(levels: Levels) -> float:
    """建议增益（倍数，1.0 = 不用加）。**只建议，不自动套用**（理由见模块 docstring）。

    三个上限同时成立才取值，哪个先卡住听哪个：
      ① 目标电平：把语音段抬到 `TARGET_VOICED_RMS`；
      ② 削顶：抬完峰值不许过满量程 90%（本机实测峰值本来就到 84%，这条常常先卡住）；
      ③ `MAX_GAIN`：底噪不许被抬过能量门下限。
    """
    if levels.voiced_rms <= 0:
        return 1.0                       # 没量到语音：任何增益都是瞎猜
    want = TARGET_VOICED_RMS / levels.voiced_rms
    headroom = (FULL_SCALE * CLIP_HEADROOM) / max(1.0, float(levels.peak))
    gain = min(want, headroom, MAX_GAIN)
    if gain < 1.05:
        return 1.0                       # 差得还没到 5%：不值得为它调参
    return round(gain * 4) / 4           # 1/4 步进：现场手抄得下来，也不会为 1.28 vs 1.3 纠结


def gain_from_env(default: float = 1.0) -> float:
    """`CAPSPLUS_VOICE_GAIN=<倍数>`：手工增益，夹在 [1.0, MAX_GAIN]。

    不做自动增益（AGC）是**量过之后的决定**，不是偷懒：见模块 docstring。打错的值一律当没设
    —— 现场调参不该让语音输入整个失灵（同 `voice_vad.params_from_env` 的口径）。
    """
    raw = os.environ.get(GAIN_ENV)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 1.0:
        return 1.0
    return min(MAX_GAIN, value)


def apply_gain(pcm: bytes, gain: float) -> bytes:
    """int16 逐样本乘增益，**饱和夹到 ±32767**（宁可削顶也不许回绕成刺耳的噪声）。"""
    if not pcm or gain <= 1.0:
        return pcm
    n = len(pcm) // 2
    samples = struct.unpack("<%dh" % n, pcm[:n * 2])
    scaled = [max(-32767, min(32767, int(round(v * gain)))) for v in samples]
    return struct.pack("<%dh" % n, *scaled)


def report_lines(levels: Levels, params: V.VadParams | None = None) -> list[str]:
    """`capsplus-voice meter` 打印的那几行（人看的；数值全来自 `Levels`，没有第二处计算）。"""
    p = params or V.VadParams()
    suggest = suggest_gain(levels)
    lines = [
        f"  时长     : {levels.frames * p.frame_ms / 1000:.1f}s（{levels.frames} 帧，"
        f"{levels.frames * p.frame_ms}ms）",
        f"  RMS      : {levels.rms:.1f}    峰值: {levels.peak}（满量程 32767）",
        f"  分位     : p10={levels.p10:.0f} p25={levels.p25:.0f} p50={levels.p50:.0f} "
        f"p90={levels.p90:.0f}",
        f"  过门帧   : {levels.voiced_frames}/{levels.frames}，其中位数 RMS="
        f"{levels.voiced_rms:.0f}",
        f"  底噪     : {levels.noise_rms:.0f}（开头 {p.noise_window_ms}ms 的 "
        f"{int(p.noise_percentile * 100)} 分位）",
        f"  当前门限 : {levels.gate:.0f}（= max({p.gate_floor:.0f}, "
        f"{p.gate_multiple:.0f}×底噪)，上限 {p.gate_ceiling:.0f}）",
    ]
    if not levels.voiced_frames:
        lines.append("  建议增益 : 没量到过门的语音 —— 对着麦克风把要说的说完再跑一次")
        return lines
    if suggest > 1.0:
        lines.append(f"  建议增益 : {suggest:.2f}× —— 这句偏轻（语音 RMS {levels.voiced_rms:.0f} "
                     f"< 目标 {TARGET_VOICED_RMS:.0f}）。要开就 `{GAIN_ENV}={suggest:.2f}`")
    elif levels.voiced_rms and levels.voiced_rms < QUIET_RMS:
        lines.append(f"  建议增益 : 加不了（语音 RMS {levels.voiced_rms:.0f} 已低于 "
                     f"{QUIET_RMS:.0f}，但峰值 {levels.peak} 把余量吃光了）—— "
                     f"先查麦克风距离/`pactl get-source-volume`，软件放大只会削顶")
    else:
        lines.append(f"  建议增益 : 不用加（语音 RMS {levels.voiced_rms:.0f}，"
                     f"峰值 {levels.peak} 已用 {levels.peak / FULL_SCALE * 100:.0f}% 量程，"
                     f"加增益只会削顶）")
    return lines
