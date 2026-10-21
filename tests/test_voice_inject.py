# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""上屏后端的单测：命令序列、运行时探测、以及**不许假装能成功**那条红线。

为什么这批用例必须全部用假 runner / 假探针：真跑一次 `ydotool key` 就是往
用户正在用的屏幕上打键（硬边界）。所以：

  * `run_probe(...)` 的四个注入口在测试里一律给假件，并断言 `Probe.ran is False`
    —— "这条用例有没有碰真系统"是**可断言的**，不是靠人记得；
  * `inject(...)` 一律注入假 runner，只记录 argv 与 stdin；
  * 唯一真跑外部命令的是 `TestRealProbeRunsOnThisMachine`：它只跑 `--help`、
    `ydotool help`，以及**不带任何键码**的 `ydotool key`（0 个键码 = 0 个事件）。
    这台机上 ydotoold 没起来，那条也就只会报"daemon 不在跑"（实测 rc=2）。

键码来源也是判据之一：`KEY_LEFTCTRL`/`KEY_V` 的值从 `/usr/include/linux/input-event-codes.h`
**现读**，测试写一份假头文件（值改成 99/77）看命令里跟没跟着变 —— 变了才证明代码里没写死数字。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_inject as I            # noqa: E402

FAKE_CODES = {"KEY_LEFTCTRL": 99, "KEY_V": 77}     # **故意**与真值(29/47)不同


def fake_header(tmp: Path, codes=FAKE_CODES) -> str:
    path = tmp / "input-event-codes.h"
    body = ["#ifndef _LINUX_INPUT_EVENT_CODES_H", "#define _LINUX_INPUT_EVENT_CODES_H"]
    for name, value in codes.items():
        body.append(f"#define {name}\t{value}")          # 真头文件用的是制表符
    body.append("#endif")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return str(path)


def fake_blob(with_keycode_syntax=True, with_click=True, with_keysym=False) -> bytes:
    parts = [b"Usage: key [OPTION]... [KEYCODES]...", b"\twe're using raw keycodes now."]
    if with_keycode_syntax:
        parts.append(b"Syntax: <keycode>:<pressed>")
    if with_click:
        parts.append(b"  0x40 - Mouse down")
    if with_keysym:
        parts.append(b"e.g. ctrl+a opens something")
    return b"\n".join(parts)


def probe(**over) -> I.Probe:
    """一份"探测全通过"的假结论；`over` 用来逐项打破它。"""
    base = dict(wl_copy="/usr/bin/wl-copy", wl_copy_primary=True, ydotool="/usr/bin/ydotool",
                ydotool_subcommands=("key", "click", "type"), ydotool_daemon=True,
                syntax=("keycode", "click-bitmask"), keycodes=dict(FAKE_CODES),
                keycodes_source="/fake/input-event-codes.h", ran=False)
    base.update(over)
    return I.Probe(**base)


class FakeRunner:
    """记命令的假 subprocess.run。默认成功，可按 argv 片段指定退出码。"""

    def __init__(self, fail_for=(), stderr=b"boom"):
        self.calls = []
        self.fail_for = list(fail_for)
        self.stderr = stderr

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        rc = 0
        for needle, code in self.fail_for:
            if needle in " ".join(argv):
                rc = code
        return type("R", (), {"returncode": rc, "stderr": self.stderr if rc else b"",
                              "stdout": b""})()

    @property
    def joined(self):
        return [" ".join(a) for a, _ in self.calls]


