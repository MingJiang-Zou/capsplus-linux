# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：`bin/capsplus-shot` 的四条命令（区域 / 直抓 / 标注 / 贴图）与它们的**编排**。

为什么单开一份：第 1~4 步各自的细节由 `test_shot_frame/overlay/annotate/pin` 钉，这里钉的是
"**命令之间怎么接**"。全部走注入的假实现，**不需要真屏、不需要扩展、不需要注销重登**。

第二批 P0 之后 `--region` 的编排是（顺序不能改）：

    freeze（抓一张联合图，**不可见**）→ 遮罩（冻结图当背景）→ 从冻结图裁剪 → 标注窗

由此两条老口径**故意改了**，别当成回退：
  * **取消也会先抓一张**：冻结帧是遮罩的背景，没有它就只能靠透明露桌面（Wayland 下全黑盲选）。
    所以"取消之后一件事都不发生"改成"取消之后**不写文件、不开窗**，但冻结那一下已经发生"。
  * **失败判据从"抓帧失败"挪到"冻结失败"**：冻结失败必须**不开遮罩**、退出码非零 ——
    宁可告诉他"这次没成"，也不能给他一个全黑盲选的窗。

反例具型（删掉实现就红）：
  * 删掉 `if not rect: return 0` → `test_cancel_writes_nothing_and_opens_no_window` 红；
  * 删掉 `if not frames: … return 1` → `test_failed_freeze_refuses_to_open_the_overlay` 红；
  * 把 `crop_from_frames` 换成"再抓一帧" → `test_no_second_capture_after_confirm` 红。
