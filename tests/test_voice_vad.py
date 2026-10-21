# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""能量门 VAD 的单测 —— 核心是**四条拿真音频跑的实质判据**。

为什么这些是本功能的第一判据（2026-10-17 本机实测）：

  * 房间底噪很轻：`arecord` 采 3 秒，整段 RMS≈19、峰值 108（int16 满值 32767）；
  * 而 voice-svc 会把这 3 秒底噪**识别成一个字**（实测：真服务回 `{"ok":true,"text":"我"}`；
    任务书里那次是「嗯」）。也就是说 —— **只要能量门松一次，用户就会得到一句凭空冒出来的字**，
    而且它比"没反应"更难被发现。

所以：
  ① 3 秒 RMS≈19 的底噪 → 必须判**无语音**（`TestMeasuredNoiseIsRejected`）；
  ② 真人语音（sherpa-onnx 自带 `test_wavs/zh.wav` 的 PCM）→ 必须判**有语音**
     （`TestRealHumanSpeechIsAccepted`；CI 上没有那个文件时退回合成语音形状，
      并显式说明那不是真人录音）。

2026-10-20 补的两条（用户：「语音转文字几乎不可用，只可以是短语音，且识别率有点低」）：
  ③ **一句话中间的 900ms 正常停顿不许把它切成两半** —— 尾静音门槛自适应
     （`TestSentenceIsNotCutAtEveryPause`）。旧值固定 700ms，正好卡在"换气"和"说完了"中间，
     真机日志里因此全是"采集 3s → 3 字"的半截货。
  ④ **句首弱起音不许被 trim 切掉** —— 句首 pad 150→300ms
     （`TestTrimPadKeepsTheFirstSyllable`）。同一段真人音频对真服务 A/B（8 种前置静音）：
     头 pad 150 → 丢首字 5/7，300 → 丢 0/7；400/500/800 一个都不多救，所以取 300。
  ⑤ 录音上限 20s→60s，且**超了不许静默**（`TestSixtySecondLimit`）。

反例具型全在 `TestGuardHasTeeth`：把旧参数塞回去（700ms / 150ms / 20000ms / 门限 0），
上面每一条都必须红 —— 没红过的断言不算判据。
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
SILENCE_STOP_MS = 1200          # 自适应起点（旧值 700，见文件头③）
SILENCE_STOP_MAX_MS = 2400
MAX_MS = 60000                  # 旧值 20000，见文件头⑤
TRIM_PAD_HEAD_MS = 300
TRIM_PAD_TAIL_MS = 250
GATE_FLOOR = 150.0
GATE_CEILING = 1000.0

# 旧口径的三个数，只在**反例具型**里用：它们必须能让新判据红，而不是还当默认值使
OLD_SILENCE_STOP_MS = 700
OLD_TRIM_PAD_MS = 150
OLD_MAX_MS = 20000


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
    """先锁夹具本身：夹具不对，后面几条判据就都是空的。"""

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
        self.assertEqual(p.silence_stop_frames, 40, "自适应起点 1200ms / 30ms = 40 帧")
        self.assertEqual(p.silence_stop_max_frames, 80, "上界 2400ms / 30ms = 80 帧")
        self.assertEqual(p.max_frames, 2000, "60s / 30ms = 2000 帧（旧值 667 帧 = 20s）")
        self.assertEqual(p.min_voiced_frames, 7, "200ms / 30ms = 7 帧")
        self.assertEqual(p.trim_pad_head_frames, 10, "句首 300ms = 10 帧")
        self.assertEqual(p.trim_pad_tail_frames, 8, "句尾 250ms = 8 帧（240ms，取整向下）")


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
        pcm = silence(400) + voiced(900) + silence(1500)
        result = V.analyze(pcm)
        self.assertTrue(result.has_speech, result.summary())
        self.assertEqual(result.stop_reason, "endpoint", "尾静音够长时必须自己判停")
        self.assertGreaterEqual(result.voiced_ms, 600)
        self.assertLessEqual(result.total_ms, 400 + 900 + SILENCE_STOP_MS + 120)