class TestProbeLogic(unittest.TestCase):
    def test_all_pieces_present_means_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = fake_header(Path(tmp))
            p = I.run_probe(which=lambda name: {"wl-copy": "/usr/bin/wl-copy",
                                                "wl-paste": "/usr/bin/wl-paste",
                                                "ydotool": "/usr/bin/ydotool"}[name],
                             help_runner=lambda argv: (0, "--primary\n--clear\n"
                                                        "Available commands:\n"
                                                        "  key\n  click\n  type\n"),
                             binary_reader=lambda path: fake_blob(),
                             daemon_probe=lambda: (True, "可达"),
                             header=header)
        self.assertTrue(p.verified, p.problems)
        self.assertFalse(p.ran, "注入了假件还跑了真命令 = 这条用例在动用户的屏幕")
        self.assertEqual(p.keycodes, FAKE_CODES)
        self.assertEqual(sorted(p.syntax), ["click-bitmask", "keycode"])

    def test_missing_tools_are_reported_not_raised(self):
        p = I.run_probe(which=lambda name: None, help_runner=lambda argv: (0, ""),
                        binary_reader=lambda path: b"", daemon_probe=lambda: (None, "没跑"),
                        header="/no/such/header")
        self.assertFalse(p.verified)
        self.assertFalse(p.ran)
        self.assertIn("没装 wl-copy", "; ".join(p.problems))
        self.assertIn("没装 ydotool", "; ".join(p.problems))

    def test_daemon_down_is_the_only_problem_and_it_blocks_injection(self):
        p = I.run_probe(which=lambda name: "/usr/bin/" + name,
                        help_runner=lambda argv: (0, "--primary\n  key\n  click\n"),
                        binary_reader=lambda path: fake_blob(),
                        daemon_probe=lambda: (False, "failed to connect socket"),
                        header="/x")
        p.keycodes = dict(FAKE_CODES)
        self.assertFalse(p.verified)
        self.assertIn("ydotoold", "; ".join(p.problems))
        self.assertIn("daemon", " ".join(p.report_lines()))

    def test_keysym_only_build_is_refused(self):
        """别的发行版/老版本 ydotool 认 `ctrl+a` 而不认裸键码 —— 那种情况**不许**发键码序列。"""
        p = I.run_probe(which=lambda name: "/usr/bin/" + name,
                        help_runner=lambda argv: (0, "  key\n  click\n"),
                        binary_reader=lambda path: fake_blob(with_keycode_syntax=False,
                                                             with_keysym=True),
                        daemon_probe=lambda: (True, "可达"), header="/x")
        p.keycodes = dict(FAKE_CODES)
        self.assertFalse(p.verified)
        self.assertIn("只认键名", "; ".join(p.problems))
        self.assertEqual(I.plan("clipboard", p, "字"), [])

    def test_unreadable_binary_is_unverified(self):
        p = I.run_probe(which=lambda name: "/usr/bin/" + name,
                        help_runner=lambda argv: (0, "  key\n"),
                        binary_reader=lambda path: (_ for _ in ()).throw(OSError("读不了")),
                        daemon_probe=lambda: (True, "可达"), header="/x")
        self.assertEqual(p.syntax, ())
        self.assertFalse(p.verified)

    def test_report_lines_name_every_layer(self):
        lines = " ".join(probe().report_lines())
        for needle in ("wl-copy", "ydotool", "用法语法", "键码来源", "daemon"):
            self.assertIn(needle, lines)


