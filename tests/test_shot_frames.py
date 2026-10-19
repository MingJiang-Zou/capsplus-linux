# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：冻结帧（第二批 P0）—— 抓联合图、按屏切、按选区裁、贴边吸附、忙等重试。

关键口径（每条都有红法）：
  * **只抓一次**：`freeze()` 就一次 `capture_fn` 往返（多屏、跨屏选区都不加次数）；
  * **"同一时刻只允许一个截图操作"是正常情况**（用户连按 / 热键连击）⇒ 按标记重试，
    而不是一次就告诉用户抓不了；
  * **裁剪内容来自冻结图**：跨屏选区左右两半必须分别来自两块屏（图案判据，不看 alpha）；
  * 缩放按"图有多大"反推（本机 =1；2× 屏上要按 2 倍算像素），**不硬编码**。
"""

import os
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_frames as SFR        # noqa: E402

try:
    import cairo
    HAVE_CAIRO = True
except Exception:
    cairo = None
    HAVE_CAIRO = False

BLUE = (0.2, 0.4, 0.9)
GREEN = (0.1, 0.85, 0.3)


def pattern_png(path, w: int, h: int) -> None:
    """假 shell 的产物：左半蓝、右半绿（一眼看出哪半边来自哪块屏/哪个区域）。"""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, w), max(1, h))
    cr = cairo.Context(surface)
    cr.set_source_rgb(*BLUE)
    cr.rectangle(0, 0, w / 2, h)
    cr.fill()
    cr.set_source_rgb(*GREEN)
    cr.rectangle(w / 2, 0, w / 2, h)
    cr.fill()
    surface.write_to_png(str(path))


def px(surface, x: int, y: int):
    surface.flush()
    data = bytes(surface.get_data())
    off = y * surface.get_stride() + x * 4
    return (data[off + 2], data[off + 1], data[off], data[off + 3])


class FakeShell:
    """假 shell：记下每次抓帧请求，按请求尺寸吐一张图案 PNG。"""

    def __init__(self, fail_times=0, busy=False, real_size=None, workdir=None):
        self.calls = []
        self.fail_times = fail_times
        self.busy = busy
        self.real_size = real_size
        self.workdir = workdir

    def __call__(self, x, y, w, h, out):
        self.calls.append((x, y, w, h))
        if self.busy and len(self.calls) <= self.fail_times:
            return False, f"✗ 抓帧超时：经过 3000ms 仍未完成 —— journal 里 why={SFR.BUSY_MARKER} is permitted"
        if self.fail_times and not self.busy and len(self.calls) <= self.fail_times:
            return False, "✗ 抓帧超时：经过 3000ms 仍未完成"
        pw, ph = self.real_size or (w, h)
        pattern_png(out, pw, ph)
        return True, f"✓ {out}（stub）"


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：冻结帧全靠 cairo")
class TestFreeze(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-frames-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.monitors = [(0, 0, 800, 600), (800, 0, 800, 600)]

    def test_one_capture_for_all_screens(self):
        shell = FakeShell()
        frames, union, msg = SFR.freeze(self.monitors, shell, out_dir=self.dir)
        self.assertEqual(len(shell.calls), 1, "多屏也只抓一次（一次 D-Bus 往返）")
        self.assertEqual(shell.calls[0], (0, 0, 1600, 600), "抓的是所有屏的联合矩形")
        self.assertEqual(union, (0, 0, 1600, 600))
        self.assertEqual(len(frames), 2, "每块屏一块冻结图")
        self.assertIn("1600×600", msg)

    def test_frames_carry_their_own_rect_and_pixels(self):
        frames, _union, _msg = SFR.freeze(self.monitors, FakeShell(), out_dir=self.dir)
        self.assertEqual([f.rect for f in frames], self.monitors)
        self.assertEqual(frames[0].pixel_size, (800, 600))
        self.assertEqual(frames[1].pixel_size, (800, 600))

    def test_scale_is_derived_from_the_returned_image(self):
        """2× 屏：抓回来是双倍像素 —— 缩放按「图有多大」反推，不许硬编码 1。"""
        shell = FakeShell(real_size=(3200, 1200))
        frames, _union, msg = SFR.freeze(self.monitors, shell, out_dir=self.dir)
        self.assertEqual(frames[0].scale, 2.0)
        self.assertEqual(frames[0].pixel_size, (1600, 1200))
        self.assertIn("@2x", msg)

    def test_busy_is_retried_not_reported_as_failure(self):
        """「同一时刻只允许一个截图操作」= 用户连按，属正常：重试几次就该成功。"""
        shell = FakeShell(busy=True, fail_times=2)
        frames, _union, _msg = SFR.freeze(self.monitors, shell, out_dir=self.dir,
                                          sleep=lambda _s: None)
        self.assertEqual(len(frames), 2, "忙等要重试，不该直接判失败")
        self.assertEqual(len(shell.calls), 3, f"重试次数不对：{len(shell.calls)}")

    def test_other_failures_are_reported_immediately(self):
        shell = FakeShell(fail_times=99)
        frames, _union, msg = SFR.freeze(self.monitors, shell, out_dir=self.dir,
                                         sleep=lambda _s: None)
        self.assertEqual(frames, [])
        self.assertIn("抓帧超时", msg)
        self.assertEqual(len(shell.calls), 1, "不是忙等就别傻重试")

    def test_busy_gives_up_after_the_retry_budget(self):
        shell = FakeShell(busy=True, fail_times=99)
        frames, _union, msg = SFR.freeze(self.monitors, shell, out_dir=self.dir, retries=3,
                                         sleep=lambda _s: None)
        self.assertEqual(frames, [])
        self.assertEqual(len(shell.calls), 3)
        self.assertIn(SFR.BUSY_MARKER, msg)

    def test_no_monitors_is_reported_plainly(self):
        frames, union, msg = SFR.freeze([], FakeShell(), out_dir=self.dir)
        self.assertEqual(frames, [])
        self.assertEqual(union, (0, 0, 0, 0))
        self.assertIn("显示器几何", msg)


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：裁剪靠 cairo")
class TestCrop(unittest.TestCase):
    def setUp(self):
        self.monitors = [(0, 0, 800, 600), (800, 0, 800, 600)]
        union = SFR.union_rect(self.monitors)
        self.big = cairo.ImageSurface(cairo.FORMAT_ARGB32, union[2], union[3])
        cr = cairo.Context(self.big)
        cr.set_source_rgb(*BLUE)
        cr.rectangle(0, 0, 800, 600)
        cr.fill()
        cr.set_source_rgb(*GREEN)
        cr.rectangle(800, 0, 800, 600)
        cr.fill()
        self.frames = SFR.slice_frames(self.big, self.monitors, 1.0)

    def test_single_screen_crop(self):
        out = SFR.crop_from_frames((100, 100, 300, 200), self.frames)
        self.assertEqual((out.get_width(), out.get_height()), (300, 200))
        self.assertGreater(px(out, 10, 10)[2], 0.7, "第一块屏是蓝的")

    def test_cross_screen_crop_stitches_both(self):
        out = SFR.crop_from_frames((700, 100, 200, 100), self.frames)
        self.assertEqual((out.get_width(), out.get_height()), (200, 100))
        self.assertGreater(px(out, 10, 50)[2], px(out, 10, 50)[1], "左半来自第一块屏（蓝）")
        self.assertGreater(px(out, 190, 50)[1], px(out, 190, 50)[2], "右半来自第二块屏（绿）")

    def test_scaled_screens_crop_at_the_right_pixels(self):
        """2× 的两块屏：输出体积翻倍，内容位置也不能错。"""
        big = cairo.ImageSurface(cairo.FORMAT_ARGB32, 3200, 1200)
        cr = cairo.Context(big)
        cr.set_source_rgb(*BLUE)
        cr.rectangle(0, 0, 1600, 1200)
        cr.fill()
        cr.set_source_rgb(*GREEN)
        cr.rectangle(1600, 0, 1600, 1200)
        cr.fill()
        frames = SFR.slice_frames(big, self.monitors, 2.0)
        out = SFR.crop_from_frames((700, 100, 200, 100), frames)
        self.assertEqual((out.get_width(), out.get_height()), (400, 200), "2× 下像素翻倍")
        self.assertGreater(px(out, 20, 100)[2], px(out, 20, 100)[1])
        self.assertGreater(px(out, 380, 100)[1], px(out, 380, 100)[2])

    def test_degenerate_rect_is_refused(self):
        for bad in ((10, 10, 0, 10), (10, 10, 10, 0)):
            with self.assertRaises(ValueError):
                SFR.crop_from_frames(bad, self.frames)

    def test_rect_far_away_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            SFR.crop_from_frames((9000, 9000, 10, 10), self.frames)


class TestSnap(unittest.TestCase):
    def setUp(self):
        self.monitors = [(0, 0, 800, 600), (800, 0, 800, 600)]

    def test_snaps_to_a_monitor_edge(self):
        self.assertEqual(SFR.snap_rect((3, 5, 700, 500), self.monitors), (0, 0, 703, 505))

    def test_snaps_to_another_monitors_edge(self):
        """跨屏拖拽时"吸到相邻屏的边界"最常用：右边缘 805 离 800 只有 5px ⇒ 吸上去。"""
        self.assertEqual(SFR.snap_rect((700, 100, 105, 200), self.monitors), (700, 100, 100, 200))

    def test_snap_is_deterministic(self):
        """候选边排序后再试：同输入必须同输出（set 迭代顺序不定会让"有时吸有时不吸"）。"""
        rect = (795, 5, 700, 500)
        results = {SFR.snap_rect(rect, self.monitors) for _ in range(20)}
        self.assertEqual(len(results), 1, f"吸附结果不稳定：{results}")
        self.assertEqual(results.pop(), (800, 0, 695, 505),
                         "左边缘从 795 吸到 800 ⇒ 宽度少 5px（右边缘不动，才叫吸附）")

    def test_far_from_any_edge_is_left_alone(self):
        self.assertEqual(SFR.snap_rect((100, 100, 200, 200), self.monitors), (100, 100, 200, 200))

    def test_tolerance_is_configurable(self):
        self.assertEqual(SFR.snap_rect((20, 20, 100, 100), self.monitors, tolerance=0),
                         (20, 20, 100, 100))

    def test_degenerate_rect_passes_through(self):
        self.assertEqual(SFR.snap_rect((1, 1, 0, 0), self.monitors), (1, 1, 0, 0))
        self.assertIsNone(SFR.snap_rect(None, self.monitors))

    def test_snap_never_produces_a_zero_size(self):
        x, y, w, h = SFR.snap_rect((0, 0, 1, 1), self.monitors)
        self.assertGreaterEqual(w, 1)
        self.assertGreaterEqual(h, 1)


class TestHelpers(unittest.TestCase):
    def test_union_rect_of_scattered_monitors(self):
        self.assertEqual(SFR.union_rect([(0, 0, 100, 100), (200, 50, 100, 100), (-100, 0, 50, 50)]),
                         (-100, 0, 400, 150))

    def test_derive_scale(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1600, 600) if HAVE_CAIRO else None
        if surface is None:
            self.skipTest("没有 pycairo")
        self.assertEqual(SFR.derive_scale((0, 0, 1600, 600), surface), 1.0)
        self.assertEqual(SFR.derive_scale((0, 0, 0, 0), surface), 1.0)

    def test_runtime_dir_honours_xdg(self):
        old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = "/tmp/xyz-runtime"
        try:
            self.assertEqual(str(SFR.runtime_dir()), "/tmp/xyz-runtime/capsplus-shot")
        finally:
            if old is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = old


if __name__ == "__main__":
    unittest.main()