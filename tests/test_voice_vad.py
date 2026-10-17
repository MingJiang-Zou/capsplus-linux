# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""能量门 VAD 的单测 —— 核心是**两条拿真音频跑的实质判据**。

为什么这两条是本功能的第一判据（2026-10-17 本机实测）：

  * 房间底噪很轻：`arecord` 采 3 秒，整段 RMS≈19、峰值 108（int16 满值 32767）；
  * 而 voice-svc 会把这 3 秒底噪**识别成一个字**（实测：真服务回 `{"ok":true,"text":"我"}`；
    任务书里那次是「嗯」）。也就是说 —— **只要能量门松一次，用户就会得到一句凭空冒出来的字**，
    而且它比"没反应"更难被发现。

所以：
  ① 3 秒 RMS≈19 的底噪 → 必须判**无语音**（`TestMeasuredNoiseIsRejected`）；
  ② 真人语音（sherpa-onnx 自带 `test_wavs/zh.wav` 的 PCM）→ 必须判**有语音**
     （`TestRealHumanSpeechIsAccepted`；CI 上没有那个文件时退回合成语音形状，
     并显式说明那不是真人录音）。

反例具型在 `TestGuardHasTeeth`：把门限下限降到 0，同一段底噪**必须**被判成有语音 ——
证明①真的在靠这条门限红，而不是碰巧。
"""

import math
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_vad as V            # noqa: E402

# 本机实测的真人语音夹具（sherpa-onnx 模型自带）。**不在仓里**，也不写死家目录。
REAL_SPEECH_WAV = (Path.home() / ".cache/sherpa-onnx"
                   / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
                   / "test_wavs" / "zh.wav")
SILENCE_STOP_MS = 700
GATE_FLOOR = 150.0
GATE_CEILING = 1000.0


def gate_of(result) -> float:
    return float(result.gate)


def rms_of(pcm: bytes) -> float:
    n = len(pcm) // 2
    vals = struct.unpack("<%dh" % n, pcm[:n * 2])
    return (sum(v * v for v in vals) / max(n, 1)) ** 0.5


def room_noise_pcm(ms: int = 3000, seed: int = 20261017) -> bytes:
    """复刻本机实测的底噪：高斯、RMS≈19、峰值≤108（不是白噪声的理想化版本，是按实测的形状）。"""
    import random

    rng = random.Random(seed)
    n = int(V.SAMPLE_RATE * ms / 1000)
    samples = []
    for _ in range(n):
        v = int(round(rng.gauss(0.0, 19.0)))
        samples.append(max(-108, min(108, v)))
    return struct.pack("<%dh" % n, *samples)


def voiced(ms: int, level: int = 6000, freq: int = 220) -> bytes:
    """带正弦包络的"有声"段。`level` 取 6000 ≈ 真人讲话量级的 RMS。"""
    n = int(V.SAMPLE_RATE * ms / 1000)
    samples = [int(level * math.sin(2 * math.pi * freq * i / V.SAMPLE_RATE)) for i in range(n)]
    return struct.pack("<%dh" % n, *samples)


def silence(ms: int, level: int = 15) -> bytes:
    n = int(V.SAMPLE_RATE * ms / 1000)
    return struct.pack("<%dh" % n, *([level] * n))


def real_speech_pcm() -> bytes:
    """从 wav 里取裸 PCM（跳过 44 字节头；用 `wave` 读，不认头长写死）。"""
    import wave

    with wave.open(str(REAL_SPEECH_WAV), "rb") as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000 and w.getsampwidth() == 2, \
            f"夹具格式变了：{w.getnchannels()}ch/{w.getframerate()}Hz/{w.getsampwidth()}B"
        return w.readframes(w.getnframes())


class TestFixtureIsTheMeasuredThing(unittest.TestCase):
    """先锁夹具本身：夹具不对，后面两条判据就都是空的。"""

    def test_noise_fixture_matches_measured_rms_and_peak(self):
        pcm = room_noise_pcm()
        self.assertAlmostEqual(rms_of(pcm), 19.0, delta=2.0,
                               msg="本机 3 秒底噪实测 RMS≈19，夹具必须在这个量级")
        n = len(pcm) // 2
        peak = max(abs(v) for v in struct.unpack("<%dh" % n, pcm))
        self.assertLessEqual(peak, 108, "实测峰值 108，夹具不许比现场响")
        self.assertEqual(len(pcm), V.SAMPLE_RATE * 2 * 3, "3 秒 s16le 单声道 = 96000 字节")

    def test_frame_geometry_is_what_arecord_gives(self):
        p = V.VadParams()
        self.assertEqual(p.frame_bytes, 960, "30ms @16k mono s16le = 960 字节")
        self.assertEqual(p.frame_samples, 480)
        self.assertEqual(p.calib_frames, 10, "300ms 校准窗 = 10 帧")
        self.assertEqual(p.silence_stop_frames, 23, "700ms / 30ms 取整 = 23 帧（≈690ms）")
        self.assertEqual(p.max_frames, 667, "20s / 30ms 取整 = 667 帧")
        self.assertEqual(p.min_voiced_frames, 7, "200ms / 30ms = 7 帧")


class TestMeasuredNoiseIsRejected(unittest.TestCase):
    """实质判据①：3 秒底噪必须判无语音 —— 否则用户会得到一句凭空冒出来的字。"""

    def test_three_seconds_of_room_noise_is_not_speech(self):
        result = V.analyze(room_noise_pcm())
        self.assertFalse(result.has_speech, result.summary())
        self.assertEqual(result.drop_reason, "no_speech")
        self.assertEqual(result.voiced_ms, 0, "底噪一帧都不许过门")
        self.assertLess(result.noise_rms, 40.0, result.summary())
        self.assertEqual(result.gate, GATE_FLOOR, "底噪太轻时门限就该由**下限**说话")

    def test_reject_happens_before_any_asr_would_be_needed(self):
        """判无语音时 `trim_pcm(span=None)` 不给东西：识别那步压根没料可送。"""
        pcm = room_noise_pcm()
        result = V.analyze(pcm)
        self.assertFalse(result.has_speech)
        self.assertEqual(V.trim_pcm(pcm, None, V.VadParams()), pcm,
                         "span=None 时不许把 PCM 清空 —— 是上层用 has_speech 决定不送，别靠切空")


class TestRealHumanSpeechIsAccepted(unittest.TestCase):
    """实质判据②：真人语音必须判有语音。"""

    @unittest.skipUnless(REAL_SPEECH_WAV.exists(),
                         f"本机没有真人语音夹具 {REAL_SPEECH_WAV}（CI 正常没有）")
    def test_sherpa_reference_zh_wav_is_speech(self):
        pcm = real_speech_pcm()
        result = V.analyze(pcm)
        self.assertTrue(result.has_speech, result.summary())
        self.assertEqual(result.drop_reason, "")
        self.assertGreaterEqual(result.voiced_ms, 1000,
                                f"5.6 秒的中文参考音频不该只判出 1 秒有声：{result.summary()}")
        self.assertGreaterEqual(result.gate, GATE_FLOOR)
        self.assertLessEqual(result.noise_rms, 60.0,
                             f"参考音频开头是安静段：{result.summary()}")

    def test_synthetic_voiced_burst_is_speech(self):
        """CI 也跑得动的版本：合成有声段 + 尾静音 → 判有语音且能自己收尾。"""
        pcm = silence(400) + voiced(900) + silence(1200)
        result = V.analyze(pcm)
        self.assertTrue(result.has_speech, result.summary())
        self.assertEqual(result.stop_reason, "endpoint", "尾静音 700ms 必须自己判停")
        self.assertGreaterEqual(result.voiced_ms, 600)
        self.assertLessEqual(result.total_ms, 400 + 900 + SILENCE_STOP_MS + 60)


class TestGateFormula(unittest.TestCase):
    def test_gate_is_clamped_between_floor_and_ceiling(self):
        p = V.VadParams()
        self.assertEqual(V.energy_gate(19.0, p), 150.0)        # 底噪太轻 → 由下限决定
        self.assertEqual(V.energy_gate(60.0, p), 240.0)        # 4 × 60
        self.assertEqual(V.energy_gate(250.0, p), 1000.0)      # 1000 到顶：开头有人说话也封顶
        self.assertEqual(V.energy_gate(4000.0, p), 1000.0,
                         "门被自己听到的语音抬到天上 = 张口就说会被截断（见上限那条回归）")
        self.assertEqual(V.energy_gate(-5.0, p), 150.0, "负数不该把门拉低")

    def test_gate_uses_only_the_head_window(self):
        """门限由**开头 300ms** 定，后面的响声不许再把门改动（越说门越高的失真）。"""
        pcm = silence(300, level=100) + voiced(2000)
        result = V.analyze(pcm)
        self.assertAlmostEqual(result.gate, min(GATE_CEILING, 4 * V.percentile(
            [V.frame_rms(pcm[i * 960:(i + 1) * 960]) for i in range(10)], 0.25)), delta=1.0)

    def test_percentile_matches_hand_computed_value(self):
        self.assertEqual(V.percentile([10, 20, 30, 40], 0.25), 17.5)
        self.assertEqual(V.percentile([], 0.5), 0.0)
        self.assertEqual(V.percentile([7], 0.25), 7.0)
        self.assertEqual(V.percentile([1, 2, 3], 0.0), 1)
        self.assertEqual(V.percentile([1, 2, 3], 1.0), 3)

    def test_half_frame_does_not_raise(self):
        self.assertEqual(V.frame_rms(b""), 0.0)
        self.assertEqual(V.frame_rms(b"\x01"), 0.0)


class TestStateTransitions(unittest.TestCase):
    def test_events_in_order(self):
        vad = V.Vad()
        seen = []
        for chunk in (silence(300), voiced(60), voiced(900), silence(800)):
            seen.append(vad.push(chunk))
        result = vad.flush()
        self.assertEqual(seen, ["", "speech", "", "endpoint"])
        self.assertEqual(result.stop_reason, "endpoint")
        self.assertTrue(result.has_speech)

    def test_tail_silence_stops_and_ignores_the_rest(self):
        """判停之后的帧不许再进结论（否则长静音会把 voiced_ms 算歪）。"""
        pcm = silence(300) + voiced(600) + silence(2000) + voiced(1000)
        result = V.analyze(pcm)
        self.assertEqual(result.stop_reason, "endpoint")
        self.assertLess(result.speech_end_ms, 1300,
                        f"判停后又把后面那段响声算进去了：{result.summary()}")

    def test_max_duration_is_a_hard_stop(self):
        # 幅度带轻微起伏（±8%），像真人讲话那样不是恒幅：门限由开头算，不能反过来掐死自己
        pcm = voiced(30000, level=6000) + voiced(100, level=5000)
        result = V.analyze(pcm)
        self.assertEqual(result.stop_reason, "max_duration", result.summary())
        self.assertEqual(result.frames, V.VadParams().max_frames)
        self.assertTrue(result.has_speech)

    def test_speaking_immediately_is_not_cut_into_a_fragment(self):
        """回归：按键后**张口就说**（开头 300ms 全是人声）不许被自己抬高的门截断。

        这条是写单测时抓出来的真失真：没封顶时一段平稳音频会在 960ms 处被判"说完"，
        只留下约 270ms 的碎片 —— 用户听到的是"我说了整句，屏上只上来半截"。
        """
        pcm = voiced(3000)                      # 没有开头静音段，一上来就响
        result = V.analyze(pcm)
        self.assertEqual(result.stop_reason, "source_end", result.summary())
        self.assertGreaterEqual(result.voiced_ms, 2900, result.summary())
        self.assertLessEqual(result.gate, V.VadParams().gate_ceiling)

    def test_external_stop_wins_over_source_end(self):
        vad = V.Vad()
        vad.push(silence(300) + voiced(400))
        result = vad.stop("user_stop")
        self.assertEqual(result.stop_reason, "user_stop")
        self.assertTrue(result.has_speech)

    def test_short_blip_is_dropped_as_too_short(self):
        result = V.analyze(silence(500) + voiced(120) + silence(1500))
        self.assertFalse(result.has_speech)
        self.assertEqual(result.drop_reason, "too_short")

    def test_empty_input_is_no_audio(self):
        result = V.analyze(b"")
        self.assertFalse(result.has_speech)
        self.assertEqual(result.drop_reason, "no_audio")
        self.assertEqual(result.frames, 0)

    def test_partial_tail_frame_is_counted(self):
        pcm = voiced(300)[:-7]                     # 故意留半个采样、不满一帧
        result = V.analyze(pcm)
        self.assertTrue(result.has_speech)
        self.assertGreaterEqual(result.frames, 9)


class TestStreamingEqualsBatch(unittest.TestCase):
    """录音器是一帧帧喂的，测试是整段喂的 —— 两条路必须给同一个结论。"""

    def _push_in_chunks(self, pcm: bytes, size: int) -> V.VadResult:
        vad = V.Vad()
        for i in range(0, len(pcm), size):
            vad.push(pcm[i:i + size])
        return vad.flush()

    def test_odd_chunk_sizes_give_same_verdict(self):
        pcm = silence(300) + voiced(800) + silence(1000)
        base = V.analyze(pcm)
        for size in (960, 700, 1024, 4096):
            with self.subTest(chunk=size):
                got = self._push_in_chunks(pcm, size)
                self.assertEqual(got.has_speech, base.has_speech)
                self.assertEqual(got.voiced_ms, base.voiced_ms)
                self.assertEqual(got.stop_reason, base.stop_reason)
                self.assertEqual(got.gate, base.gate)


class TestTrimPcm(unittest.TestCase):
    def test_span_and_padding_are_frame_aligned(self):
        params = V.VadParams()
        pcm = silence(500) + voiced(800) + silence(900)
        vad = V.Vad(params)
        vad.push(pcm)
        vad.flush()
        span = vad.speech_span_frames()
        self.assertIsNotNone(span)
        trimmed = V.trim_pcm(pcm, span, params)
        self.assertEqual(len(trimmed) % params.frame_bytes, 0)
        self.assertGreater(len(trimmed), vad.result().voiced_ms * 32)   # pad 让它比纯语音段长
        self.assertLess(len(trimmed), len(pcm))

    def test_trim_without_span_is_identity(self):
        pcm = voiced(200)
        self.assertEqual(V.trim_pcm(pcm, None), pcm)

    def test_span_is_clamped_to_whole_frames(self):
        """越界的区间夹到缓冲区末尾，且只按**整帧**切（半个采样不能进 WAV）。"""
        params = V.VadParams()
        pcm = voiced(200)
        trimmed = V.trim_pcm(pcm, (0, 10 ** 6), params)
        self.assertEqual(len(trimmed), (len(pcm) // params.frame_bytes) * params.frame_bytes)
        self.assertEqual(trimmed, pcm[:len(trimmed)])


class TestParamsFromEnv(unittest.TestCase):
    def test_env_overrides_and_notes(self):
        env = {"CAPSPLUS_VOICE_MAX_MS": "1234", "CAPSPLUS_VOICE_GATE_FLOOR": "300",
               "CAPSPLUS_VOICE_SILENCE_MS": "500"}
        with mock.patch.dict(os.environ, env, clear=False):
            p = V.params_from_env()
            self.assertEqual(p.max_ms, 1234)
            self.assertEqual(p.gate_floor, 300.0)
            self.assertEqual(p.silence_stop_ms, 500)
            self.assertEqual(p.frame_ms, V.FRAME_MS, "没覆盖的字段保持默认")
            notes = V.env_notes()
            self.assertEqual(len(notes), 3)
            self.assertTrue(all(n.startswith("CAPSPLUS_VOICE_") for n in notes))

    def test_garbage_value_is_ignored_not_fatal(self):
        """现场调参打错一个字符，不该让语音输入整个失灵。"""
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_MAX_MS": "二十秒"}):
            self.assertEqual(V.params_from_env().max_ms, V.MAX_MS)

    def test_stricter_gate_changes_the_verdict(self):
        """参数是**真**参与判定的：把地板抬到 5000，合成语音也得判没说话。

        顺带锁一条顺序语义：地板是硬下限，**不许被上限夹掉**
        （`max(地板, min(上限, 4×底噪))`，写反了这里就会绿不了）。
        """
        pcm = silence(300) + voiced(800, level=2000) + silence(900)
        self.assertTrue(V.analyze(pcm).has_speech)
        tight = V.VadParams(gate_floor=5000.0)
        self.assertEqual(V.energy_gate(19.0, tight), 5000.0)
        self.assertFalse(V.analyze(pcm, tight).has_speech)


class TestGuardHasTeeth(unittest.TestCase):
    """判据红过一次才算判据：这四条反例具型证明上面的断言都依赖真实机制。"""

    def test_zero_floor_lets_the_same_noise_through(self):
        """①的底噪在门限下限为 0 时**必须**变成"有语音"，否则①是摆设。"""
        loose = V.VadParams(gate_floor=0.0)
        result = V.analyze(room_noise_pcm(), loose)
        self.assertTrue(result.has_speech,
                        f"门限为 0 还能判没说话 = 能量门根本没参与判定：{result.summary()}")

    def test_zero_min_voiced_lets_a_single_click_through(self):
        loose = V.VadParams(min_voiced_ms=0)
        pcm = silence(500) + voiced(35) + silence(1500)
        self.assertFalse(V.analyze(pcm).has_speech, "默认参数下这声脆响该被丢")
        self.assertTrue(V.analyze(pcm, loose).has_speech)

    def test_huge_silence_threshold_never_endpoints(self):
        pcm = silence(300) + voiced(600) + silence(2000)
        self.assertEqual(V.analyze(pcm).stop_reason, "endpoint")
        stuck = V.VadParams(silence_stop_ms=2 ** 20)
        self.assertNotEqual(V.analyze(pcm, stuck).stop_reason, "endpoint")

    def test_the_ceiling_is_what_saves_the_immediate_speech(self):
        """反例具型：把上限取消（抬到无穷），同一段"张口就说"必须重新被截成碎片。"""
        no_ceiling = V.VadParams(gate_ceiling=float("inf"))
        pcm = voiced(3000)
        cut = V.analyze(pcm, no_ceiling)
        good = V.analyze(pcm)
        self.assertEqual(cut.stop_reason, "endpoint", "上限没了还自己判停 = 上限没起作用")
        self.assertLess(cut.voiced_ms, good.voiced_ms)
        self.assertEqual(gate_of(good), V.VadParams().gate_ceiling)

    def test_analyze_does_not_have_its_own_copy_of_the_logic(self):
        """`analyze()` 必须只是 `Vad` 的薄封装 —— 两处判定早晚会漂（本仓踩过两次）。"""
        import inspect

        src = inspect.getsource(V.analyze)
        self.assertIn("Vad(", src)
        for forbidden in ("frame_rms(", "energy_gate(", "percentile("):
            self.assertNotIn(forbidden, src, f"analyze() 里又写了一遍 {forbidden}")

    def test_noise_fixture_actually_has_energy(self):
        """夹具反例：如果底噪是纯 0，①就成了"检测静音"而不是"检测底噪"。"""
        pcm = room_noise_pcm()
        self.assertGreater(rms_of(pcm), 5.0, "夹具几乎是数字 0 ⇒ 判据①测的是空气")
        self.assertEqual(rms_of(b"\x00" * 96000), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
