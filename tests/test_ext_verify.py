# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：`tools/verify-after-login.sh` / `capsplus/extverify.py` 的七项判词。

为什么这批判据非有不可（2026-10-20）：跑这条命令时**扩展正好是 ERROR 态**，所以现场拿到的
全是 FAIL —— 也就是说"PASS 那一半从来没被人走到过"。只红不绿的分支等于没验过，
所以这里用**注入依赖**把每一项的正例也钉住：不碰真屏、不起真 shell、不发真信号。

两条最要紧的（都是这次真踩到的形态）：
  * `test_capture_timeout_does_not_retry_regions` —— 抓帧超时是"通道死了"，换 region 再试
    只会让人白等 4×5 秒（第一版实测 20 秒就是这么来的）；
  * `test_mask_traceback_is_a_failure_not_a_pass` —— **假绿陷阱**：GTK 回调抛异常不会让进程
    非零退出，窗口照样 map、几何自检照样过，于是"遮罩是坏的"却报 PASS。
"""

import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import extverify as EV          # noqa: E402


def cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


READY_GOOD = ("10月 20 21:00:00 host gnome-shell[100]: [raise-browser] ready capture=on pin=on "
              "clear=on toolbar+menu+modal=on promisified=true default=microsoft-edge.desktop "
              "windows=0 on-current=0\n")
PROBE_LINE = "10月 20 21:00:01 host gnome-shell[100]: [raise-browser] probe default=x windows=1 on-current=1\n"


class TestShellUnit(unittest.TestCase):
    def test_active_shell_is_pass(self):
        rows = cp([], 0, "org.gnome.Shell@ubuntu.service loaded active running GNOME Shell\n")
        self.assertEqual(EV.shell_unit_state(runner=lambda _a: rows)[0], EV.PASS)

    def test_no_unit_at_all_is_fail(self):
        self.assertEqual(EV.shell_unit_state(runner=lambda _a: cp([], 0, ""))[0], EV.FAIL)

    def test_unit_present_but_not_running_is_fail(self):
        rows = cp([], 0, "org.gnome.Shell@ubuntu.service loaded inactive dead GNOME Shell\n")
        state, why = EV.shell_unit_state(runner=lambda _a: rows)
        self.assertEqual(state, EV.FAIL)
        self.assertIn("没跑起来", why)


class TestReadyLine(unittest.TestCase):
    def test_ready_line_with_channels_is_pass(self):
        self.assertEqual(EV.ready_line_state(READY_GOOD)[0], EV.PASS)

    def test_missing_ready_line_is_fail(self):
        state, why = EV.ready_line_state("别的日志\n")
        self.assertEqual(state, EV.FAIL)
        self.assertIn("没有", why)

    def test_ready_line_without_channels_is_fail(self):
        """加载成功但订阅没挂上 —— 现象与"没装"一模一样，必须抓住。"""
        state, why = EV.ready_line_state("[raise-browser] ready capture=off pin=off\n")
        self.assertEqual(state, EV.FAIL)
        self.assertIn("capture=on", why)

    def test_the_real_boot_log_of_2026_10_20_is_fail(self):
        """**现场原样**：那次事故在本机 journal 里留下的是这样一行（没有 ready，只有 import 错）。"""
        real = ("10月 20 17:35:34 host gnome-shell[7532]: Extension raise-browser@capsplus.example: "
                "TypeError: malformed UTF-8 character sequence at offset 8190\n")
        self.assertEqual(EV.ready_line_state(real)[0], EV.FAIL)


class TestBusName(unittest.TestCase):
    def test_owned_is_pass(self):
        state, _ = EV.name_owned_state(runner=lambda _a: cp([], 0, "(true,)\n"))
        self.assertEqual(state, EV.PASS)

    def test_not_owned_is_fail(self):
        state, why = EV.name_owned_state(runner=lambda _a: cp([], 0, "(false,)\n"))
        self.assertEqual(state, EV.FAIL)
        self.assertIn("没人占", why)

    def test_serviceunknown_is_fail(self):
        """ERROR 态时的实测回话就是这句 —— 它也是 FAIL，不能因为"有输出"就当成功。"""
        state, _ = EV.name_owned_state(runner=lambda _a: cp(
            [], 1, "", "错误：GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown\n"))
        self.assertEqual(state, EV.FAIL)


class TestProbeRoundtrip(unittest.TestCase):
    def test_journal_growth_is_pass(self):
        seen = {"n": 0}

        def journal():
            return READY_GOOD + (PROBE_LINE if seen["n"] else "")

        def emit():
            seen["n"] = 1

        state, why = EV.probe_roundtrip_state(journal, emit, sleep_fn=lambda _s: None)
        self.assertEqual(state, EV.PASS)
        self.assertIn("0 → 1", why)

    def test_no_growth_is_fail(self):
        state, why = EV.probe_roundtrip_state(lambda: READY_GOOD, lambda: None,
                                             sleep_fn=lambda _s: None)
        self.assertEqual(state, EV.FAIL)
        self.assertIn("没有增加", why)

    def test_emit_return_code_is_not_evidence(self):
        """`gdbus emit` 在没人听的时候**照样 exit 0** —— 只有 journal 增量算数。"""
        calls = []

        def emit():
            calls.append(1)
        state, _ = EV.probe_roundtrip_state(lambda: "", emit, sleep_fn=lambda _s: None)
        self.assertEqual(state, EV.FAIL)
        self.assertEqual(len(calls), 1, "emit 只该发一次")


class TestPixels(unittest.TestCase):
    def test_uniform_is_one_colour(self):
        self.assertEqual(EV.distinct_pixels(bytes([10, 20, 30, 255]) * 100, 10, 10), 1)

    def test_two_colours(self):
        raw = bytes([0, 0, 0, 255]) * 50 + bytes([255, 255, 255, 255]) * 50
        self.assertEqual(EV.distinct_pixels(raw, 10, 10), 2)

    def test_empty_is_zero(self):
        self.assertEqual(EV.distinct_pixels(b"", 0, 0), 0)

    def test_only_reads_width_height_pixels(self):
        """尾部多出来的字节（凑整/填充）不许算进颜色 —— 否则会数出假的"非纯色"。"""
        raw = bytes([1, 2, 3, 4]) * 4 + bytes([9, 9, 9, 9]) * 8
        self.assertEqual(EV.distinct_pixels(raw, 2, 2), 1)


class TestCapture(unittest.TestCase):
    def _dir(self):
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-verify-test-"))
        self.addCleanup(__import__("shutil").rmtree, d, True)
        return d

    def test_non_uniform_frame_is_pass(self):
        calls = []

        def capture(x, y, w, h, out):
            calls.append((x, y, w, h))
            pathlib.Path(out).write_bytes(b"x")
            return True, "ok"

        state, why = EV.capture_state(self._dir(), capture, lambda: [(0, 0, 1920, 1080)],
                                      lambda _p: (bytes([0, 0, 0, 255, 255, 0, 0, 255]), 2, 1))
        self.assertEqual(state, EV.PASS)
        self.assertIn("非纯色", why)
        self.assertEqual(len(calls), 1)

    def test_timeout_does_not_retry_regions(self):
        """超时 = 通道死了：换 region 再试只会白等（第一版实测耗 20 秒就是这条）。"""
        calls = []

        def capture(x, y, w, h, out):
            calls.append((x, y, w, h))
            return False, "✗ 5.0s 内没等到产物"

        state, why = EV.capture_state(self._dir(), capture,
                                      lambda: [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)],
                                      lambda _p: (b"", 0, 0))
        self.assertEqual(state, EV.FAIL)
        self.assertEqual(len(calls), 1, f"超时不该换 region 重试，实际试了 {calls}")
        self.assertIn("超时不换 region", why)

    def test_uniform_frame_falls_through_to_the_next_region(self):
        """纯色**才**该换 region（那是"选了个空 region"，换一块就有）。"""
        calls = []
        # 第一次返回纯色（4 个像素全同），第二次返回两种颜色
        colours = {1: bytes([1, 1, 1, 1]) * 4,
                   2: bytes([1, 1, 1, 1, 2, 2, 2, 2])}

        def capture(x, y, w, h, out):
            calls.append((x, y))
            pathlib.Path(out).write_bytes(b"x")
            return True, "ok"

        state, why = EV.capture_state(self._dir(), capture,
                                      lambda: [(0, 0, 1920, 1080)],
                                      lambda _p: (colours[len(calls)], 4, 1))
        self.assertEqual(state, EV.PASS)
        self.assertEqual(len(calls), 2, "第一次纯色之后该再试一块 region")
        self.assertIn("非纯色", why)

    def test_no_monitors_still_tries_a_region(self):
        self.assertTrue(EV.regions_to_try([]))


class TestPinClear(unittest.TestCase):
    def test_both_ok_is_pass(self):
        state, _ = EV.pin_clear_state(pathlib.Path("/dev/null"),
                                      lambda _p: (True, "✓ 已钉到 (40, 40)：200×150，共 1 张"),
                                      lambda: (True, "✓ 已清空（cleared=1）"))
        self.assertEqual(state, EV.PASS)

    def test_pin_failure_is_fail(self):
        state, why = EV.pin_clear_state(pathlib.Path("/dev/null"),
                                        lambda _p: (False, "✗ 没等到 ack"),
                                        lambda: (True, ""))
        self.assertEqual(state, EV.FAIL)
        self.assertIn("贴图没成", why)

    def test_clear_failure_is_fail(self):
        state, why = EV.pin_clear_state(pathlib.Path("/dev/null"),
                                        lambda _p: (True, "✓ ok"),
                                        lambda: (False, "✗ 没等到 ack"))
        self.assertEqual(state, EV.FAIL)
        self.assertIn("清贴图没成", why)

    def test_no_frame_means_skip_not_fail(self):
        state, _ = EV.pin_clear_state(None, None, None)
        self.assertEqual(state, EV.SKIP)


class TestMask(unittest.TestCase):
    """遮罩那一步：**必须在 xvfb 里跑**，并且不许被假绿骗过去。"""

    WHICH = staticmethod(lambda name: "/usr/bin/" + name)

    def test_ok_is_pass(self):
        runner = lambda _a: cp([], 0, "MASK_OK 1 块屏：建窗 → 几何自检通过 → 收窗\n")
        state, _ = EV.mask_state(runner=runner, which=self.WHICH)
        self.assertEqual(state, EV.PASS)

    def test_traceback_is_a_failure_not_a_pass(self):
        """**假绿陷阱**（2026-10-20 实测踩到）：窗口 map 了、几何过了，但每帧 draw 都在抛异常。

        GTK 回调抛异常不会让进程非零退出 ⇒ 光看 `MASK_OK` 会得到 PASS，而遮罩其实是坏的。
        """
        text = ("MASK_OK 1 块屏：建窗 → 几何自检通过 → 收窗\n"
                "Traceback (most recent call last):\n"
                '  File "/repo/capsplus/shot_overlay.py", line 790, in _frame_for\n'
                "AttributeError: 'cairo.ImageSurface' object has no attribute 'rect'\n")
        state, why = EV.mask_state(runner=lambda _a: cp([], 0, text), which=self.WHICH)
        self.assertEqual(state, EV.FAIL)
        self.assertIn("假绿", why)

    def test_refusing_on_a_real_display_is_fail(self):
        """驱动自己查到后端是 Wayland 就拒跑（退出 2）—— 那意味着差点弹到真屏上。"""
        runner = lambda _a: cp([], 2, "MASK_REFUSE 这个显示后端是 MetaWaylandCompositor\n")
        state, why = EV.mask_state(runner=runner, which=self.WHICH)
        self.assertEqual(state, EV.FAIL)
        self.assertIn("MASK_REFUSE", why)

    def test_no_xvfb_is_skip(self):
        state, why = EV.mask_state(runner=lambda _a: cp([], 0, ""),
                                   which=lambda _n: None)
        self.assertEqual(state, EV.SKIP)
        self.assertIn("xvfb-run", why)

    def test_driver_refuses_wayland_before_building_anything(self):
        """驱动里那道安全阀必须**在拿显示器几何/建窗之前**：真机上连窗都不许建。"""
        code = EV.MASK_DRIVER
        self.assertIn("Wayland", code)
        self.assertLess(code.index("Wayland"), code.index("monitor_rects()"),
                        "查后端的动作必须排在拿显示器几何/建窗之前")
        self.assertIn("Overlay(", code)

    def test_the_isolated_pose_is_exactly_the_repo_standard(self):
        """xvfb 那一路必须是本仓界面的标准姿势：显式给 CAPSPLUS_GUI=1、GDK_BACKEND=x11、
        并且 `-u WAYLAND_DISPLAY`（只去掉这一个是不够的：libwayland 会退回 wayland-0）。"""
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return cp(argv, 0, "MASK_OK ok\n")

        EV.mask_state(runner=runner, which=self.WHICH)
        argv = seen["argv"]
        joined = " ".join(argv)
        self.assertIn("xvfb-run", argv[0])
        self.assertIn("WAYLAND_DISPLAY", argv, "必须显式 -u WAYLAND_DISPLAY")
        self.assertIn("GDK_BACKEND=x11", argv)
        self.assertIn("CAPSPLUS_GUI=1", argv, "界面类的副作用要显式授权才跑（本仓纪律）")
        self.assertIn("screen 0 1024x768x24", joined)


class TestTableAndWholeRun(unittest.TestCase):
    def test_table_counts_are_right(self):
        import io
        rows = [(EV.PASS, "a"), (EV.FAIL, "b"), (EV.SKIP, "c")]
        text = EV.render_table(rows, when="X", stream=io.StringIO())
        self.assertIn("PASS 1 / FAIL 1 / SKIP 1", text)
        self.assertIn("FAIL ——", text)
        self.assertIn("下一步", text)

    def test_all_green_says_pass_and_has_no_next_step(self):
        import io
        rows = [(EV.PASS, "ok")] * 7
        text = EV.render_table(rows, when="X", stream=io.StringIO())
        self.assertIn("PASS ——", text)
        self.assertNotIn("下一步", text)

    def test_run_all_with_all_deps_injected_gives_seven_rows(self):
        """**正例**：七项全绿的那张表长什么样 —— 这条是"PASS 那一半"的凭据。"""
        seen = {"emitted": False}

        def journal():
            return READY_GOOD + (PROBE_LINE if seen["emitted"] else "")

        def emit():
            seen["emitted"] = True

        def capture(x, y, w, h, out):
            pathlib.Path(out).write_bytes(b"x")
            return True, "ok"

        rows = EV.run_all(
            journal_fn=journal,
            unit_fn=lambda: (EV.PASS, "unit ok"),
            owned_fn=lambda: (EV.PASS, "(true,)"),
            emit_fn=emit,
            capture_fn=capture,
            monitor_fn=lambda: [(0, 0, 1920, 1080)],
            pixel_fn=lambda _p: (bytes([0, 0, 0, 255, 255, 0, 0, 255]), 2, 1),
            pin_fn=lambda _p: (True, "✓ 钉上了"),
            clear_fn=lambda: (True, "✓ 清了"),
            sleep_fn=lambda _s: None,
            mask_runner=lambda _a: cp([], 0, "MASK_OK 1 块屏：建窗 → 收窗\n"),
            which=lambda n: "/usr/bin/" + n,
            out_dir=None)
        self.assertEqual(len(rows), 7, rows)
        self.assertTrue(all(state == EV.PASS for state, _ in rows),
                       [r for r in rows if r[0] != EV.PASS])
        import io
        text = EV.render_table(rows, when="X", stream=io.StringIO())
        self.assertIn("PASS ——", text)

    def test_run_all_no_screen_skips_only_the_pin_step(self):
        seen = {"emitted": False}

        def journal():
            return READY_GOOD + (PROBE_LINE if seen["emitted"] else "")

        rows = EV.run_all(
            journal_fn=journal,
            unit_fn=lambda: (EV.PASS, "ok"), owned_fn=lambda: (EV.PASS, "ok"),
            emit_fn=lambda: seen.update(emitted=True),
            capture_fn=lambda x, y, w, h, out: (pathlib.Path(out).write_bytes(b"x") or True, "ok"),
            monitor_fn=lambda: [(0, 0, 100, 100)],
            pixel_fn=lambda _p: (bytes([1, 2, 3, 4, 5, 6, 7, 8]), 2, 1),
            pin_fn=lambda _p: (True, "x"), clear_fn=lambda: (True, "x"),
            sleep_fn=lambda _s: None,
            mask_runner=lambda _a: cp([], 0, "MASK_OK ok\n"),
            which=lambda n: "/usr/bin/" + n,
            no_screen=True)
        self.assertEqual(rows[5][0], EV.SKIP)
        self.assertIn("--no-screen", rows[5][1])


class TestScratchDirIsCleanedUp(unittest.TestCase):
    """`run_all` 自建的临时目录必须自己收掉，并且别把死了的绝对路径留在表里。

    2026-10-20 实测踩到：第一版每跑一次就 `mkdtemp` 一个 `/tmp/capsplus-verify-*` 从不回收，
    跑几轮全量单测攒了 35 个。本仓为这类事吃过亏（`/tmp/capsplus-extjs-*` 一天 141 个）。
    """

    GLOB = "capsplus-verify-*"

    def _left_behind(self):
        parent = pathlib.Path(__import__("tempfile").gettempdir())
        return {p.name for p in parent.glob(self.GLOB)}

    def _rows(self, out_dir):
        seen = {"emitted": False}

        def journal():
            return READY_GOOD + (PROBE_LINE if seen["emitted"] else "")

        return EV.run_all(
            journal_fn=journal, unit_fn=lambda: (EV.PASS, "ok"),
            owned_fn=lambda: (EV.PASS, "ok"), emit_fn=lambda: seen.update(emitted=True),
            capture_fn=lambda x, y, w, h, out: (pathlib.Path(out).write_bytes(b"x") or True, "ok"),
            monitor_fn=lambda: [(0, 0, 100, 100)],
            pixel_fn=lambda _p: (bytes([1, 2, 3, 4, 5, 6, 7, 8]), 2, 1),
            pin_fn=lambda _p: (True, "x"), clear_fn=lambda: (True, "x"),
            sleep_fn=lambda _s: None,
            mask_runner=lambda _a: cp([], 0, "MASK_OK ok\n"),
            which=lambda n: "/usr/bin/" + n, no_screen=True, out_dir=out_dir)

    def test_self_built_dir_is_removed(self):
        before = self._left_behind()
        rows = self._rows(None)
        self.assertEqual(len(rows), 7)
        self.assertEqual(self._left_behind() - before, set(),
                         "run_all 自建的临时目录没被收掉")

    def test_caller_supplied_dir_is_kept(self):
        """`--keep DIR` 那一侧必须**留住**（调用方给的目录不归我们删）。"""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="capsplus-verify-keep-") as keep:
            rows = self._rows(pathlib.Path(keep))
            self.assertEqual(len(rows), 7)
            self.assertTrue(pathlib.Path(keep).exists(), "调用方给的目录被删了")

    def test_failure_detail_has_no_dead_absolute_path(self):
        """红的行里不许留着已经删掉的 `/tmp/...` 路径（那比只给文件名更误导人）。"""
        seen = {"emitted": False}

        def journal():
            return READY_GOOD + (PROBE_LINE if seen["emitted"] else "")

        rows = EV.run_all(
            journal_fn=journal, unit_fn=lambda: (EV.PASS, "ok"),
            owned_fn=lambda: (EV.PASS, "ok"), emit_fn=lambda: seen.update(emitted=True),
            capture_fn=lambda x, y, w, h, out: (False, f"✗ 5.0s 内没等到产物 {out}"),
            monitor_fn=lambda: [(0, 0, 100, 100)],
            pixel_fn=lambda _p: (b"", 0, 0),
            pin_fn=lambda _p: (True, "x"), clear_fn=lambda: (True, "x"),
            sleep_fn=lambda _s: None,
            mask_runner=lambda _a: cp([], 0, "MASK_OK ok\n"),
            which=lambda n: "/usr/bin/" + n, no_screen=True, out_dir=None)
        self.assertEqual(rows[4][0], EV.FAIL)
        self.assertNotIn("capsplus-verify-", rows[4][1],
                         f"红行里留着已经删掉的临时路径：{rows[4][1]}")


class TestScriptShape(unittest.TestCase):
    SCRIPT = REPO / "tools" / "verify-after-login.sh"

    def test_parses_with_its_own_shebang(self):
        interp = self.SCRIPT.read_text(encoding="utf-8").splitlines()[0].lstrip("#!").strip()
        self.assertTrue(interp.endswith("sh"))
        self.assertEqual(subprocess.run([interp, "-n", str(self.SCRIPT)],
                                        capture_output=True, text=True).returncode, 0)

    def test_no_bashisms_and_no_killing(self):
        code = "\n".join(ln for ln in self.SCRIPT.read_text(encoding="utf-8").splitlines()
                         if not ln.lstrip().startswith("#"))
        for bad in ("=(", "shopt", "[[ ", "pkill", "killall"):
            self.assertNotIn(bad, code, f"出现禁用写法：{bad}")

    def test_verdict_is_not_reimplemented_in_sh(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("capsplus.extverify", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)