"""

import contextlib
import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_capture as SC          # noqa: E402
from capsplus import shot_frame as SF            # noqa: E402

try:
    import cairo
    HAVE_CAIRO = True
except Exception:                                 # pragma: no cover - 无 pycairo 的机器
    cairo = None
    HAVE_CAIRO = False

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

class TestArgParsing(unittest.TestCase):
    def setUp(self):
        self._sink = contextlib.redirect_stdout(io.StringIO())
        self._sink.__enter__()
        self.addCleanup(self._sink.__exit__, None, None, None)

    def test_four_commands_parse(self):
        self.assertEqual(SC.parse_args(["--capture", "1", "2", "3", "4"]).capture, [1, 2, 3, 4])
        self.assertTrue(SC.parse_args(["--region"]).region)
        self.assertEqual(SC.parse_args(["--annotate", "/tmp/a.png"]).annotate, "/tmp/a.png")
        self.assertEqual(SC.parse_args(["--pin", "/tmp/a.png"]).pin, "/tmp/a.png")

    def test_at_is_optional_and_typed(self):
        self.assertIsNone(SC.parse_args(["--pin", "x"]).at)
        self.assertEqual(SC.parse_args(["--pin", "x", "--at", "5", "6"]).at, [5, 6])

    def test_usage_exit_code_is_two(self):
        self.assertEqual(SC.main([]), 2, "什么都不给 = 用法错误（退出码 2），不是静默成功")

    def test_annotate_missing_file_is_exit_two_not_crash(self):
        out = []
        with_stdout = SC.sys.stdout
        try:
            import io
            SC.sys.stdout, SC.sys.stderr = io.StringIO(), io.StringIO()
            code = SC.main(["--annotate", "/nonexistent/nope.png"])
            out.append(SC.sys.stderr.getvalue())
        finally:
            SC.sys.stdout, SC.sys.stderr = with_stdout, sys.stderr
        self.assertEqual(code, 2)
        self.assertIn("找不到", "".join(out))


@unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：region 现在的产物是从冻结图裁剪出来的")
class TestRegionFlow(unittest.TestCase):
    """`--region` 的编排：冻结 → 选区 → 裁剪 → 标注，四步之间不许乱序、不许漏。"""

    def setUp(self):
        self._sink = contextlib.redirect_stdout(io.StringIO())
        self._sink.__enter__()
        self.addCleanup(self._sink.__exit__, None, None, None)
        self.dir = temp_dir("capsplus-cli-test-")
        self.out = self.dir / "s.png"
        self.calls = []
        self.monitors = [(0, 0, 800, 600)]

    def _frames(self):
        from capsplus import shot_frames as SFR
        union = SFR.union_rect(self.monitors)
        return SFR.slice_frames(_pattern_surface(union[2], union[3]), self.monitors, 1.0)

    def _deps(self, rect, freeze_ok=True, annotate_result=("copy", None, "✓ stub 复制了")):
        from capsplus import shot_frames as SFR

        def capture(x, y, w, h, out):
            """假 shell：按请求尺寸画一张**有左右分区**的图案 PNG（跨屏裁剪靠它验内容来源）。"""
            self.calls.append(("freeze-capture", x, y, w, h))
            _write_pattern_png(out, w, h)
            return True, "✓ stub"

        def freeze(mons):
            self.calls.append(("freeze", tuple(mons[0])))
            if not freeze_ok:
                return [], (0, 0, 0, 0), "✗ stub 冻结失败"
            return SFR.freeze(mons, capture, out_dir=self.dir)

        def select(frames, mons):
            self.calls.append(("select", len(frames)))
            return rect, None       # 契约：`(rect, failure)`

        def annotate(path, on_pin, providers=None):
            self.calls.append(("annotate", str(path), providers is not None))
            return annotate_result

        return {"monitors": self.monitors, "capture": capture, "freeze": freeze, "select": select,
                "annotate": annotate, "no_pin": True}

    def test_freeze_happens_before_the_overlay_and_uses_the_union_rect(self):
        args = SC.parse_args(["--region", "--out", str(self.out)])
        code = SC.command_region(args, self._deps((100, 200, 300, 200)))
        self.assertEqual(code, 0)
        order = [c[0] for c in self.calls]
        self.assertLess(order.index("freeze"), order.index("select"),
                        "必须先冻结再开遮罩 —— 反过来就只能靠透明露桌面（P0 的病根）")
        self.assertIn(("freeze-capture", 0, 0, 800, 600), self.calls,
                      "冻结抓的是**所有屏的联合矩形**（跨屏选区就靠它）")
        self.assertEqual(self.calls[order.index("select")], ("select", 1),
                         "选区拿到的是切好的冻结帧")

    def test_selection_rect_is_cropped_verbatim_from_the_frames(self):
        args = SC.parse_args(["--region", "--out", str(self.out)])
        code = SC.command_region(args, self._deps((100, 200, 300, 200)))
        self.assertEqual(code, 0)
        self.assertTrue(self.out.exists(), "裁剪结果要落盘")
        surface = cairo.ImageSurface.create_from_png(str(self.out))
        self.assertEqual((surface.get_width(), surface.get_height()), (300, 200),
                         "输出尺寸必须等于选区尺寸（差一个像素就是另一个工具了）")
        self.assertIn(("annotate", str(self.out), True), self.calls)

    def test_no_second_capture_after_confirm(self):
        """确认后从**冻结图**裁剪：不许再抓一次（那会把遮罩自己拍进去，也正是旧 settle 想遮的）。"""
        args = SC.parse_args(["--region", "--out", str(self.out)])
        SC.command_region(args, self._deps((0, 0, 10, 10)))
        captures = [c for c in self.calls if c[0] == "freeze-capture"]
        self.assertEqual(len(captures), 1, f"抓帧只该发生一次（冻结那一次），实际 {captures}")

    def test_cancel_writes_nothing_and_opens_no_window(self):
        args = SC.parse_args(["--region", "--out", str(self.out)])
        code = SC.command_region(args, self._deps(None))
        self.assertEqual(code, 0, "取消是用户的意图，不是错误（退出码 0）")
        self.assertEqual([c[0] for c in self.calls if c[0] != "freeze-capture"],
                         ["freeze", "select"],
                         "取消之后不许写文件、不许开标注窗（冻结那一下已经发生，这是设计）")
        self.assertFalse(self.out.exists(), "取消不许留下文件")

    def test_failed_freeze_refuses_to_open_the_overlay(self):
        """冻结失败 = **绝不开遮罩**。给他一个全黑盲选的窗，比直接说"这次没成"更糟。"""
        args = SC.parse_args(["--region", "--out", str(self.out)])
        code = SC.command_region(args, self._deps((0, 0, 10, 10), freeze_ok=False))
        self.assertEqual(code, 1, "冻结失败必须非零退出（脚本里靠它判断）")
        self.assertEqual([c[0] for c in self.calls], ["freeze"],
                         "冻结失败之后不许开遮罩、不许开标注窗")

    def test_no_annotate_switches_the_window_off(self):
        args = SC.parse_args(["--region", "--no-annotate", "--out", str(self.out)])
        code = SC.command_region(args, self._deps((0, 0, 10, 10)))
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.calls if c[0] == "annotate"], [])
        self.assertTrue(self.out.exists(), "--no-annotate 也要把图落盘（不然这一下白按了）")

    def test_region_writes_to_an_autosave_path_when_out_is_missing(self):
        """没给 --out 时落点是默认相册目录 —— 判据里用 CAPSPLUS_SHOT_DIR 把它引到临时目录，
        **绝不让测试往 ~/Pictures/Screenshots 里写东西**。"""
        args = SC.parse_args(["--region"])
        deps = self._deps((0, 0, 10, 10))
        with mock.patch.dict(os.environ, {"CAPSPLUS_SHOT_DIR": str(self.dir)}):
            self.assertEqual(SC.command_region(args, deps), 0)
            written = [c for c in self.calls if c[0] == "annotate"][0]
            self.assertEqual(pathlib.Path(written[1]).parent, self.dir)

    def test_cross_screen_selection_is_cropped_from_both_frames(self):
        """跨屏选区：输出尺寸 = 选区尺寸，且左右两半**分别来自两块屏的冻结图**。

        这条是"内容来自冻结图"的端到端版：假 shell 画的是**左蓝右绿**的联合图，
        选区 700..900 横跨两块屏 ⇒ 输出必须左蓝右绿。要是哪天有人把背景改回"透明+压暗"、
        或者拼接只用了第一块屏，这里立刻红。
        """
        self.monitors = [(0, 0, 800, 600), (800, 0, 800, 600)]
        deps = self._deps((700, 100, 200, 100))
        deps["monitors"] = self.monitors
        args = SC.parse_args(["--region", "--out", str(self.out)])
        self.assertEqual(SC.command_region(args, deps), 0)
        surface = cairo.ImageSurface.create_from_png(str(self.out))
        self.assertEqual((surface.get_width(), surface.get_height()), (200, 100))
        left = _px(surface, 10, 50)
        right = _px(surface, 190, 50)
        self.assertGreater(left[2], left[1], f"左半边该来自第一块屏（蓝），实际 {left}")
        self.assertGreater(right[1], right[2], f"右半边该来自第二块屏（绿），实际 {right}")

    @unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真渲染一次标注结果")
    def test_pin_hook_pins_the_rendered_result(self):
        """标注窗上"钉到屏幕上"：必须拿**渲染后的图**（带标注）去钉，而不是原图。"""
        pinned = []
        args = SC.parse_args(["--region", "--out", str(self.out)])
        deps = self._deps((0, 0, 10, 10))
        deps["pin"] = lambda path, x=None, y=None: (pinned.append(str(path)), (True, "✓ 钉上了"))[1]
        deps.pop("no_pin")

        def annotate(path, on_pin, providers=None):
            surface = _fake_surface()
            self.calls.append(("annotate", str(path), on_pin(surface)[0]))
            return "pin", None, "✓ stub"

        deps["annotate"] = annotate
        code = SC.command_region(args, deps)
        self.assertEqual(code, 0)
        self.assertTrue(pinned, "钉屏回调没被调用")
        self.assertTrue(pinned[0].endswith(".pinned.png"),
                        "钉的应该是渲染后的产物（.pinned.png），不是原始截图")
        self.assertIn(("annotate", str(self.out), True), self.calls)


def _pattern_surface(w: int, h: int):
    """一张"看得出来的图案"当假冻结图：跨屏裁剪的判据靠它的像素分布验内容来源。"""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    cr.set_source_rgb(0.2, 0.4, 0.8)
    cr.paint()
    return surface


def _write_pattern_png(path, w: int, h: int) -> None:
    """假 shell 的产物：**左半蓝、右半绿**（跨屏拼接时一眼看出哪半边来自哪块屏）。"""
    if not HAVE_CAIRO:                                # pragma: no cover - 无 pycairo 的机器
        pathlib.Path(path).write_bytes(PNG_1PX)
        return
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, w), max(1, h))
    cr = cairo.Context(surface)
    cr.set_source_rgb(0.2, 0.4, 0.8)
    cr.rectangle(0, 0, w / 2, h)
    cr.fill()
    cr.set_source_rgb(0.2, 0.8, 0.4)
    cr.rectangle(w / 2, 0, w / 2, h)
    cr.fill()
    surface.write_to_png(str(path))


def _px(surface, x: int, y: int):
    """读一个像素（ARGB32 是预乘的，这里只看相对大小，够判颜色）。"""
    surface.flush()
    data = bytes(surface.get_data())
    stride = surface.get_stride()
    off = y * stride + x * 4
    b, g, r, _a = data[off], data[off + 1], data[off + 2], data[off + 3]
    return (r, g, b)


def _fake_surface():
    """一个最小可用的 cairo surface（判据里假装是标注结果）。"""
    import cairo
    return cairo.ImageSurface(cairo.FORMAT_ARGB32, 2, 2)


class TestPinCommand(unittest.TestCase):
    def setUp(self):
        self._sink = contextlib.redirect_stdout(io.StringIO())
        self._sink.__enter__()
        self.addCleanup(self._sink.__exit__, None, None, None)

    def test_pin_command_passes_path_and_position(self):
        seen = {}
        args = SC.parse_args(["--pin", "/tmp/x.png", "--at", "7", "8"])

        def pin_fn(path, x=None, y=None):
            seen.update(path=path, x=x, y=y)
            return True, "✓"

        self.assertEqual(SC.command_pin(args, {"pin": pin_fn}), 0)
        self.assertEqual(seen, {"path": "/tmp/x.png", "x": 7, "y": 8})

    def test_pin_failure_is_nonzero(self):
        args = SC.parse_args(["--pin", "/tmp/x.png"])
        self.assertEqual(SC.command_pin(args, {"pin": lambda p, x=None, y=None: (False, "✗ 不行")}), 1)


if __name__ == "__main__":
    unittest.main()