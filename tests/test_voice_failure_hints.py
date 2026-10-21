# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""五种失败，每一种都必须给出「状态 + 下一步」—— 一个都不许变成静默。

为什么单独立一个文件（2026-10-20）：用户那句话背后的第五层是"出了事我不知道是哪种事"。
这五种在这条链上是**完全不同的处置**，混成一句"识别失败"等于把用户丢回猜谜：

  ① 识别服务不在      → 起服务（`systemctl --user start voice-svc`）
  ② 识别服务卡住/超时 → 重启服务，看日志
  ③ 没装 arecord      → 装 alsa-utils
  ④ 设备打不开/被占   → 换默认源（`pactl set-default-source`）
  ⑤ 太短 / 没说话     → 对着麦克风说完整一句（不是"麦克风坏了"）

判据的口径是 **"hint 里必须出现那个动作"**，不是"hint 非空"—— 后者靠一句"出错了"就能糊过去。
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from capsplus import voice_asr as A          # noqa: E402
from capsplus import voice_recorder as R     # noqa: E402
from capsplus import voice_vad as V          # noqa: E402
from capsplus import voice_worker as W       # noqa: E402
from test_voice_asr import pcm_silence, stop_stub, with_stub   # noqa: E402


class FakeStream:
    def __init__(self, data: bytes = b""):
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
    returncode = 1

    def __init__(self, stdout=b"", stderr=b""):
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class TestAsrFailuresSayWhatToDo(unittest.TestCase):
    """① 服务不在 / ② 超时：真跑一遍，看 code 与下一步。"""

    def test_nothing_listening_is_unreachable_with_a_start_command(self):
        import socket
        with socket.socket() as probe:                  # 找一个确定没人听的端口
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_HOST": "127.0.0.1",
                                          "CAPSPLUS_ASR_PORT": str(port)}):
            result = A.recognize(pcm_silence(50), timeout=1.0)
        self.assertEqual(result.code, "unreachable")
        self.assertIn("systemctl --user start voice-svc", result.hint,
                      f"下一步里没有「起服务」这个动作：{result.hint!r}")

    def test_a_stuck_service_reports_timeout_with_a_restart(self):
        server, _seen, patch = with_stub("sleep")
        self.addCleanup(stop_stub, server, patch)
        result = A.recognize(pcm_silence(50), timeout=0.3)
        self.assertEqual(result.code, "timeout")
        self.assertIn("restart voice-svc", result.hint,
                      f"下一步里没有「重启服务」这个动作：{result.hint!r}")

    def test_the_five_cases_each_name_their_own_action(self):
        """五种失败是**五种不同的处置**，一张表把它们钉在一起。

        这张表就是需求本身（"服务不在/超时/无 arecord/设备打不开/太短没说话，
        五种都要有状态 + 下一步"）。任何一条的 hint 被改成一句"出错了"，这里立刻红 ——
        而"hint 非空"那种判据是拦不住的。
        """
        table = [
            ("unreachable", "systemctl --user start voice-svc"),
            ("timeout", "restart voice-svc"),
            ("no-arecord", "alsa-utils"),
            ("device-busy", "pactl"),
            ("no_speech", "说完整一句"),
            ("too_short", "说完整一句"),
        ]
        homes = {"unreachable": A.HINTS, "timeout": A.HINTS,
                 "no-arecord": R.HINTS, "device-busy": R.HINTS,
                 "no_speech": None, "too_short": None}
        for code, token in table:
            home = homes[code]
            hint = W.Session._drop_hint(code) if home is None else home[code]
            self.assertIn(token, hint,
                          f"{code} 的下一步里没有「{token}」：{hint!r}")


