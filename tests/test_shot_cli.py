# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：`bin/capsplus-shot` 的四条命令（区域 / 直抓 / 标注 / 贴图）与它们的**编排**。

为什么单开一份：第 1~4 步各自的细节由 `test_shot_frame/overlay/annotate/pin` 钉，这里钉的是
"**命令之间怎么接**"——选区取消不许抓图、抓帧失败不许开标注窗、`--region` 必须把选区坐标原样交给
抓帧、钉屏失败必须退出码非零。全部走注入的假实现，**不需要真屏、不需要扩展、不需要注销重登**。

反例具型（删掉实现就红）：
  * 把 `command_region` 里的 `if not rect: return 0` 删掉 → `test_cancel_never_captures` 红；
  * 把 `if not ok: return 1` 删掉 → `test_failed_capture_does_not_open_the_annotator` 红；
  * 把退出码写成恒 0 → 两处"退出码"判据红。
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


class TestRegionFlow(unittest.TestCase):
    def setUp(self):
        self._sink = contextlib.redirect_stdout(io.StringIO())
        self._sink.__enter__()
        self.addCleanup(self._sink.__exit__, None, None, None)
        self.dir = temp_dir("capsplus-cli-test-")
        self.out = self.dir / "s.png"
        self.calls = []

    def _deps(self, rect, ok=True, annotate_result=("copy", None, "✓ stub 复制了")):
        def capture(x, y, w, h, out):
            self.calls.append(("capture", x, y, w, h, str(out)))
            if ok:
                pathlib.Path(out).write_bytes(PNG_1PX)
            return ok, "✓ stub"

        def annotate(path, on_pin):
            self.calls.append(("annotate", str(path)))
            return annotate_result

        return {"select": lambda: rect, "capture": capture, "annotate": annotate,
                "no_pin": True}

    def test_selection_rect_is_handed_to_capture_verbatim(self):
        args = SC.parse_args(["--region", "--out", str(self.out)])
        code = SC.command_region(args, self._deps((100, 200, 640, 480)))
        self.assertEqual(code, 0)
        self.assertIn(("capture", 100, 200, 640, 480, str(self.out)), self.calls,
                      "选区坐标必须原样交给抓帧（差一个像素就是另一个工具了）")
        self.assertIn(("annotate", str(self.out)), self.calls,
                      "抓完要接着开标注窗（第 3 步的入口就在这儿）")

    def test_cancel_never_captures(self):
        args = SC.parse_args(["--region", "--out", str(self.out)])
        code = SC.command_region(args, self._deps(None))
        self.assertEqual(code, 0, "取消是用户的意图，不是错误（退出码 0）")
        self.assertEqual(self.calls, [], "取消之后一件事都不该发生：不抓帧、不开窗")

    def test_failed_capture_does_not_open_the_annotator(self):
        args = SC.parse_args(["--region", "--out", str(self.out)])
        code = SC.command_region(args, self._deps((0, 0, 10, 10), ok=False))
        self.assertEqual(code, 1, "抓帧失败必须非零退出（脚本里靠它判断）")
        self.assertEqual([c[0] for c in self.calls], ["capture"],
                         "抓帧失败还开标注窗 = 让人对着空白画布发懵")

    def test_no_annotate_switches_the_window_off(self):
        args = SC.parse_args(["--region", "--no-annotate", "--out", str(self.out)])
        code = SC.command_region(args, self._deps((0, 0, 10, 10)))
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.calls], ["capture"])

    def test_region_writes_to_an_autosave_path_when_out_is_missing(self):
        """没给 --out 时落点是默认相册目录 —— 判据里用 CAPSPLUS_SHOT_DIR 把它引到临时目录，
        **绝不让测试往 ~/Pictures/Screenshots 里写东西**。"""
        args = SC.parse_args(["--region"])
        deps = self._deps((0, 0, 10, 10))
        with mock.patch.dict(os.environ, {"CAPSPLUS_SHOT_DIR": str(self.dir)}):
            self.assertEqual(SC.command_region(args, deps), 0)
            captured = [c for c in self.calls if c[0] == "capture"][0]
            self.assertEqual(pathlib.Path(captured[-1]).parent, self.dir)

    @unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真渲染一次标注结果")
    def test_pin_hook_pins_the_rendered_result(self):
        """标注窗上"钉到屏幕上"：必须拿**渲染后的图**（带标注）去钉，而不是原图。"""
        pinned = []
        args = SC.parse_args(["--region", "--out", str(self.out)])
        deps = self._deps((0, 0, 10, 10))
        deps["pin"] = lambda path, x=None, y=None: (pinned.append(str(path)), (True, "✓ 钉上了"))[1]
        deps.pop("no_pin")

        def annotate(path, on_pin):
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