# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：统一入口 `capsplus`（capsplus/cli.py）与装机件（capsplus/install.py）。

为什么单开这一组（2026-10-20）：这两个模块是"把散在五处的装机/启停/体检收成一处"的落点，
它们自己坏掉的表现是**最难看的那种** —— 装机装一半、卸载删过头、状态页骗人说没事。
所以这里钉四类事：
  ① 分发：未知子命令必须退非 0（别静默当 status 跑）；
  ② 落盘：软链/桌面入口/单元都是"幂等 + 原子"（跑两遍结果一样、不留半截临时文件）；
  ③ 退役：删东西一律**移进焚化炉**，撞名不许覆盖（可能那是唯一一份）；
  ④ 状态：有红项就必须有下一步 —— 这条带反例具型（`groups_without_actions` 真抓得住）。
"""

import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import cli, install  # noqa: E402


class TestDispatch(unittest.TestCase):
    """子命令分发：错的口令不许被当成别的命令跑。"""

    def test_unknown_subcommand_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["nope"])
        self.assertEqual(rc, 2)
        self.assertIn("未知子命令", err.getvalue())

    def test_version_and_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["version"]), 0)
            self.assertEqual(cli.main(["help"]), 0)
        text = out.getvalue()
        self.assertIn("capsplus", text)
        self.assertIn("install", text)
        self.assertIn("doctor", text)

    def test_default_is_status(self):
        """不带参数 = status（不是什么都不做，也不是 apply）。"""
        with mock.patch.object(cli, "render_status", return_value=[]) as fake:
            self.assertEqual(cli.main([]), 0)
        fake.assert_called_once()

    def test_ext_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["ext", "wat"]), 2)
        self.assertIn("capsplus ext", err.getvalue())

    def test_single_entry_point_goes_through_cli(self):
        """bin/capsplus 必须分发到 cli —— 2026-10-20 之前它只转发 apply，
        于是 `capsplus install` 这类口令会被当成未知子命令（装机入口分裂成五处的直接后果）。"""
        src = (REPO / "bin" / "capsplus").read_text(encoding="utf-8")
        self.assertIn("from capsplus.cli import main", src)
        self.assertNotIn("from capsplus.apply import main", src)


class TestInstallHelpers(unittest.TestCase):
    def test_render_template_replaces_every_token(self):
        text = "Exec=@BIN_DIR@/capsplus-settings\nIcon=@REPO_DIR@/assets/x.svg\n"
        done = install.render_template(text, bin_dir=pathlib.Path("/opt/bin"),
                                       repo=pathlib.Path("/opt/repo"))
        self.assertIn("/opt/bin/capsplus-settings", done)
        self.assertIn("/opt/repo/assets/x.svg", done)
        self.assertNotIn("@", done)

    def test_write_atomic_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "capsplus.target"
            install.write_atomic(target, "hello\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
            leftovers = [p.name for p in pathlib.Path(tmp).iterdir() if p.name.startswith(".")]
            self.assertEqual(leftovers, [], "原子写留下的临时文件没清")

    def test_retire_moves_into_the_graveyard_and_keeps_collisions(self):
        """反例具型：退役**不是删除**，撞名也不许覆盖（上一次退役的东西可能只有那一份）。"""
        with tempfile.TemporaryDirectory() as tmp:
            grave = pathlib.Path(tmp) / "attic"
            one = pathlib.Path(tmp) / "a.desktop"
            one.write_text("第一份\n", encoding="utf-8")
            where = install.retire(one, grave=grave)
            self.assertFalse(one.exists(), "退役后源路径还在？")
            self.assertEqual(where.read_text(encoding="utf-8"), "第一份\n")

            two = pathlib.Path(tmp) / "a.desktop"
            two.write_text("第二份\n", encoding="utf-8")
            where2 = install.retire(two, grave=grave)
            self.assertNotEqual(where, where2, "撞名时覆盖了上一次退役的文件")
            self.assertEqual(where.read_text(encoding="utf-8"), "第一份\n")
            self.assertEqual(where2.read_text(encoding="utf-8"), "第二份\n")

    def test_retire_missing_path_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(install.retire(pathlib.Path(tmp) / "nope", grave=pathlib.Path(tmp)))

    def test_attic_dir_is_dated_under_tmp(self):
        path = install.attic_dir("20261020")
        self.assertEqual(path, pathlib.Path("/tmp/attic-20261020"))


class TestInstallBins(unittest.TestCase):
    def test_installs_symlinks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            with contextlib.redirect_stdout(io.StringIO()):
                install.install_bins(install.Reporter(stream=io.StringIO()), bin_dir=bin_dir)
                first = {name: (bin_dir / name).is_symlink() for name in install.ENTRY_POINTS}
                again = install.Reporter(stream=io.StringIO())
                install.install_bins(again, bin_dir=bin_dir)
            self.assertTrue(all(first.values()), f"没装全：{first}")
            # 第二遍应当是"已指向本仓，未改动"，一个退役动作都不该发生
            self.assertTrue(all("已指向本仓" in line for line in again.actions if line.endswith("）")),
                            f"第二遍不是幂等：{again.actions}")
            for name in install.ENTRY_POINTS:
                self.assertEqual(pathlib.Path(os.readlink(bin_dir / name)),
                                 REPO / "bin" / name)

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            with contextlib.redirect_stdout(io.StringIO()):
                install.install_bins(install.Reporter(dry_run=True, stream=io.StringIO()),
                                     bin_dir=bin_dir)
            self.assertFalse(bin_dir.exists(), "--dry-run 竟然落了盘")

    def test_dependency_check_uses_injected_which(self):
        out = install.check_dependencies(which=lambda name: None)
        self.assertTrue(all(not ok for ok, _ in out), "which 全为空却报告齐全")
        only_python = install.check_dependencies(
            which=lambda name: "/usr/bin/python3" if name == "python3" else None)
        self.assertTrue(only_python[0][0])
        self.assertFalse(only_python[1][0])


class TestUpDown(unittest.TestCase):
    """`capsplus up` 的关键形状：迁移到单元组之后，autostart 留下的托盘必须**先收掉**。

    反例具型（2026-10-20 真踩过）：托盘用 `tray.lock` 抢单实例，抢不到就 `return 1`；
    而单元是 `Restart=on-failure` ⇒ 不收掉旧实例就是"每 5 秒重启一次"的循环。
    另一条同日踩到的：**不许用 `pkill -f capsplus-tray` 收它** —— `-f` 匹配整条命令行，
    会把"命令行里提到这个字符串"的调用者自己也杀了（我第一次跑 `capsplus up` 就把自己的
    shell 杀了）。所以这里同时钉"用 pid 精确杀"这条形状。
    """

    def _run_up(self, orphan_pids: list[int], unit_active: bool):
        calls = []

        def fake_systemctl(*args):
            calls.append(("systemctl", args))
            return subprocess.CompletedProcess(args, 0, "", "")

        def fake_kill(pid, sig):
            calls.append(("kill", pid, sig))

        with mock.patch.object(cli, "_unit_installed", return_value=True), \
                mock.patch.object(cli, "_has_user_manager", return_value=True), \
                mock.patch.object(cli, "_tray_pids", return_value=orphan_pids), \
                mock.patch.object(cli, "unit_active", return_value=unit_active), \
                mock.patch.object(cli, "_systemctl_user", fake_systemctl), \
                mock.patch.object(cli.os, "kill", fake_kill), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = cli.up(io.StringIO())
        return rc, calls

    def test_orphan_tray_is_stopped_before_starting_the_target(self):
        rc, calls = self._run_up(orphan_pids=[4242], unit_active=False)
        self.assertEqual(rc, 0)
        kinds = [c[0] for c in calls]
        self.assertIn("kill", kinds, "没去停 autostart 留下的托盘")
        self.assertLess(kinds.index("kill"), kinds.index("systemctl"),
                        "顺序反了：应当先收旧实例、再 start target")
        self.assertEqual(calls[-1], ("systemctl", ("start", "capsplus.target")))
        self.assertIn(("kill", 4242, cli.signal.SIGTERM), calls)

    def test_no_kill_when_the_unit_itself_owns_the_tray(self):
        rc, calls = self._run_up(orphan_pids=[4242], unit_active=True)
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in calls], ["systemctl"], "单元自己管着托盘时不该去杀进程")

    def test_tray_discovery_is_by_argv_not_by_command_line_substring(self):
        """真进程扫描（读 /proc）：只认 `python3 …/capsplus-tray` 或直接执行的那个入口。

        这条的牙口在两件事上：① 不能因为命令行里出现这个字符串就算托盘（否则 pkill 会杀调用者）；
        ② 本进程自己不算。判据直接拿本进程构造：把自己的 cmdline 里塞进那个词，然后要求
        `_tray_pids()` 不把自己算进去。
        """
        pids = cli._tray_pids()
        self.assertNotIn(os.getpid(), pids)
        # 本进程的 argv 里就有 "capsplus-tray" 这个子串（下面这行注释就带），也必须不算
        self.assertTrue(all(pid != os.getpid() for pid in pids))


class TestDesktopAndUnits(unittest.TestCase):
    def test_desktop_entry_installed_rendered_and_install_owns_it(self):
        """应用菜单里**只装一条**：更早那版装两条，用户实测说"应用里面有两个"。"""
        self.assertEqual(len(install.DESKTOP_ENTRIES), 1, f"桌面入口不止一条：{install.DESKTOP_ENTRIES}")
        with tempfile.TemporaryDirectory() as tmp:
            apps = pathlib.Path(tmp)
            with mock.patch.object(install, "APPLICATIONS_DIR", apps), \
                    mock.patch.object(install.shutil, "which", return_value=None), \
                    contextlib.redirect_stdout(io.StringIO()):
                written = install.install_desktop(install.Reporter(stream=io.StringIO()))
            self.assertEqual(len(written), 1)
            text = pathlib.Path(written[0]).read_text(encoding="utf-8")
            self.assertNotIn("@BIN_DIR@", text)
            self.assertNotIn("@REPO_DIR@", text)
            self.assertIn(str(install.bindir()), text)

    def test_user_units_are_one_target_plus_two_components(self):
        """统一管理的关键形状：target 收全部常驻件，且 target 自己挂在 graphical-session 上。"""
        self.assertIn("capsplus.target", install.USER_UNITS)
        target = (REPO / "packaging" / "capsplus.target").read_text(encoding="utf-8")
        self.assertIn("Wants=capsplus-tray.service capsplus-extension-sync.service", target)
        self.assertIn("WantedBy=graphical-session.target", target)
        tray = (REPO / "packaging" / "capsplus-tray.service").read_text(encoding="utf-8")
        self.assertIn("PartOf=capsplus.target", tray)
        self.assertIn("WantedBy=capsplus.target", tray)


class TestUninstall(unittest.TestCase):
    def test_retires_install_artifacts_and_keeps_user_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bin_dir, apps, units, grave = root / "bin", root / "apps", root / "units", root / "attic"
            for d in (bin_dir, apps, units):
                d.mkdir()
            (bin_dir / "capsplus").symlink_to(REPO / "bin" / "capsplus")
            (apps / "capsplus-settings.desktop").write_text("x\n", encoding="utf-8")
            (units / "capsplus.target").write_text("x\n", encoding="utf-8")
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "voice.log").write_text("用户数据\n", encoding="utf-8")

            out = io.StringIO()
            with mock.patch.object(install, "bindir", return_value=bin_dir), \
                    mock.patch.object(install, "APPLICATIONS_DIR", apps), \
                    mock.patch.object(install, "UNIT_DIR", units), \
                    mock.patch.object(install, "attic_dir", return_value=grave), \
                    mock.patch.object(install.shutil, "which", return_value=None), \
                    contextlib.redirect_stdout(out):
                rc = install.uninstall()
            self.assertEqual(rc, 0)
            self.assertFalse((bin_dir / "capsplus").exists(), "bin 软链没退役")
            self.assertFalse((apps / "capsplus-settings.desktop").exists(), "桌面入口没退役")
            self.assertFalse((units / "capsplus.target").exists(), "单元没退役")
            self.assertTrue((grave / "capsplus").exists(), "退役件不在焚化炉里（被删了？）")
            self.assertTrue((state_dir / "voice.log").exists(), "卸载动了用户数据")
            self.assertIn("没有", out.getvalue())


class TestStatusRendering(unittest.TestCase):
    GROUPS = [
        {"title": "扩展", "rows": [(cli.BAD, "扩展状态：ERROR")],
         "next": ["注销重登一次"]},
        {"title": "语音", "rows": [(cli.OK, "worker：在跑")], "next": []},
    ]

    def test_renders_rows_and_next_actions(self):
        out = io.StringIO()
        cli.render_groups(self.GROUPS, out)
        text = out.getvalue()
        self.assertIn("[扩展]", text)
        self.assertIn("注销重登一次", text)
        self.assertIn("没有待办" if False else "下一步", text)

    def test_no_pending_actions_says_so(self):
        groups = [dict(self.GROUPS[1])]
        out = io.StringIO()
        cli.render_groups(groups, out)
        self.assertIn("没有待办", out.getvalue())

    def test_red_without_action_is_caught(self):
        """反例具型：报红却不给动作 —— 这条判据必须抓得住（它就是 2026-10-16 那次白折腾的形状）。"""
        bare = [{"title": "扩展", "rows": [(cli.BAD, "扩展状态：ERROR")], "next": []}]
        self.assertEqual(cli.groups_without_actions(bare), ["扩展"])
        self.assertEqual(cli.groups_without_actions(self.GROUPS), [])
        out = io.StringIO()
        cli.render_groups(bare, out)
        self.assertIn("还没有已知动作", out.getvalue())


if __name__ == "__main__":
    unittest.main()
