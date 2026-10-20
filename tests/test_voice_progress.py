# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""录音期的过程反馈：录音器实时电平 → worker 每秒心跳 → 牌子 / status / 日志。

为什么这批判据值得写（2026-10-20）：明江那句「只可以是短语音」的背后，除了端点参数，
还有一件事 —— **录音期屏幕上什么都没有**。于是"它还在录吗／听到我说话了吗／录了多久"
全靠猜，而端点判定恰恰是按静音时长下刀的，看不见就一定会以为"它自己断了"。

这批判据钉的是那条从"麦克风那一帧"到"用户看得见的那行字"的链：
  * `Recorder.live()` 的每个数都是**那一帧**算出来的（含峰值，按手算的数对）；
  * worker 每秒把它搬到 status 与牌子上，并**限流**写日志（否则 60s 录音刷 60 行）；
  * `meter --live` 的渲染与参数校验（CI 没有麦克风，用字节流喂同一段代码）。

反例具型在 `TestGuardHasTeeth`：把限流拿掉、把 `live()` 的时间轴改成自己读时钟，必须红。
"""

import io
import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_osd as O          # noqa: E402
from capsplus import voice_proto as P        # noqa: E402
from capsplus import voice_recorder as R     # noqa: E402
from capsplus import voice_vad as V          # noqa: E402
from capsplus import voice_worker as W       # noqa: E402


def speech(ms: int, level: int = 6000, freq: int = 440) -> bytes:
    n = int(V.SAMPLE_RATE * ms / 1000)
    return struct.pack("<%dh" % n,
                       *[int(level * math.sin(2 * math.pi * freq * i / V.SAMPLE_RATE))
                         for i in range(n)])


def quiet(ms: int, level: int = 10) -> bytes:
    n = int(V.SAMPLE_RATE * ms / 1000)
    return struct.pack("<%dh" % n, *([level] * n))


def hand_rms(chunk: bytes) -> float:
    n = len(chunk) // 2
    vals = struct.unpack("<%dh" % n, chunk[:n * 2])
    return (sum(v * v for v in vals) / max(n, 1)) ** 0.5


class FakeStream:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self.data) - self.pos
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def close(self):
        pass


class FakeProc:
    returncode = 0

    def __init__(self, pcm: bytes):
        self.stdout = FakeStream(pcm)
        self.stderr = FakeStream(b"")

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class TestRecorderLiveReads(unittest.TestCase):
    """`live()` 里的每个数都必须是那一帧自己的。"""

    def setUp(self):
        self.rec = R.Recorder(V.VadParams())

    def feed(self, chunk: bytes) -> None:
        """照**真实录音线程里的顺序**走一遍：先 `vad.push`（时间轴与 RMS 都产生在那儿），
        再 `_note_live`。跳过 push 直接调 `_note_live` 是拿一个别的时序去测它，没意义。"""
        self.rec.vad.push(chunk)
        self.rec._note_live(chunk)

    def test_a_fresh_recorder_has_the_full_shape(self):
        live = self.rec.live()
        self.assertEqual(set(live), set(R._empty_live()),
                         "形状不一致 —— 少一个键就是调用方的 KeyError")
        self.assertEqual(live["elapsed_ms"], 0)

    def test_one_frame_updates_rms_peak_and_time(self):
        frame = speech(30, level=6000)
        self.feed(frame)
        live = self.rec.live()
        self.assertEqual(live["elapsed_ms"], V.FRAME_MS, "时长不是按帧算的（只许有一条时间轴）")
        self.assertAlmostEqual(live["rms"], hand_rms(frame), places=3,
                               msg="RMS 与手算不一致（要么重算了，要么拿错了帧）")
        self.assertAlmostEqual(live["peak"], 6000, delta=40,
                               msg="峰值不是这一帧的绝对值最大值")

    def test_the_reading_follows_the_sound(self):
        """安静 → 说话：两个读数必须跟着变，否则条子是死的。"""
        self.feed(quiet(30))
        silent = self.rec.live()
        self.feed(speech(30))
        loud = self.rec.live()
        self.assertLess(silent["rms"], loud["rms"])
        self.assertGreater(loud["rms"], 3000)
        self.assertFalse(silent["speaking"])
        self.assertTrue(loud["speaking"], "过了门还不算‘在说话’，牌子就会一直显示没听见")

    def test_the_gate_exposed_is_the_one_the_vad_used(self):
        """校准窗填满之后，`live()['gate']` 必须等于 VAD 当场那个门（不许自己再算一份）。"""
        for _ in range(int(V.NOISE_WINDOW_MS / V.FRAME_MS) + 1):
            self.feed(quiet(30))
        self.assertEqual(self.rec.live()["gate"], self.rec.vad.gate)
        self.assertEqual(self.rec.live()["gate"],
                         V.energy_gate(self.rec.vad.noise_rms, self.rec.vad.p))

    def test_a_full_recording_leaves_the_last_reading_readable(self):
        """录完之后 `live()` 仍给出最后那一刻（status 与日志要拿它复盘）。"""
        from capsplus import voice_inject as INJ
        pcm = speech(600)
        rec = R.Recorder(V.VadParams(max_ms=1000), popener=lambda argv, **kw: FakeProc(pcm))
        done = []
        rec.on_result = done.append
        rec.start()
        rec.wait(timeout=10)
        self.assertTrue(done, "假麦克风都没走完")
        live = rec.live()
        self.assertGreater(live["elapsed_ms"], 0)
        self.assertGreater(live["peak"], 0)
        self.assertEqual(set(live), set(R._empty_live()))


class FakeRecorder:
    def __init__(self, live):
        self._live = live
        self.calls = 0

    def live(self):
        self.calls += 1
        return dict(self._live)


class FakeBadge:
    def __init__(self):
        self.events = []

    def post_progress(self, *args):
        self.events.append(args)

    def close_badge(self):
        self.events.append("closed")


class TestWorkerProgressReachesTheUser(unittest.TestCase):
    """每秒心跳：status 里看得见、牌子上刷得到、日志里有限流。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": self._tmp.name,
                                                 "CAPSPLUS_VOICE_UI": "0",
                                                 "CAPSPLUS_VOICE_BADGE": ""})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.srv = W.Server(params=V.VadParams(max_ms=60000), popener=lambda *a, **k: None,
                            review_ui=False)
        self.srv.broadcast = lambda frame: None
        self.session = self.srv._ensure_session()
        self.session.state = "recording"
        self.session.recorder = FakeRecorder({"elapsed_ms": 1200, "rms": 3000.0, "peak": 9000,
                                              "voiced_frames": 40, "gate": 150.0,
                                              "speaking": True})

    def log_lines(self, needle: str) -> list[str]:
        path = P.log_path()
        if not path.exists():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if needle in ln]

    def test_status_frame_carries_the_live_reading(self):
        self.srv._record_progress(self.session)
        self.srv.session = self.session
        frame = self.srv._status_frame()
        self.assertTrue(frame.get("live"), f"status 里没有录音读数：{frame.get('live')!r}")
        for key in ("录音中", "RMS", "门限"):
            self.assertIn(key, frame["live"], f"读数少了 {key}：{frame['live']}")
        self.assertEqual(frame["live"], self.session.live_summary)

    def test_the_badge_gets_the_same_numbers(self):
        badge = FakeBadge()
        self.session.badge_ui = badge
        self.srv._record_progress(self.session)
        self.assertEqual(badge.events, [(1200, 3000.0, 9000, 150.0, 60000)],
                         "牌子拿到的不是同一份读数（两处各读一次迟早会漂）")

    def test_no_badge_is_not_an_error(self):
        self.session.badge_ui = None
        self.assertEqual(self.srv._record_progress(self.session), None)

    def test_the_log_is_rate_limited(self):
        """1 Hz 心跳不许每秒写一行：60s 录音该是十来行，不是 60 行。"""
        self.srv._record_progress(self.session)                     # 首拍必写
        seen = len(self.log_lines(" voice_progress "))
        self.assertEqual(seen, 1, f"首拍没写或写多了：{seen}")
        for elapsed in (2200, 3200, 4200, 5200):                    # 5s 内连刷
            self.session.recorder._live["elapsed_ms"] = elapsed
            self.srv._record_progress(self.session)
        self.assertEqual(len(self.log_lines(" voice_progress ")), 1,
                         "5 秒内的心跳也写日志了 —— 限流没生效")
        self.session.recorder._live["elapsed_ms"] = 12000           # 跨过 5s
        self.srv._record_progress(self.session)
        self.assertEqual(len(self.log_lines(" voice_progress ")), 2, "过了限流窗口还是不写")

    def test_the_log_line_has_the_numbers_and_no_content(self):
        self.srv._record_progress(self.session)
        line = self.log_lines(" voice_progress ")[0]
        for key in ("elapsed_ms=", "rms=", "peak=", "gate=", "speaking="):
            self.assertIn(key, line, f"日志行少了 {key}：{line}")

    def test_progress_rides_the_existing_tick(self):
        """口径：主线程只有**一个周期任务**（第二个会跟看门狗咬在一起）。

        用 AST 而不是数源码里的字面量：`after_ms` 也调 `glib.timeout_add`（那是一次性的），
        数总数会把两条完全不同性质的东西混在一起，判据就成了"数字对不对"而不是"性质对不对"。
        """
        import ast
        tree = ast.parse((REPO / "capsplus" / "voice_worker.py").read_text(encoding="utf-8"))
        tick = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "_watchdog_tick")
        called = {n.func.attr for n in ast.walk(tick)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn("_record_progress", called,
                      "心跳没挂在那唯一的周期任务上 —— 难道又开了一个定时器？")


class TestBadgeIsOffByDefault(unittest.TestCase):
    """牌子默认不弹（Wayland 没有"别把焦点给我"，所以"绝不抢焦点"= 那时没有窗口）。"""

    def test_no_window_is_built_without_the_opt_in(self):
        with mock.patch.dict(os.environ, {O.BADGE_ENV: ""}):
            ui = O.VoiceUi()
            self.assertFalse(ui.open_badge())
            self.assertIsNone(ui._window)
            self.assertFalse(O.badge_enabled())

    def test_progress_events_are_dropped_when_there_is_no_window(self):
        """攒着它们会在建窗那一刻一次放出来（60s 录音 = 60 次刷新），把真正要显示的淹掉。"""
        ui = O.VoiceUi()
        for i in range(20):
            ui.post_progress(i * 1000, 3000.0, 9000, 150.0, 60000)
        self.assertEqual(ui._pending, [], "心跳被攒起来了")

    def test_other_events_are_still_queued(self):
        """只有心跳丢，别的一律照旧排队（这条用来防"顺手把队列整个关掉"）。"""
        ui = O.VoiceUi()
        ui.post_text("早上九点开会")
        self.assertEqual(len(ui._pending), 1)


class TestRecordingLineVerdict(unittest.TestCase):
    """那行字的判读规则（纯函数；牌子与 meter --live 共用同一处判读）。"""

    def test_three_bands_around_the_gate(self):
        self.assertEqual(O.level_verdict(3000, 150), "有声")
        self.assertEqual(O.level_verdict(100, 150), "偏轻")
        self.assertEqual(O.level_verdict(10, 150), "还没听到说话")

    def test_an_ungated_reading_refuses_to_judge(self):
        """门限还没校准出来（0）时不许编判读 —— 拿假判读指导用户调麦克风比不判更糟。"""
        self.assertEqual(O.level_verdict(3000, 0), "—")
        line = O.recording_line(1000, 3000, 9000, 0, 60000)
        self.assertNotIn("门限", line)
        self.assertIn("RMS 3000", line)

    def test_the_line_carries_time_level_and_verdict(self):
        line = O.recording_line(3200, 3000, 9000, 150.0, 60000)
        self.assertIn("3.2s", line)
        self.assertIn("RMS 3000", line)
        self.assertIn("峰 9000", line)
        self.assertIn("有声", line)

    def test_the_approaching_limit_is_announced(self):
        self.assertNotIn("上限", O.recording_line(1000, 3000, 9000, 150.0, 60000))
        self.assertIn("上限", O.recording_line(58000, 3000, 9000, 150.0, 60000))


class TestMeterLiveRendering(unittest.TestCase):
    """`meter --live` 那一行：CI 没有麦克风，用字节流喂同一段代码。"""

    def test_the_bar_is_calibrated_to_the_gate(self):
        quiet_line = W.meter_live_line(1000, 20, 200, 150.0)
        loud_line = W.meter_live_line(1000, 400, 2000, 150.0)
        self.assertLess(quiet_line.count("█"), loud_line.count("█"))
        self.assertIn("有声", loud_line)
        self.assertIn("还没听到说话", quiet_line)

    def test_the_line_shows_time_level_and_peak(self):
        line = W.meter_live_line(2500, 320.5, 2800, 150.0)
        self.assertIn("2.5s", line)
        self.assertIn("320", line)
        self.assertIn("2800", line)

    def test_the_verdict_comes_from_the_shared_function(self):
        """判读只有一份：meter 的判读必须与牌子上的判读逐字相同。"""
        for rms in (10, 100, 3000):
            self.assertIn(O.level_verdict(rms, 150.0), W.meter_live_line(0, rms, 0, 150.0))


class TestRunMeterLive(unittest.TestCase):
    """真跑那段循环（喂字节流，不碰麦克风）。"""

    def setUp(self):
        self.params = V.VadParams()

    def _run(self, pcm: bytes, seconds: float):
        out = io.StringIO()
        got = W.run_meter_live(io.BytesIO(pcm), seconds, self.params, out=out)
        return got, out.getvalue()

    def test_it_collects_exactly_what_it_paints(self):
        pcm = quiet(600) + speech(600)
        got, text = self._run(pcm, 1.0)
        self.assertEqual(len(got), int(1.0 * V.SAMPLE_RATE) * V.SAMPLE_WIDTH,
                         "采够时长就该停，别多读也别少读")
        self.assertEqual(got, pcm[:len(got)], "画的和收的不是同一段字节")
        self.assertTrue(text.endswith("\n"), "收尾没换行会把小结接在条子后面")
        self.assertGreaterEqual(text.count("\n"), 1)

    def test_it_paints_more_than_once_and_reports_speech(self):
        got, text = self._run(speech(1500), 1.5)
        self.assertTrue(got)
        self.assertGreater(text.count("\r"), 1, "只画了第一帧 —— 那就不是‘实时’")
        self.assertIn("有声", text, "一段 6000 RMS 的语音没被标成有声")

    def test_a_short_stream_stops_cleanly(self):
        got, text = self._run(speech(120), 3.0)
        self.assertEqual(len(got), len(speech(120)) - (len(speech(120)) % self.params.frame_bytes),
                         "流提前结束时要按帧收口，别把半帧算进去")
        self.assertTrue(text.endswith("\n"))

    def test_an_empty_stream_returns_nothing(self):
        got, text = self._run(b"", 1.0)
        self.assertEqual(got, b"")
        self.assertTrue(text.endswith("\n"))


class TestMeterArgs(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(W.meter_args(["meter"]), (False, 3.0))
        self.assertEqual(W.meter_args(["meter", "--live"]), (True, 10.0))
        self.assertEqual(W.meter_args(["meter", "-l", "20"]), (True, 20.0))
        self.assertEqual(W.meter_args(["meter", "5"]), (False, 5.0))

    def test_bad_arguments_are_refused_not_silently_defaulted(self):
        for argv in (["meter", "0.5"], ["meter", "99"], ["meter", "--live", "99"],
                     ["meter", "abc"], ["meter", "1", "2"]):
            with self.assertRaises(ValueError, msg=f"{argv} 没被拒"):
                W.meter_args(argv)

    def test_live_allows_a_longer_window_than_the_summary_meter(self):
        self.assertEqual(W.meter_args(["meter", "--live", "60"]), (True, 60.0))
        with self.assertRaises(ValueError):
            W.meter_args(["meter", "60"])


class TestGuardHasTeeth(unittest.TestCase):
    """反例具型：这几条规则拿掉之后，上面的判据必须真的红（现场已复算过，见交付报告）。"""

    def test_rate_limit_is_load_bearing(self):
        """把限流去掉，5 秒内的心跳会留下 5 行 —— 上面那条断言就是为它写的。"""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": tmp,
                                             "CAPSPLUS_VOICE_UI": "0"}):
            srv = W.Server(params=V.VadParams(max_ms=60000), popener=lambda *a, **k: None,
                           review_ui=False)
            srv.broadcast = lambda frame: None
            session = srv._ensure_session()
            session.state = "recording"
            session.recorder = FakeRecorder({"elapsed_ms": 1000, "rms": 3000.0, "peak": 9000,
                                             "voiced_frames": 40, "gate": 150.0,
                                             "speaking": True})
            original = srv.PROGRESS_LOG_EVERY_MS
            srv.PROGRESS_LOG_EVERY_MS = 1                      # ← 等于把限流拿掉
            try:
                for elapsed in (1000, 2000, 3000, 4000, 5000):
                    session.recorder._live["elapsed_ms"] = elapsed
                    srv._record_progress(session)
            finally:
                srv.PROGRESS_LOG_EVERY_MS = original
            lines = [ln for ln in P.log_path().read_text(encoding="utf-8").splitlines()
                     if " voice_progress " in ln]
            self.assertGreater(len(lines), 1,
                               "限流一旦拿掉就该多写几行 —— 这条不红说明限流其实没在起作用")

    def test_the_single_timer_count_reacts_to_a_second_timer(self):
        """那条"心跳挂在既有节拍上"的判据是靠 AST 定位函数成立的。"""
        import ast
        source = (REPO / "capsplus" / "voice_worker.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        def tick_calls(text):
            tree_ = ast.parse(text)
            tick = next(n for n in ast.walk(tree_)
                        if isinstance(n, ast.FunctionDef) and n.name == "_watchdog_tick")
            return {n.func.attr for n in ast.walk(tick)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}

        self.assertIn("_record_progress", tick_calls(source))
        self.assertNotIn("_record_progress",
                         tick_calls(source.replace("self._record_progress(session)",
                                                   "self._something_else(session)")),
                         "定位方式失效了，那条判据就成了摆设")


if __name__ == "__main__":
    unittest.main(verbosity=2)