class TestRecorderFailuresSayWhatToDo(unittest.TestCase):
    """③ 没装 arecord / ④ 设备打不开：code 必须分得开，下一步必须对得上。"""

    def _run(self, popener, **kw):
        rec = (R.Recorder(V.VadParams(max_ms=500), popener=popener, **kw) if popener
               else R.Recorder(V.VadParams(max_ms=500), **kw))
        errors = []
        rec.on_error = lambda code, message, hint: errors.append((code, message, hint))
        rec.start()
        rec.wait(timeout=10)
        return errors, rec

    def test_missing_arecord_points_at_alsa_utils(self):
        """**不注入 popener**（注入了就等于说"我自己知道跑什么"，那条检查会被跳过）——
        只把 `shutil.which` 指空，看它自己判不判得出来。"""
        with mock.patch("shutil.which", return_value=None):
            errors, _rec = self._run(None)
        self.assertEqual([e[0] for e in errors], ["no-arecord"])
        self.assertIn("alsa-utils", errors[0][2], f"下一步没说要装什么：{errors[0][2]!r}")

    def test_a_busy_device_points_at_the_default_source(self):
        """设备被占过去报的是通用的 no-data（下一步是"查麦克风权限"）—— 方向是错的。"""
        proc = FakeProc(stderr=b"arecord: main:831: audio open error: Device or resource busy")
        errors, _rec = self._run(lambda argv, **kw: proc)
        self.assertEqual([e[0] for e in errors], ["device-busy"],
                         "设备被占还是走了通用分支 —— 那 HINTS['device-busy'] 就是死代码")
        self.assertIn("pactl", errors[0][2],
                      f"下一步没说是换源：{errors[0][2]!r}")

    def test_an_unclassifiable_failure_stays_generic(self):
        """**没有线索就不许编**：stderr 里没提 busy 就该是通用的 no-data。"""
        proc = FakeProc(stderr="arecord: 说不清的怪错误".encode())
        errors, _rec = self._run(lambda argv, **kw: proc)
        self.assertEqual([e[0] for e in errors], ["no-data"])

    def test_classification_only_uses_real_clues(self):
        self.assertEqual(R.classify_capture_failure("Device or resource busy"), "device-busy")
        self.assertEqual(R.classify_capture_failure("RESOURCE BUSY"), "device-busy")
        self.assertEqual(R.classify_capture_failure(""), "no-data")
        self.assertEqual(R.classify_capture_failure("No such file or directory"), "no-data")

    def test_every_recorder_code_has_a_hint(self):
        for code in ("no-arecord", "device-busy", "spawn-failed", "no-data"):
            self.assertIn(code, R.HINTS, f"录音侧少了 {code}")
            self.assertTrue(R.HINTS[code].strip(), f"HINTS[{code}] 是空的")


class TestTooShortSaysTheRightThing(unittest.TestCase):
    """⑤ 太短/没说话：**不许**把它报成"麦克风坏了"。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": self._tmp.name,
                                                 "CAPSPLUS_VOICE_UI": "0"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_the_two_codes_share_a_hint_that_tells_you_to_speak(self):
        for code in ("no_speech", "too_short"):
            hint = W.Session._drop_hint(code)
            self.assertIn("说完整一句", hint, f"{code} 的下一步没告诉用户他该做什么：{hint!r}")

    def test_it_is_not_reported_as_a_broken_microphone(self):
        server = W.Server(params=V.VadParams(max_ms=1000), popener=lambda *a, **k: None,
                          review_ui=False)
        server.broadcast = lambda frame: None
        session = server._ensure_session()
        events = []
        server.broadcast = events.append
        session.state = "recording"      # 不起真录音线程：这条测的是"判决后怎么报"
        session._on_recording_done(R.Recording(pcm=b"", vad=V.VadResult(
            has_speech=False, drop_reason="no_speech", frames=10, total_ms=300)))
        errors = [e for e in events if e.get("kind") == "error"]
        self.assertTrue(errors, "没说话这条一个错误事件都没发 —— 那就是静默")
        self.assertIn("没检测到说话", errors[-1].get("message", ""))
        hint = errors[-1].get("hint", "")
        self.assertIn("完整一句", hint, f"下一步没有可执行的建议：{hint!r}")


class TestHintsActuallyReachTheUser(unittest.TestCase):
    """hint 写在表里不等于用户看得见：这条把"表 → 报文"这条路走通一次。"""

    def test_recorder_hints_travel_on_the_error_event(self):
        server = W.Server(params=V.VadParams(max_ms=500), popener=lambda *a, **k: None,
                          review_ui=False)
        events = []
        server.broadcast = events.append
        session = server._ensure_session()
        session._on_recorder_error("device-busy", "Device or resource busy",
                                   R.HINTS["device-busy"])
        self.assertEqual(events[-1]["kind"], "error")
        self.assertEqual(events[-1]["code"], "device-busy")
        self.assertIn("pactl", events[-1]["hint"])


if __name__ == "__main__":
    unittest.main(verbosity=2)