class TestSentenceIsNotCutAtEveryPause(unittest.TestCase):
    """实质判据③：一句话中间的**正常停顿**不许把句子切成两半（2026-10-20 加）。

    真机症状：`~/.local/state/capsplus/voice.log` 2026-10-20T18:44:00 采集 3s → 3 字、
    18:44:09 → 4 字，都在说到一半时被 endpoint 掐掉；同一段话手动收尾能录 12s → 47 字。
    900ms 是中文里"报完一个型号再接口令"的典型停顿（本判据用固定 900ms 代表它）。
    """

    def test_900ms_mid_sentence_pause_does_not_split_it(self):
        pcm = silence(400) + voiced(1200) + silence(900) + voiced(1200) + silence(2600)
        result = V.analyze(pcm)
        self.assertTrue(result.has_speech, result.summary())
        self.assertAlmostEqual(result.voiced_ms, 2400, delta=120,
                               msg=f"两段 1.2s 的语音必须算作一整段：{result.summary()}")
        self.assertEqual(result.stop_reason, "endpoint", result.summary())
        self.assertGreaterEqual(result.total_ms, 400 + 1200 + 900 + 1200,
                                "结尾那 2.6 秒静音之前的全部内容都该在录音里")

    def test_short_utterance_still_ends_quickly(self):
        """"打开设置"这种短口令：说完 1.4 秒内必须收尾，不能因为"要容忍长停顿"就拖。"""
        pcm = silence(300) + voiced(500) + silence(2000)
        result = V.analyze(pcm)
        self.assertEqual(result.stop_reason, "endpoint", result.summary())
        self.assertLessEqual(result.silence_hold_ms, SILENCE_STOP_MS,
                             f"短句的基础门槛就是 1200ms，不该更长：{result.summary()}")
        self.assertLessEqual(result.total_ms, 300 + 500 + SILENCE_STOP_MS + 120)

    def test_hold_grows_with_how_long_you_talk_and_is_capped(self):
        """容忍时长随"已经说了多久"单调增长，但**有上界**（否则说完半天不出字）。"""
        holds = {}
        for talked_ms in (600, 2500, 6000, 11000, 16000, 25000):
            vad = V.Vad()
            vad.push(silence(300) + voiced(talked_ms) + silence(3000))
            holds[talked_ms] = vad.result().silence_hold_ms
        # 门槛按**整帧**取整（`round(ms/30)`），所以比出来的数会有 ±15ms 的量化误差：
        # 这就是判定真正用的那个数，别在断言里假装它有毫秒精度。
        self.assertAlmostEqual(holds[600], SILENCE_STOP_MS, delta=15, msg=f"短句用基础值：{holds}")
        self.assertAlmostEqual(holds[2500], SILENCE_STOP_MS + 200, delta=15,
                               msg=f"说了 2.5 秒 → +200ms：{holds}")
        self.assertAlmostEqual(holds[11000], SILENCE_STOP_MS + 1100, delta=15,
                               msg=f"说了 11 秒 → +1100：{holds}")
        self.assertAlmostEqual(holds[16000], SILENCE_STOP_MAX_MS, delta=15,
                               msg=f"到顶就顶住：{holds}")
        self.assertAlmostEqual(holds[25000], SILENCE_STOP_MAX_MS, delta=15, msg=f"必须封顶：{holds}")
        self.assertEqual(list(holds.values()), sorted(holds.values()), "只许单调不减")

    def test_a_long_pause_at_the_end_still_stops_the_recording(self):
        """说完之后的长静音必须还能判停 —— 上界不是"永不判停"。"""
        pcm = silence(300) + voiced(12000) + silence(3000)
        result = V.analyze(pcm)
        self.assertEqual(result.stop_reason, "endpoint", result.summary())
        self.assertLessEqual(result.silence_hold_ms, SILENCE_STOP_MAX_MS)
        self.assertLessEqual(result.total_ms, 300 + 12000 + SILENCE_STOP_MAX_MS + 120)