class TestKeycodesComeFromHeader(unittest.TestCase):
    def test_reads_tab_separated_defines(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = fake_header(Path(tmp))
            codes, source = I.read_keycodes(header)
            self.assertEqual(codes, FAKE_CODES)
            self.assertEqual(source, header)

    def test_missing_header_returns_nothing(self):
        codes, source = I.read_keycodes("/definitely/not/here.h")
        self.assertEqual((codes, source), ({}, None))

    def test_partial_header_is_not_padded_with_invented_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = fake_header(Path(tmp), codes={"KEY_LEFTCTRL": 99})
            codes, _source = I.read_keycodes(header)
            self.assertEqual(codes, {"KEY_LEFTCTRL": 99})
            with self.assertRaises(KeyError):
                I.key_seq("KEY_V", True, codes)


class TestPlanCommandSequences(unittest.TestCase):
    def test_clipboard_sequence_is_write_then_ctrl_v(self):
        steps = I.plan("clipboard", probe(), "九点开会")
        self.assertEqual([s.argv for s in steps], [
            ["/usr/bin/wl-copy"],
            ["/usr/bin/ydotool", "key", "99:1", "77:1", "77:0", "99:0"],
        ], "顺序 = Ctrl 按下 → V 按下 → V 抬起 → Ctrl 抬起；写成别的顺序就是打出字 v")
        self.assertEqual(steps[0].stdin, "九点开会".encode("utf-8"),
                         "文本走 stdin，不做命令行参数（ARG_MAX 与转义两个坑）")
        self.assertIsNone(steps[1].stdin)

    def test_primary_sequence_is_write_primary_then_middle_click(self):
        steps = I.plan("primary", probe(), "x")
        self.assertEqual([s.argv for s in steps], [
            ["/usr/bin/wl-copy", "--primary"],
            ["/usr/bin/ydotool", "click", "0x42", "0x82"],
        ])

    def test_manual_has_no_commands_at_all(self):
        self.assertEqual(I.plan("manual", probe(), "x"), [])

    def test_unverified_probe_produces_no_sequence(self):
        for backend in ("clipboard", "primary"):
            self.assertEqual(I.plan(backend, probe(ydotool_daemon=False), "x"), [], backend)

    def test_unknown_backend_raises_at_plan_time(self):
        with self.assertRaises(ValueError):
            I.plan("xterm", probe(), "x")

    def test_down_comes_before_up(self):
        argv = I.plan("clipboard", probe(), "x")[1].argv
        codes = argv[2:]
        self.assertEqual([c.split(":")[1] for c in codes], ["1", "1", "0", "0"])


class TestInjectExecution(unittest.TestCase):
    def test_verified_clipboard_runs_exactly_two_commands(self):
        runner = FakeRunner()
        result = I.inject("你好", backend="clipboard", probe=probe(), runner=runner)
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(result.steps, 2)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.joined[1], "/usr/bin/ydotool key 99:1 77:1 77:0 99:0")

    def test_failure_names_the_step_and_keeps_stderr(self):
        runner = FakeRunner(fail_for=[("ydotool", 3)], stderr=b"failed to connect socket")
        result = I.inject("你好", backend="clipboard", probe=probe(), runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "step_failed")
        self.assertIn("第 2/2 步", result.message)
        self.assertIn("退出码 3", result.message)
        self.assertIn("failed to connect socket", result.message)
        self.assertIn("ydotool", result.hint)

    def test_oserror_in_the_middle_is_also_structured(self):
        def raiser(argv, **kw):
            if "ydotool" in " ".join(argv):
                raise OSError("没权限")
            return type("R", (), {"returncode": 0, "stderr": b""})()

        result = I.inject("你好", backend="clipboard", probe=probe(), runner=raiser)
        self.assertEqual((result.ok, result.code), (False, "step_failed"))
        self.assertIn("没权限", result.message)

    def test_manual_never_touches_the_runner(self):
        runner = FakeRunner()
        result = I.inject("你好", backend="manual", probe=probe(), runner=runner)
        self.assertTrue(result.ok)
        self.assertTrue(result.keep_window)
        self.assertEqual(runner.calls, [])
        self.assertIn("没注入", result.describe())

    def test_unverified_degrades_to_clipboard_write_but_does_not_claim_success(self):
        """探测没过：只写剪贴板，`ok=False`，且**一个键都不打**（红线）。"""
        runner = FakeRunner()
        bad = probe(ydotool_daemon=False)
        result = I.inject("你好", backend="primary", probe=bad, runner=runner)
        self.assertFalse(result.ok, "没验证还报成功 = 骗人")
        self.assertEqual(result.code, "needs_manual_paste")
        self.assertTrue(result.keep_window)
        self.assertEqual(runner.joined, ["/usr/bin/wl-copy"],
                         f"降级只许写剪贴板，实际跑了：{runner.joined}")
        self.assertIn("Ctrl+V", result.hint)

    def test_degrade_can_be_turned_off(self):
        result = I.inject("你好", backend="clipboard", probe=probe(ydotool=None),
                          runner=FakeRunner(), degrade=False)
        self.assertEqual(result.code, "unverified")
        self.assertIn("manual", result.message)

    def test_backend_defaults_to_clipboard_and_honours_env(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_BACKEND": ""}):
            self.assertEqual(I.backend_from_env(), I.DEFAULT_BACKEND)
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_BACKEND": "PRIMARY"}):
            self.assertEqual(I.backend_from_env(), "primary")
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_BACKEND": "xdotool"}):
            self.assertEqual(I.backend_from_env(), I.DEFAULT_BACKEND,
                             "写错后端名要退回默认，不能整个失灵")
        result = I.inject("字", backend="xdotool", probe=probe(), runner=FakeRunner())
        self.assertEqual((result.ok, result.code), (False, "bad_backend"))

    def test_sleep_between_steps_only_between_them(self):
        slept = []
        I.inject("你好", backend="clipboard", probe=probe(), runner=FakeRunner(),
                 sleep=slept.append)
        self.assertEqual(len(slept), 1, "两步之间等一次，首尾不等")


class TestRealProbeRunsOnThisMachine(unittest.TestCase):
    """默认实现也得真跑一次：注入口全空的这条路 CI 上没有 wl-copy/ydotool，
    目标是"**不崩**且报未验证"，不是要求它通过。"""

    def test_run_probe_with_no_injections_does_not_raise(self):
        p = I.run_probe()
        self.assertIsInstance(p.verified, bool)
        self.assertIsInstance(p.problems, list)
        if not p.verified:
            self.assertTrue(p.problems, "没过却没写为什么 = 状态不可查")
        self.assertTrue(" ".join(p.report_lines()))

    def test_the_three_backends_are_the_documented_set(self):
        self.assertEqual(I.BACKENDS, ("clipboard", "primary", "manual"))
        self.assertEqual(I.DEFAULT_BACKEND, "clipboard")


class TestGuardHasTeeth(unittest.TestCase):
    """反例具型。"""

    def test_codes_in_the_sequence_are_the_probed_ones(self):
        """把假头文件的键码改掉，命令里跟着变 —— 证明数字不是写死在代码里的。"""
        with tempfile.TemporaryDirectory() as tmp:
            header = fake_header(Path(tmp), codes={"KEY_LEFTCTRL": 111, "KEY_V": 222})
            codes, _ = I.read_keycodes(header)
            argv = I.plan("clipboard", probe(keycodes=codes), "x")[1].argv
        self.assertIn("111:1", argv)
        self.assertIn("222:0", argv)
        self.assertNotIn("29:1", argv, "又变成写死的 29 = 探测没参与")

    def test_verified_property_reacts_to_each_piece(self):
        """`verified` 不许是摆设：五项里缺任意一项都必须变假。"""
        broken = [
            dict(wl_copy=None), dict(wl_copy_primary=False), dict(ydotool=None),
            dict(syntax=("click-bitmask",)), dict(keycodes={"KEY_V": 47}),
            dict(ydotool_daemon=False), dict(ydotool_daemon=None),
        ]
        for over in broken:
            with self.subTest(**over):
                self.assertFalse(probe(**over).verified, f"这都没拦住：{over}")

    def test_unverified_reason_offers_two_ways_out(self):
        text = I.unverified_reason(probe(ydotool_daemon=False,
                                         problems=["ydotoold 没在跑"]))
        self.assertIn("manual", text)
        self.assertIn("ydotoold", text)

    def test_fake_runner_is_mandatory_for_the_success_path(self):
        """反例具型：`inject` 不传 runner 会走真 subprocess —— 这里用一条**必然不存在**的
        可执行文件路径，确认它只是失败而不是抛出来。"""
        result = I.inject("你好", backend="clipboard",
                          probe=probe(wl_copy="/nonexistent-bin-zz",
                                      ydotool="/nonexistent-bin-zz"))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "step_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
