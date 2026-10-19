# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：截图链路的纯逻辑层（信号实参 / 等产物 / 超时话术 / journal 解析 / 选区几何）。

为什么能在这里钉：这一层刻意不 import gi（`capsplus/shot_frame.py`），也不碰真屏、不碰扩展，
所以**没有任何显示服务器也能跑**，无头 CI 上照跑。真机抓帧那一半（`capsplus/shot_capture.py`
的 gi 部分、扩展的 `Shell.Screenshot` 调用）在这里只钉**形状**：实参顺序、超时行为、格式契约。

每条判据的"牙"在哪：
* `TestWaitForFile` 的假时钟：删掉轮询里的 `exists()` → 立刻红（会一直等到超时）；
  删掉"次数上限"兜底 → 假时钟不前进时死循环（CI 挂住比红更糟，这条守的就是它）；
* `TestCaptureOrchestration.test_stale_artifact_is_not_mistaken_for_fresh_shot`：真产物**先清残骸**
  这一步删掉就红 —— 不清的话"文件在不在"永远为真，会把上一次的旧图当成本次截图交出去；
* `TestLogContract`：扩展改了 journal 格式而解析器没跟上 → 红（这是跨语言契约，不是自说自话）。
"""

import pathlib
import datetime
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_frame as SF          # noqa: E402
from capsplus import shot_capture as SC        # noqa: E402

# 一张真的 1×1 PNG（本机实测被抓帧链路写成过这种产物，拿它当"完整产物"的样本）
PNG_1PX = bytes([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1,
                 8, 2, 0, 0, 0, 144, 119, 83, 222, 0, 0, 0, 12, 73, 68, 65, 84, 8, 215, 99, 248,
                 207, 192, 0, 0, 3, 1, 1, 0, 24, 221, 141, 176, 0, 0, 0, 0, 73, 69, 78, 68, 174,
                 66, 96, 130])



# 临时目录统一入口：判据**不许往 /tmp 里堆垃圾**（跑一轮留一堆目录 = 事后审计的负担，
# 2026-10-19 实测一次全量跑就留下两百多个）。tearDownModule 保证一个测试模块跑完全清。
_TEMP_DIRS = []


def temp_dir(prefix: str = "capsplus-shot-test-"):
    d = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append(d)
    return d


def tearDownModule():
    for d in _TEMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    _TEMP_DIRS.clear()

class FakeClock:
    """假时钟 + 假睡眠：`wait_for_file` 的注入点就是为它留的（判据不许真等 3 秒）。"""

    def __init__(self, step=0.05):
        self.t = 1000.0
        self.step = step
        self.slept = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept += 1
        self.t += max(seconds, self.step)


class TestSignalArgs(unittest.TestCase):
    """发出去的那 5 个实参：顺序错了 shell 那边就订不到 / 参数解不出来。"""

    def test_emit_signal_shape(self):
        args = SF.capture_signal_args(1, 2, 3, 4, "/tmp/x.png")
        self.assertEqual(args, (None, SF.BUS_PATH, SF.BUS_IFACE, SF.M_CAPTURE,
                                (1, 2, 3, 4, "/tmp/x.png")),
                         "emit_signal 的实参顺序是 (destination, path, interface, member, parameters)"
                         " —— 本机实测过（tools/verify-raise-browser.py 同一条），别改")

    def test_capture_signature_matches_arg_count(self):
        self.assertEqual(SF.CAPTURE_SIG, "(iiiis)")
        self.assertEqual(len(SF.capture_signal_args(0, 0, 1, 1, "p")[4]), 5,
                         "tuple 里的元素个数必须等于签名里的类型个数，不然 shell 那边 deepUnpack"
                         " 出来的是错位的值")

    def test_pin_signature_matches_arg_count(self):
        self.assertEqual(SF.PIN_SIG, "(siiiis)")
        self.assertEqual(len(SF.pin_signal_args("/tmp/r.raw", 2, 3, 4, 5, "/tmp/a")[4]), 6)

    def test_members_are_distinct(self):
        members = {SF.M_CAPTURE, SF.M_PIN}
        self.assertEqual(len(members), 2, "抓帧与贴图必须是两个成员名，否则 shell 侧分不出来")

    def test_coords_are_ints(self):
        args = SF.capture_signal_args(1.0, 2.0, 3.7, 4, "p")
        self.assertTrue(all(isinstance(v, int) for v in args[4][:4]),
                        "GVariant '(iiiis)' 里塞 float 会被 GI 拒（或截断成你想不到的值）")


class TestWaitForFile(unittest.TestCase):
    def test_returns_as_soon_as_the_artifact_exists(self):
        clock = FakeClock()
        seen = {"n": 0}

        def exists(_p):
            seen["n"] += 1
            return seen["n"] >= 3

        ok, waited = SF.wait_for_file("/x", 3000, exists=exists, sleep=clock.sleep, now=clock.now)
        self.assertTrue(ok)
        self.assertEqual(seen["n"], 3, "文件一出现就该立刻返回，别再多睡几轮")
        self.assertLess(waited, 3000)

    def test_times_out_and_says_how_long(self):
        clock = FakeClock()
        ok, waited = SF.wait_for_file("/x", 300, exists=lambda _p: False,
                                      sleep=clock.sleep, now=clock.now)
        self.assertFalse(ok)
        self.assertGreaterEqual(waited, 300, "超时时间要如实报出来（排查靠它）")
        self.assertLess(waited, 400, "别把超时拖成两倍：wait 的上限应约等于 timeout")

    def test_a_mocked_broken_clock_cannot_hang(self):
        """时钟不前进（判据写错 / now 被 mock 坏）时也必须收工 —— 挂在 CI 上比红更糟。"""
        ok, _ = SF.wait_for_file("/x", 200, poll_ms=50, exists=lambda _p: False,
                                 sleep=lambda _s: None, now=lambda: 42.0)
        self.assertFalse(ok)


class TestTimeoutReport(unittest.TestCase):
    """超时话术必须**可操作**：明江讨厌被打断，给他能自己跑的一条命令，而不是一句"失败了"。"""

    def setUp(self):
        self.text = SF.timeout_report("/tmp/out.png", 3000, journal_hint="journalctl --user -n 200")

    def test_names_the_artifact(self):
        self.assertIn("/tmp/out.png", self.text)

    def test_gives_the_three_actions(self):
        self.assertIn("gnome-extensions list --enabled", self.text)
        self.assertIn(SF.EXT_UUID, self.text)
        self.assertIn("注销重登", self.text, "改过 extension.js 后 disable/enable 不会重新 import"
                                          "（2026-10-19 实测）—— 这条必须写在话术里，"
                                          "否则现场只会以为'扩展坏了'")
        self.assertIn("journalctl", self.text)

    def test_failure_reason_from_journal_is_surfaced(self):
        recs = SF.parse_shot_log("[raise-browser] shot result=fail path=/tmp/o.png why=open:权限不够 ms=3")
        self.assertIn("open:权限不够", SF.failure_report("/tmp/o.png", recs))
        self.assertEqual(SF.failure_report("/tmp/o.png", []), "",
                         "journal 里没有失败行时不许编一条原因出来")


class TestLogContract(unittest.TestCase):
    """扩展写的 journal 行 ↔ 这里的解析器：跨语言契约，格式漂了就红。"""

    OK_LINE = "MESSAGE [raise-browser] shot result=ok path=/tmp/s.png rect=120,80,640,480 scale=2 ms=42"

    def test_parses_ok_line(self):
        recs = SF.parse_shot_log(self.OK_LINE)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["result"], "ok")
        self.assertEqual(r["path"], "/tmp/s.png")
        self.assertEqual(r["rect"], "120,80,640,480")
        self.assertEqual(r["scale"], "2")
        self.assertEqual(r["ms"], "42")

    def test_parses_fail_line_with_reason(self):
        recs = SF.parse_shot_log("[raise-browser] shot result=started path=/tmp/s.png rect=0,0,1,1 scale=1")
        self.assertEqual(recs[0]["result"], "started")

    def test_ignores_unrelated_lines(self):
        text = "随便一行\n[raise-browser] probe default=x windows=1\n[raise-browser] ready capture=on\n"
        self.assertEqual(SF.parse_shot_log(text), [],
                         "别的行不许被当成抓帧记录（不然判活会看错）")


class TestCaptureOrchestration(unittest.TestCase):
    """`shot_capture.capture()` 的编排：单测里 stub 掉 D-Bus、时钟与产物校验 —— 没有真屏也能跑。"""

    def setUp(self):
        self.dir = temp_dir("capsplus-shot-test-")
        self.out = self.dir / "s.png"
        self.calls = []

    def _emit_making_file(self):
        def emit_fn():
            self.calls.append("emit")
            self.out.write_bytes(PNG_1PX)
        return emit_fn

    def test_happy_path_emits_then_sees_the_product(self):
        ok, msg = SC.capture(10, 20, 30, 40, self.out, emit_fn=self._emit_making_file())
        self.assertTrue(ok, msg)
        self.assertEqual(self.calls, ["emit"])
        self.assertIn(str(self.out), msg)

    def test_stale_artifact_is_not_mistaken_for_fresh_shot(self):
        """残骸先清掉：不然"文件在不在"这个判据永远为真 —— 会把旧图当新图交出去。"""
        self.out.write_bytes("上次的旧图".encode())
        ok, msg = SC.capture(10, 20, 30, 40, self.out, emit_fn=lambda: self.calls.append("emit"),
                             timeout_ms=100)
        self.assertFalse(ok, "旧产物还在就直接判成功 = 交出一张假截图")
        self.assertIn("没等到", msg)

    def test_timeout_message_is_actionable_and_quotes_journal(self):
        (self.dir / "fail.txt").write_text("x")   # 只为确定性：目录存在
        ok, msg = SC.capture(0, 0, 10, 10, self.out, emit_fn=lambda: None, timeout_ms=50,
                             wait_fn=lambda p, ms: SF.wait_for_file(
                                 p, ms, exists=lambda _p: False, sleep=lambda _s: None,
                                 now=lambda: 7.0))
        self.assertFalse(ok)
        for token in ("gnome-extensions list --enabled", "注销重登", "journalctl"):
            self.assertIn(token, msg)

    def test_half_product_is_rejected(self):
        def emit_fn():
            self.out.write_bytes(PNG_1PX[:-12])      # 缺 IEND = 半张图

        ok, msg = SC.capture(0, 0, 10, 10, self.out, emit_fn=emit_fn)
        self.assertFalse(ok, "半张图必须当场报错，不能拿出去当截图用")
        self.assertIn("IEND", msg)

    def test_degenerate_rect_never_reaches_the_bus(self):
        ok, _ = SC.capture(0, 0, 0, 10, self.out, emit_fn=lambda: self.calls.append("emit"))
        self.assertFalse(ok)
        self.assertEqual(self.calls, [], "0 宽的区域不该发信号（浪费一次 shell 抓帧）")


class TestPngCompleteness(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir("capsplus-shot-test-")

    def _write(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_complete_png_passes(self):
        ok, why = SF.is_complete_png(self._write("a.png", PNG_1PX))
        self.assertTrue(ok, why)

    def test_truncated_png_is_caught(self):
        ok, why = SF.is_complete_png(self._write("b.png", PNG_1PX[:-12]))
        self.assertFalse(ok)
        self.assertIn("IEND", why)

    def test_zero_byte_and_wrong_magic_are_caught(self):
        self.assertFalse(SF.is_complete_png(self._write("c.png", b""))[0])
        self.assertFalse(SF.is_complete_png(self._write("d.png", b"not a png at all"))[0])

    def test_missing_file_is_caught_not_raised(self):
        ok, why = SF.is_complete_png(self.dir / "nope.png")
        self.assertFalse(ok)
        self.assertIn("读不到", why)


class TestSelectionGeometry(unittest.TestCase):
    def test_reverse_drag_gives_positive_size(self):
        self.assertEqual(SF.normalize_rect(100, 80, 20, 10), (20, 10, 80, 70),
                         "从右下往左上拖（很常见）必须得到正的宽高，否则抓帧会失败")

    def test_tiny_selection_is_rejected(self):
        self.assertFalse(SF.accept_rect(SF.normalize_rect(10, 10, 11, 11)))
        self.assertTrue(SF.accept_rect(SF.normalize_rect(10, 10, 60, 40)))

    def test_clamp_keeps_selection_inside_the_screen(self):
        self.assertEqual(SF.clamp_rect((1800, 1000, 400, 400), (0, 0, 1920, 1080)),
                         (1800, 1000, 120, 80))
        self.assertEqual(SF.clamp_rect((-50, -50, 100, 100), (0, 0, 1920, 1080)), (0, 0, 50, 50))

    def test_union_covers_all_monitors(self):
        self.assertEqual(SF.union_rect([(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]),
                         (0, 0, 3840, 1080),
                         "遮罩要盖住所有屏；抓帧用的是**全局逻辑坐标**（本机三块屏实测）")
        self.assertEqual(SF.union_rect([]), (0, 0, 0, 0))


class TestSaveLocation(unittest.TestCase):
    def test_env_override_wins(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"CAPSPLUS_SHOT_DIR": "/tmp/demo-shots"}):
            self.assertEqual(SF.save_dir(), Path("/tmp/demo-shots"))

    def test_default_is_pictures_screenshots_under_home(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CAPSPLUS_SHOT_DIR", None)
            self.assertEqual(SF.save_dir(), Path.home() / "Pictures" / "Screenshots",
                             "默认落点按明江的要求是 ~/Pictures/Screenshots（不许写死某个家目录）")

    def test_filename_carries_a_timestamp(self):
        name = SF.shot_filename(datetime.datetime(2026, 10, 19, 11, 22, 33))
        self.assertEqual(name, "shot-2026-10-19-112233.png")

    def test_never_overwrites_an_existing_shot(self):
        d = temp_dir("capsplus-shot-test-")
        (d / "shot-2026-10-19-112233.png").write_bytes(PNG_1PX)
        picked = SF.unique_path(d, "shot-2026-10-19-112233.png")
        self.assertEqual(picked.name, "shot-2026-10-19-112233-1.png",
                         "同一秒连拍两张不许覆盖前一张")


if __name__ == "__main__":
    unittest.main()