class TestSixtySecondLimit(unittest.TestCase):
    """实质判据④：上限 60s（旧值 20s），且到点必须**说得出**是被截断的。

    服务端早就吃得下长音频（2026-10-20 实测：60s 音频 HTTP 200、4.9s 出结果），
    20s 这条纯属客户端自造。这条判据钉的是"客户端别再提早掐 + 掐了要留证"。
    """

    def test_default_limit_is_sixty_seconds(self):
        p = V.VadParams()
        self.assertEqual(p.max_ms, MAX_MS)
        self.assertEqual(p.max_frames, 2000)
        self.assertGreater(p.max_ms, OLD_MAX_MS * 2, "本次改动就是要把它抬到 60s 量级")

    def test_sixty_seconds_of_continuous_speech_is_not_cut_at_twenty(self):
        # 63 秒连续有声（字节拼接，别真去算 200 万个正弦）
        pcm = voiced(30000) + voiced(30000) + voiced(3000)
        result = V.analyze(pcm)
        self.assertEqual(result.stop_reason, "max_duration", result.summary())
        self.assertEqual(result.frames, V.VadParams().max_frames)
        self.assertEqual(result.total_ms, MAX_MS, "到 60s 整才停，早一帧都是回退")
        self.assertTrue(result.has_speech)
        self.assertGreater(result.voiced_ms, OLD_MAX_MS,
                           f"旧上限 20s 会把这里截成 20000：{result.summary()}")

    def test_truncation_is_reported_not_silent(self):
        """`stop_reason=max_duration` 是上层唯一的"我被截断了"信号（worker 据此提示用户）。"""
        result = V.analyze(voiced(3000) + voiced(3000), V.VadParams(max_ms=4000))
        self.assertEqual(result.stop_reason, "max_duration", result.summary())
        self.assertIn("max_duration", result.summary())


