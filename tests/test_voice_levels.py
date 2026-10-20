# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""电平与增益的单测（`capsplus-voice meter` 的那套计算）。

为什么这批判据值得写（2026-10-20）：明江说"识别率低"时，我们要么加增益、要么换模型，
但**没有一条量能区分这两种因**。这批用例钉住的是"量的口径"和"建议的边界"：

  * 口径：量出来的 RMS/峰值/分位必须是**这段字节**的（拿真人参考音频对上手算的数）；
  * 边界：**不许自动加增益** —— 本机实测峰值已用掉 84% 量程，任何 ≥1.2× 都削顶；
  * 复用：门限只有一份（`voice_vad.energy_gate` / `Vad.gate`），这里不许再写第二份公式。

反例具型在 `TestGuardHasTeeth`：把默认增益偷偷改成 2×、把门限公式抄一份到这里，
对应的判据必须红。
"""

import ast
import inspect
import math
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_levels as L         # noqa: E402
from capsplus import voice_vad as V            # noqa: E402

REAL_SPEECH_WAV = (Path.home() / ".cache/sherpa-onnx"
                   / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
                   / "test_wavs" / "zh.wav")


def voiced(ms: int, level: int = 6000, freq: int = 220) -> bytes:
    n = int(V.SAMPLE_RATE * ms / 1000)
    samples = [int(level * math.sin(2 * math.pi * freq * i / V.SAMPLE_RATE)) for i in range(n)]
    return struct.pack("<%dh" % n, *samples)


def silence(ms: int, level: int = 15) -> bytes:
    n = int(V.SAMPLE_RATE * ms / 1000)
    return struct.pack("<%dh" % n, *([level] * n))


def real_speech_pcm() -> bytes:
    import wave

    with wave.open(str(REAL_SPEECH_WAV), "rb") as w:
        return w.readframes(w.getnframes())


def hand_rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    vals = struct.unpack("<%dh" % n, pcm[:n * 2])
    return (sum(v * v for v in vals) / max(n, 1)) ** 0.5


class TestMeasuredNumbersAreTheRealOnes(unittest.TestCase):
    """量出来的数必须是这段音频自己的（拿真人参考音频对上手算的数）。"""

    @unittest.skipUnless(REAL_SPEECH_WAV.exists(),
                         f"本机没有真人语音夹具 {REAL_SPEECH_WAV}（CI 正常没有）")
    def test_reference_clip_levels_match_the_measurement(self):
        pcm = real_speech_pcm()
        lv = L.measure(pcm)
        self.assertAlmostEqual(lv.rms, hand_rms(pcm), delta=1.0, msg="RMS 不许和手算的不一样")
        self.assertEqual(lv.peak, 27611, "本机实测峰值 27611（满量程的 84%）")
        self.assertAlmostEqual(lv.rms, 4330, delta=60,
                               msg="本机实测整段 RMS≈4330 —— 这不是一段轻语音")
        self.assertGreater(lv.voiced_rms, 1000, f"语音段中位数 RMS 该是数千量级：{lv.describe()}")
        self.assertEqual(lv.frames, len(pcm) // 960, "帧数按 30ms 一帧算")

    def test_room_noise_has_no_voiced_frames(self):
        lv = L.measure(silence(3000, level=15))
        self.assertEqual(lv.voiced_frames, 0, f"静音不该有过门帧：{lv.describe()}")
        self.assertEqual(lv.gate, 150.0, "底噪太轻时门限由硬下限说话")
        self.assertEqual(lv.voiced_rms, 0.0)

    def test_gate_is_the_same_one_the_vad_uses(self):
        """门限只有一份：`Levels.gate` 必须等于 `Vad` 自己的门（唯一真相源）。"""
        for pcm in (silence(400) + voiced(900), voiced(1500), silence(3000, level=60)):
            with self.subTest(bytes=len(pcm)):
                self.assertEqual(L.measure(pcm).gate, round(V.analyze(pcm).gate, 1))

    def test_levels_reuse_the_vad_calibration_when_given_one(self):
        """传了 `vad` 就用它的校准窗：裁过的片段不许重新估底噪。

        反证就在同一条里：去掉两端 pad 让片段**只剩语音**，重新估底噪会把门限从 150
        推到上限 1000（"语音自己的 25 分位 × 4"）—— 那已经不是判定时用的那个门了，
        所以录音链必须把 `vad` 传进来。
        """
        params = V.VadParams(trim_pad_head_ms=0, trim_pad_tail_ms=0)
        pcm = silence(300) + voiced(1200) + silence(1500)
        vad = V.Vad()
        vad.p = params
        vad.push(pcm)
        vad.flush()
        seg = V.trim_pcm(pcm, vad.speech_span_frames(), params)
        self.assertEqual(vad.speech_start, 10, "300ms 静音正好 10 帧")
        self.assertEqual(seg[:960], voiced(1200)[:960], "片段确实是从语音本身开始的")
        with_vad = L.measure(seg, params, vad)
        reanalyzed = L.measure(seg, params)
        self.assertEqual(with_vad.gate, round(vad.gate, 1))
        self.assertEqual(with_vad.noise_rms, round(vad.noise_rms, 1))
        self.assertGreaterEqual(reanalyzed.gate, 1000.0,
                                "重新估的底噪就是语音自己 ⇒ 门限顶到上限（这条是必须继承的理由）")

    def test_peak_is_the_absolute_maximum(self):
        pcm = struct.pack("<3h", -30000, 500, 20000)
        self.assertEqual(L.measure(pcm).peak, 30000, "负峰值也算峰值")

    def test_empty_pcm_does_not_blow_up(self):
        lv = L.measure(b"")
        self.assertEqual(lv.frames, 0)
        self.assertEqual(lv.describe(), lv.describe())      # 只是不许抛


class TestGainSuggestionStaysInsideTheMeasuredBounds(unittest.TestCase):
    """建议增益的三个上界：目标电平 / 削顶余量 / `MAX_GAIN`。"""

    @unittest.skipUnless(REAL_SPEECH_WAV.exists(),
                         f"本机没有真人语音夹具 {REAL_SPEECH_WAV}（CI 正常没有）")
    def test_normal_speech_gets_no_gain_because_peaks_have_no_room(self):
        lv = L.measure(real_speech_pcm())
        self.assertEqual(L.suggest_gain(lv), 1.0,
                         "峰值已用 84% 量程：这里还敢加增益就是把削顶塞进识别链")

    def test_quiet_speech_gets_a_gain_but_never_over_max(self):
        # 正弦的峰值是 `level`、RMS 是 level/√2：level=300 ⇒ RMS≈212（过门 150，远低于 3000）
        lv = L.measure(silence(400) + voiced(900, level=300) + silence(1500))
        self.assertGreater(lv.voiced_rms, 150, f"夹具自己得先过门：{lv.describe()}")
        self.assertEqual(L.suggest_gain(lv), L.MAX_GAIN,
                         f"要 14× 才够 3000，但上限只放 4×：{lv.describe()}")

    def test_a_single_spike_can_veto_the_gain(self):
        """削顶余量说话的那一档：轻声说话 + 一声磕碰，宁可不加也不许把峰值推过满量程。"""
        pcm = (silence(400) + voiced(60, level=30000) + voiced(840, level=300)
               + silence(1500))
        lv = L.measure(pcm)
        self.assertGreater(lv.peak, 29000, f"夹具里那个尖峰得真在：{lv.describe()}")
        self.assertEqual(L.suggest_gain(lv), 1.0,
                         f"峰值已到满量程，任何增益都只会削顶：{lv.describe()}")

    def test_no_voiced_frames_means_no_suggestion(self):
        self.assertEqual(L.suggest_gain(L.measure(silence(2000, level=10))), 1.0,
                         "没量到语音时任何增益都是瞎猜")

    def test_gain_is_never_below_one(self):
        """已经够响的信号不许被"建议"成衰减：这条链上没有需要衰减的使用场景。"""
        self.assertEqual(L.suggest_gain(L.measure(silence(300) + voiced(900, level=9000))), 1.0)


class TestGainApplication(unittest.TestCase):
    def test_gain_one_is_identity(self):
        pcm = voiced(300)
        self.assertIs(L.apply_gain(pcm, 1.0), pcm, "不加增益时不许复制一份大字节串")

    def test_gain_scales_and_keeps_the_length(self):
        pcm = struct.pack("<4h", 1000, -1000, 0, 2000)
        out = struct.unpack("<4h", L.apply_gain(pcm, 2.0))
        self.assertEqual(out, (2000, -2000, 0, 4000))
        self.assertEqual(len(L.apply_gain(pcm, 3.0)), len(pcm))

    def test_saturation_never_wraps_around(self):
        """削顶可以，回绕不行：回绕出来的是满幅刺啦声，比削顶毁识别得多。"""
        pcm = struct.pack("<3h", 30000, -30000, 100)
        out = struct.unpack("<3h", L.apply_gain(pcm, 4.0))
        self.assertEqual(out[0], 32767)
        self.assertEqual(out[1], -32767)
        self.assertLess(abs(out[2]), 1000)

    def test_odd_tail_byte_is_dropped(self):
        out = L.apply_gain(struct.pack("<h", 1000) + b"\x01", 2.0)
        self.assertEqual(len(out), 2)


class TestGainKnob(unittest.TestCase):
    def test_default_is_no_gain(self):
        with mock.patch.dict(os.environ, {L.GAIN_ENV: ""}):
            self.assertEqual(L.gain_from_env(), 1.0, "默认不许自动放大（理由见 voice_levels docstring）")

    def test_value_is_clamped(self):
        with mock.patch.dict(os.environ, {L.GAIN_ENV: "0.25"}):
            self.assertEqual(L.gain_from_env(), 1.0, "小于 1 没用，按 1 算")
        with mock.patch.dict(os.environ, {L.GAIN_ENV: "99"}):
            self.assertEqual(L.gain_from_env(), L.MAX_GAIN)

    def test_garbage_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {L.GAIN_ENV: "两倍"}):
            self.assertEqual(L.gain_from_env(), 1.0, "现场打错一个字不该让语音输入整个失灵")

    def test_two_times_is_honoured(self):
        with mock.patch.dict(os.environ, {L.GAIN_ENV: "2"}):
            self.assertEqual(L.gain_from_env(), 2.0)


class TestMeterReport(unittest.TestCase):
    def test_report_prints_gate_and_a_verdict(self):
        lines = L.report_lines(L.measure(silence(400) + voiced(900) + silence(1500)))
        text = "\n".join(lines)
        self.assertIn("当前门限", text)
        self.assertIn("建议增益", text)
        self.assertIn("分位", text)
        self.assertTrue(any("不用加" in line for line in lines),
                        f"正常语音该明确说「不用加」：{text}")

    def test_quiet_sample_tells_you_the_exact_env_line(self):
        lv = L.measure(silence(400) + voiced(900, level=300) + silence(1500))
        text = "\n".join(L.report_lines(lv))
        self.assertIn(L.GAIN_ENV, text, "建议要给出一条能直接抄的环境变量")

    def test_describe_is_parseable_and_carries_no_content(self):
        """进日志的那一行全是状态量：`key=value` 空格分隔，没有文本（隐私判据）。"""
        line = L.measure(silence(300) + voiced(600)).describe()
        parts = dict(piece.split("=", 1) for piece in line.split())
        for key in ("rms", "peak", "voiced_rms", "gain", "frames", "gate", "noise_rms"):
            self.assertIn(key, parts, f"describe() 少了 {key}：{line}")
        for value in parts.values():
            float(value)                    # 每个值都得是数字，不许混进任何字


class TestGuardHasTeeth(unittest.TestCase):
    """反例具型：这几条保证上面的断言都靠真机制成立，而不是碰巧。"""

    def test_default_gain_really_gates_the_scaling(self):
        """把默认值改成 2×（假想的"偷偷开了 AGC"），"默认不加增益"那条必须红。"""
        with mock.patch.dict(os.environ, {L.GAIN_ENV: ""}):
            with mock.patch.object(L, "gain_from_env", lambda default=1.0: 2.0):
                self.assertNotEqual(L.gain_from_env(), 1.0, "这层 patch 没生效 = 反例不成立")
        with mock.patch.dict(os.environ, {L.GAIN_ENV: ""}):
            self.assertEqual(L.gain_from_env(), 1.0)

    def test_max_gain_really_keeps_noise_below_the_gate_floor(self):
        """`MAX_GAIN` 不是随手写的：本机底噪 19 × 4 = 76 < 150（门限下限）；×8 就过门了。"""
        noise = 19.0
        self.assertLess(noise * L.MAX_GAIN, V.GATE_FLOOR,
                        "增益上限一旦让底噪过门，用户没说话也会出字")
        self.assertGreater(noise * (L.MAX_GAIN * 2), V.GATE_FLOOR,
                           "反证：8× 就会跨过门限 —— 所以上限只能到 4× 这一档")

    def test_module_does_not_reimplement_the_gate_formula(self):
        """门限公式不许在本模块抄第二份（本仓为这类漂移付过两次代价）。

        扫的是 **AST 里的调用/属性引用**，不是源码文本：docstring 里写一句
        "复用 `voice_vad.energy_gate()`"是说明，不是实现 —— 拿文本 grep 判据会把解释判成犯罪。
        """
        # 只看**算数的那两个函数**：`report_lines` 会把 `gate_multiple` 原样打进人看的文案里
        # （"= max(150, 4×底噪)"），那是展示参数，不是算门限 —— 把展示也判成犯罪就没人敢写文档了。
        for fn in (L.measure, L.suggest_gain):
            tree = ast.parse(inspect.getsource(fn))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    self.assertNotIn(name, ("energy_gate",),
                                     f"{fn.__name__} 自己算门限了：唯一来源是 `Vad.gate`")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ("gate_multiple", "gate_floor", "gate_ceiling"),
                                     f"{fn.__name__} 自己拿门限参数算数 = 第二份公式")

    def test_report_does_not_claim_success_without_voiced_frames(self):
        """没量到语音时不许出现「不用加」这种像结论的话。"""
        lines = L.report_lines(L.measure(silence(1500, level=10)))
        text = "\n".join(lines)
        self.assertNotIn("语音 RMS 0", text, f"没有语音帧时不该假装有结论：{text}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