class TestTrimPadKeepsTheFirstSyllable(unittest.TestCase):
    """实质判据⑤：句首 pad 单独取 300ms（旧值前后各 150）。

    A/B 实测（同一段真人音频、8 种前置静音、打真服务 127.0.0.1:18090）：
    头 pad 150 → 丢首字 5/7；250 → 3/7；**300 → 0/7**；400 / 500 / 800 → 仍是 0/7。
    所以 300 是拐点：再往上只会把噪声喂给模型，不再多救一个字。
    这条判据钉的是**机制**（前置静音真被留下 300ms），概率数字靠上面那轮 A/B。
    """

    def _span(self, pcm, params=None):
        params = params or V.VadParams()
        vad = V.Vad(params)
        vad.push(pcm)
        vad.flush()
        return vad, vad.speech_span_frames()

    def test_head_pad_keeps_three_hundred_ms_of_lead_in(self):
        params = V.VadParams()
        pcm = silence(600) + voiced(900) + silence(1500)
        vad, span = self._span(pcm, params)
        self.assertIsNotNone(span)
        lead = vad.speech_start - span[0]
        self.assertEqual(lead, params.trim_pad_head_frames,
                         f"句首必须留 {TRIM_PAD_HEAD_MS}ms（{params.trim_pad_head_frames} 帧）")
        head_bytes = lead * params.frame_bytes
        self.assertEqual(head_bytes, 9600, "300ms @16k s16le = 9600 字节")
        trimmed = V.trim_pcm(pcm, span, params)
        # 留下的前置静音就是那 300ms，而不是 150ms
        self.assertEqual(trimmed[:head_bytes], pcm[(vad.speech_start - lead) * 960:
                                                  vad.speech_start * 960])

    def test_tail_pad_is_its_own_number(self):
        params = V.VadParams()
        pcm = silence(400) + voiced(900) + silence(1500)
        vad, span = self._span(pcm, params)
        tail = span[1] - (vad.speech_end + 1)
        self.assertEqual(tail, params.trim_pad_tail_frames,
                         f"句尾按自己的 250ms 算（{params.trim_pad_tail_frames} 帧）")
        self.assertLess(tail, params.trim_pad_head_frames,
                        "句尾不需要和句首一样长：弱起音只有句首有")

    def test_trim_never_cuts_voiced_frames(self):
        """pad 再大也不许反过来把语音切掉：区间必须完整包住 [start, end]。"""
        params = V.VadParams()
        pcm = silence(500) + voiced(800) + silence(1600)
        vad, span = self._span(pcm, params)
        self.assertLessEqual(span[0], vad.speech_start)
        self.assertGreater(span[1], vad.speech_end)
        trimmed = V.trim_pcm(pcm, span, params)
        speech_bytes = (vad.speech_end - vad.speech_start + 1) * params.frame_bytes
        self.assertGreaterEqual(len(trimmed), speech_bytes)

    @unittest.skipUnless(REAL_SPEECH_WAV.exists(),
                         f"本机没有真人语音夹具 {REAL_SPEECH_WAV}（CI 正常没有）")
    def test_real_speech_with_lead_silence_is_fully_kept(self):
        """真音频 + 300ms 前置静音：判出来的语音段必须**完整**留在送识别的字节里。"""
        pcm = silence(300) + real_speech_pcm()
        params = V.VadParams()
        vad, span = self._span(pcm, params)
        trimmed = V.trim_pcm(pcm, span, params)
        # 真音频的句首弱起音本来就比合成音高：判出来的起点只会落在前面那 300ms 静音里，
        # 所以留下的前置音频正好是"句首 pad"那么多（这里 == 10 帧 = 300ms）。
        self.assertEqual(vad.speech_start - span[0], params.trim_pad_head_frames,
                         f"留下的前置音频不满 300ms：重音把语音段推后了（{vad.result().summary()}）")
        self.assertLessEqual(span[0], vad.speech_start)
        begin = span[0] * params.frame_bytes
        self.assertEqual(trimmed, pcm[begin:begin + len(trimmed)],
                         "顺序不许乱：切片必须是原音频里连续的一段，不许重排")
        self.assertGreater(len(trimmed), len(real_speech_pcm()) - 32 * 1000,
                           "真音频的内容不许被切掉超过 1 秒")


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
        for chunk in (silence(300), voiced(60), voiced(900), silence(1300)):
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
        result = V.analyze(pcm, V.VadParams(max_ms=20000))
        self.assertEqual(result.stop_reason, "max_duration", result.summary())
        self.assertEqual(result.frames, V.VadParams(max_ms=20000).max_frames)
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
        pcm = silence(300) + voiced(800) + silence(1400)
        base = V.analyze(pcm)
        for size in (960, 700, 1024, 4096):
            with self.subTest(chunk=size):
                got = self._push_in_chunks(pcm, size)
                self.assertEqual(got.has_speech, base.has_speech)
                self.assertEqual(got.voiced_ms, base.voiced_ms)
                self.assertEqual(got.stop_reason, base.stop_reason)
                self.assertEqual(got.gate, base.gate)
                self.assertEqual(got.silence_hold_ms, base.silence_hold_ms,
                                 "自适应的门槛也必须与喂法无关（它只看有声帧数）")


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
               "CAPSPLUS_VOICE_SILENCE_MS": "500", "CAPSPLUS_VOICE_PAD_HEAD_MS": "420"}
        with mock.patch.dict(os.environ, env, clear=False):
            p = V.params_from_env()
            self.assertEqual(p.max_ms, 1234)
            self.assertEqual(p.gate_floor, 300.0)
            self.assertEqual(p.silence_stop_ms, 500)
            self.assertEqual(p.trim_pad_head_ms, 420)
            self.assertEqual(p.frame_ms, V.FRAME_MS, "没覆盖的字段保持默认")
            notes = V.env_notes()
            self.assertEqual(len(notes), 4)
            self.assertTrue(all(n.startswith("CAPSPLUS_VOICE_") for n in notes))

    def test_garbage_value_is_ignored_not_fatal(self):
        """现场调参打错一个字符，不该让语音输入整个失灵。"""
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_MAX_MS": "二十秒"}):
            self.assertEqual(V.params_from_env().max_ms, V.MAX_MS)

    def test_silence_cap_below_base_is_not_a_deadlock(self):
        """上界被打得比基础值还小：按基础值算，不许变成"永不判停"。"""
        params = V.VadParams(silence_stop_ms=1200, silence_stop_max_ms=300)
        self.assertEqual(params.silence_stop_max_frames, 40)
        result = V.analyze(silence(300) + voiced(800) + silence(2000), params)
        self.assertEqual(result.stop_reason, "endpoint", result.summary())

    def test_stricter_gate_changes_the_verdict(self):
        """参数是**真**参与判定的：把地板抬到 5000，合成语音也得判没说话。

        顺带锁一条顺序语义：地板是硬下限，**不许被上限夹掉**
        （`max(地板, min(上限, 4×底噪))`，写反了这里就会绿不了）。
        """
        pcm = silence(300) + voiced(800, level=2000) + silence(1900)
        self.assertTrue(V.analyze(pcm).has_speech)
        tight = V.VadParams(gate_floor=5000.0)
        self.assertEqual(V.energy_gate(19.0, tight), 5000.0)
        self.assertFalse(V.analyze(pcm, tight).has_speech)


class TestGuardHasTeeth(unittest.TestCase):
    """判据红过一次才算判据：这些反例具型证明上面的断言都依赖真实机制。"""

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

    def test_old_fixed_700ms_is_what_split_the_sentence(self):
        """反例具型③：把尾静音塞回**旧的固定 700ms**，同一句话必须重新被切成两半。

        这条是这次改动的直接依据 —— 它红了才说明"自适应"真的在救那句话，
        而不是碰巧没撞上端点。
        """
        pcm = silence(400) + voiced(1200) + silence(950) + voiced(1200) + silence(2600)
        good = V.analyze(pcm)
        self.assertAlmostEqual(good.voiced_ms, 2400, delta=120,
                               msg=f"新门槛下必须是一整段：{good.summary()}")
        old = V.VadParams(silence_stop_ms=OLD_SILENCE_STOP_MS,
                          silence_stop_max_ms=OLD_SILENCE_STOP_MS,
                          silence_grow_per_s=0)
        cut = V.analyze(pcm, old)
        self.assertEqual(cut.stop_reason, "endpoint", cut.summary())
        self.assertLessEqual(cut.voiced_ms, 1200 + 60,
                             f"旧参数下只该录到第一段 1.2s：{cut.summary()}")
        self.assertLess(cut.voiced_ms, good.voiced_ms - 1000)

    def test_old_150ms_head_pad_loses_the_lead_in(self):
        """反例具型⑤：句首 pad 塞回 150ms，同一段音频留下的前置静音就少一半（丢首字的机制）。"""
        pcm = silence(600) + voiced(900) + silence(1500)
        new = V.Vad()
        new.push(pcm)
        new.flush()
        old_params = V.VadParams(trim_pad_head_ms=OLD_TRIM_PAD_MS)
        old = V.Vad()
        old.p = old_params
        old.push(pcm)
        old.flush()
        new_lead = new.speech_start - new.speech_span_frames()[0]
        old_lead = old.speech_start - old.speech_span_frames()[0]
        self.assertEqual(new_lead * 30, TRIM_PAD_HEAD_MS)
        self.assertEqual(old_lead * 30, OLD_TRIM_PAD_MS)
        self.assertEqual(new_lead - old_lead, 5, "差的正是那 150ms = 5 帧")
        new_bytes = len(V.trim_pcm(pcm, new.speech_span_frames(), V.VadParams()))
        old_bytes = len(V.trim_pcm(pcm, old.speech_span_frames(), old_params))
        self.assertEqual(new_bytes - old_bytes, 5 * 960,
                         "多出来的 4800 字节就是被救回来的句首弱起音")

    def test_old_20s_cap_is_what_cut_the_long_sentence(self):
        """反例具型④：上限塞回 20s，同一段 63 秒音频必须早 40 秒就断。"""
        pcm = voiced(30000) + voiced(30000) + voiced(3000)
        old = V.analyze(pcm, V.VadParams(max_ms=OLD_MAX_MS))
        good = V.analyze(pcm)
        old_cap = V.VadParams(max_ms=OLD_MAX_MS)
        self.assertEqual(old.frames, old_cap.max_frames, old.summary())
        self.assertEqual(old.total_ms, old_cap.max_frames * old_cap.frame_ms,
                         f"20000ms / 30ms 取整成 667 帧 = 20010ms：{old.summary()}")
        self.assertEqual(good.total_ms, MAX_MS, good.summary())
        self.assertGreater(good.voiced_ms - old.voiced_ms, MAX_MS - OLD_MAX_MS - 60)

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
