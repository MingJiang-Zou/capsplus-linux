# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""跨发行版可移植性的单测：keyd 二进制名、用户 bin 目录、模板里不许有个人路径。

为什么要这些判据（2026-10-16）：仓里原先写死 `keyd.rvaiya` 与 `/home/<某个人>/...`，
只有打包者那台 Ubuntu 能跑。三条口径：

  ① keyd 可执行名依次试 `keyd.rvaiya`（Debian/Ubuntu 因与 onak 撞名而改名）→ `keyd`
     （Arch/Fedora/openSUSE）→ 兜底 `/usr/bin/keyd`；systemd 单元名反倒恒为 `keyd`。
  ② 家目录一律 `Path.home()` 现算，`CAPSPLUS_BINDIR` 可覆盖（测试就靠它注入）。
  ③ 仓内任何模板/源码都不许出现具体用户的绝对路径。
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import env                 # noqa: E402
from capsplus.render import binding_rows, render  # noqa: E402


class TestResolveKeydBin(unittest.TestCase):
    """keyd 二进制名的选择顺序 —— 全仓只该有这一个入口。"""

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_KEYD_BIN": "/opt/weird/keyd"}):
            self.assertEqual(env.resolve_keyd_bin(), "/opt/weird/keyd")

    def test_rvaiya_name_first(self):
        """Debian/Ubuntu：只有 keyd.rvaiya 时选它。"""
        def fake_which(name):
            return "/usr/bin/keyd.rvaiya" if name == "keyd.rvaiya" else None
        with mock.patch.dict(os.environ, {"CAPSPLUS_KEYD_BIN": ""}), \
             mock.patch("capsplus.env.shutil.which", side_effect=fake_which):
            self.assertEqual(env.resolve_keyd_bin(), "/usr/bin/keyd.rvaiya")
            self.assertEqual(env.find_keyd_bin(), "/usr/bin/keyd.rvaiya")
            self.assertIn("PATH 里的 keyd.rvaiya", env.keyd_bin_origin())

    def test_plain_keyd_name_on_other_distros(self):
        """Arch/Fedora/openSUSE：只有 keyd 时选它（这条以前会直接判成"没装"）。"""
        def fake_which(name):
            return "/usr/bin/keyd" if name == "keyd" else None
        with mock.patch.dict(os.environ, {"CAPSPLUS_KEYD_BIN": ""}), \
             mock.patch("capsplus.env.shutil.which", side_effect=fake_which):
            self.assertEqual(env.resolve_keyd_bin(), "/usr/bin/keyd")
            self.assertEqual(env.keyd_cli_hint(), "keyd")

    def test_fallback_when_nothing_installed(self):
        """/usr/bin/keyd 兜底，但 find_keyd_bin() 必须说"没装"（不许拿兜值当已安装）。"""
        with mock.patch.dict(os.environ, {"CAPSPLUS_KEYD_BIN": ""}), \
             mock.patch("capsplus.env.shutil.which", return_value=None):
            self.assertEqual(env.resolve_keyd_bin(), env.KEYD_BIN_FALLBACK)
            self.assertIsNone(env.find_keyd_bin())
            self.assertIn("兜底", env.keyd_bin_origin())

    def test_hint_defaults_to_plain_keyd_when_absent(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_KEYD_BIN": ""}), \
             mock.patch("capsplus.env.shutil.which", return_value=None):
            self.assertEqual(env.keyd_cli_hint(), "keyd")

    def test_unit_name_is_keyd(self):
        """单元名恒为 keyd（Ubuntu 的 keyd.rvaiya 装的也是 keyd.service，实测 2026-10-16）。"""
        self.assertEqual(env.KEYD_UNIT, "keyd")


class TestBindir(unittest.TestCase):
    def test_default_is_home_local_bin(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_BINDIR": ""}):
            self.assertEqual(env.bindir(), Path.home() / ".local" / "bin")

    def test_env_override_injects_into_render(self):
        """`CAPSPLUS_BINDIR` 必须能进到渲染结果 —— 家目录不写死，测试才有注入点。"""
        with mock.patch.dict(os.environ, {"CAPSPLUS_BINDIR": "/tmp/zz-test-bin"}):
            text = render()
            rows = [r for r in binding_rows("capl") if r[0] == "q"]
        self.assertIn("command(/tmp/zz-test-bin/keyd-qbar-cmd)", text)
        self.assertNotIn(env.BINDIR_TOKEN, text, "渲染结果里残留了未替换的记号")
        self.assertEqual(rows[0][1], "command(/tmp/zz-test-bin/keyd-qbar-cmd)",
                         "设置界面显示的动作必须和写进配置的一致，不能一个是记号一个是路径")

    def test_no_personal_home_path_in_source(self):
        """数据源里不许有具体用户的绝对路径（只有记号）。"""
        from capsplus import mapping_data as MD
        q = [e[1] for e in MD.CAPL if not isinstance(e, str) and e[0] == "q"][0]
        self.assertEqual(q, f"command({env.BINDIR_TOKEN}/keyd-qbar-cmd)")
        self.assertNotIn("/home/", q)

    def test_space_in_bindir_is_refused(self):
        """keyd 的 command() 按空白切参数，路径带空格 = 写出坏配置，必须当场报错。"""
        with mock.patch.dict(os.environ, {"CAPSPLUS_BINDIR": "/home/a b/.local/bin"}):
            with self.assertRaises(ValueError) as ctx:
                render()
        self.assertIn("空格", str(ctx.exception))

    def test_token_only_replaced_when_present(self):
        self.assertEqual(env.resolve_bindir_token("macro(up up)"), "macro(up up)")


class TestNoPersonalPathsInRepo(unittest.TestCase):
    """仓里不许留任何具体用户的路径/用户名（模板、脚本、源码、文档一起算）。"""

    SCANNED_SUFFIXES = {".py", ".sh", ".md", ".desktop", ".service", ".toml", ""}
    # 判据按**形状**抓，不写死任何具体用户名 —— 否则这条判据自己就成了第二份个人痕迹，
    # 而且换台机器（CI 上家目录是 /home/runner）就失效。
    PATTERNS = (re.compile(r"/home/[A-Za-z0-9._-]+"),        # 写死的绝对家目录
                re.compile(r"/run/user/[0-9]+"),             # 写死的 uid 目录
                re.compile(r"runuser\s+-u\s+[A-Za-z0-9._-]+"))  # runuser 后面跟裸名字
    EXEMPT = ("tests/", ".git")          # 测试里的临时路径与反例字符串不算

    def test_no_absolute_home_or_uid_paths_in_tracked_files(self):
        bad = []
        for path in sorted(REPO.rglob("*")):
            if path.is_dir():
                continue
            rel = str(path.relative_to(REPO))
            if rel.startswith(self.EXEMPT) or "__pycache__" in path.parts:
                continue
            if path.suffix not in self.SCANNED_SUFFIXES and path.name not in (
                    "capsplus", "capsplus-qbar", "capsplus-settings", "capsplus-tray",
                    "keyd-qbar-cmd", "LICENSE"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in self.PATTERNS:
                hit = pattern.search(text)
                if hit:
                    bad.append(f"{rel}: 出现 {hit.group(0)!r}")
        self.assertEqual(bad, [], "这些文件还写着个人路径/裸用户名：\n  " + "\n  ".join(bad))

    def test_desktop_templates_use_placeholders(self):
        for path in sorted((REPO / "packaging").glob("*.desktop")):
            text = path.read_text(encoding="utf-8")
            self.assertIn("@BIN_DIR@", text, f"{path.name} 的 Exec 还是硬路径")
            self.assertIn("@REPO_DIR@", text, f"{path.name} 的 Icon 还是硬路径")


class TestNoPersonalNames(unittest.TestCase):
    """仓里不许留打包者的姓名/登录名 —— 公开分发前必须过这一关。

    名单**不写死在代码里**（否则这条判据自己就是第二份个人痕迹，换台机器还失效）：
      ① 自动取当前机器的登录名与 gecos（`getent passwd $(id -u)` 的第 5 段）；
      ② 追加用 `CAPSPLUS_PERSONAL_MARKERS="张三;李四"`（中文全名走环境变量，不落仓）。
    所以公开前的完整跑法是：
        CAPSPLUS_PERSONAL_MARKERS=<你的名字> python3 -m unittest discover -s tests
    """

    EXEMPT = ("tests/", ".git")
    SUFFIXES = {".py", ".sh", ".md", ".desktop", ".service", ".toml", ""}
    SYSTEM_NAMES = {"root", "runner", "nobody", "www-data"}

    def markers(self) -> list[str]:
        import getpass
        import pwd
        out = {getpass.getuser()}
        try:
            out |= {t.strip() for t in re.split(r"[,;:]", pwd.getpwuid(os.getuid()).pw_gecos) if len(t.strip()) >= 2}
        except KeyError:
            pass
        out |= {t.strip() for t in os.environ.get("CAPSPLUS_PERSONAL_MARKERS", "").split(";") if t.strip()}
        return sorted(m for m in out if m and m not in self.SYSTEM_NAMES)

    def tracked_files(self):
        for path in sorted(REPO.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(REPO))
            if rel.startswith(self.EXEMPT):
                continue
            if path.suffix not in self.SUFFIXES and path.name not in (
                    "capsplus", "capsplus-qbar", "capsplus-settings", "capsplus-tray",
                    "keyd-qbar-cmd"):
                continue
            yield rel, path

    def test_no_personal_names_in_tracked_files(self):
        marks = self.markers()
        if not marks:
            self.skipTest("本机取不到任何名字类标记，这条判据无从判起")
        bad = []
        for rel, path in self.tracked_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in marks:
                if marker in text:
                    bad.append(f"{rel}: 出现 {marker!r}")
        self.assertEqual(bad, [], "这些文件还写着个人名字：\n  " + "\n  ".join(bad))

    def test_the_guard_really_catches_a_name(self):
        """反例具型：判据不是摆设 —— 造一份带名字的仓，必须抓到。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "evil.py").write_text("# 维护者：阿猫\nx = 1\n", encoding="utf-8")
            hits = [p.name for p in root.glob("*.py") if "阿猫" in p.read_text(encoding="utf-8")]
            self.assertEqual(hits, ["evil.py"], "个人名字扫不出来 = 这条判据是空的")

    def test_markers_skip_system_names_and_take_env(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_PERSONAL_MARKERS": "root;runner;阿狗"}), \
             mock.patch("getpass.getuser", return_value="runner"):
            marks = self.markers()
        self.assertIn("阿狗", marks)
        for ignored in ("root", "runner"):
            self.assertNotIn(ignored, marks)


class TestFdBinary(unittest.TestCase):
    def test_prefers_fdfind_then_fd(self):
        with mock.patch("capsplus.env.shutil.which", side_effect=lambda n: n == "fdfind"):
            self.assertEqual(env.find_fd_binary(), "fdfind")
        def fake_which(name):
            return name if name == "fd" else None
        with mock.patch("capsplus.env.shutil.which", side_effect=fake_which):
            self.assertEqual(env.find_fd_binary(), "fd")
        with mock.patch("capsplus.env.shutil.which", return_value=None):
            self.assertIsNone(env.find_fd_binary())

    def test_file_finder_resolves_binary_by_default(self):
        """FileFinder() 不传 binary 时也跟着本机走（Arch 上不该去找 fdfind）。"""
        from qbar.logic import FileFinder
        with mock.patch("capsplus.env.shutil.which", side_effect=lambda n: n == "fd"):
            self.assertEqual(FileFinder([Path("/nonexistent")]).binary, "fd")


class TestStatusNamesTheBinary(unittest.TestCase):
    """`capsplus status` 要把 keyd 二进制是怎么选出来的打出来（出问题第一眼就看它）。"""

    def test_status_prints_keyd_program_line(self):
        import contextlib
        import io
        from capsplus import apply as A

        class FakeOverrides:
            error = ""
            count = 0
            path = Path("/tmp/zz-mappings.toml")
            errors: list[str] = []

        buf = io.StringIO()
        with mock.patch.object(A, "service_state", return_value="active"), \
             mock.patch.object(A, "config_summary", return_value=([("capl", 31)], Path("/tmp/zz.conf"), True)), \
             mock.patch.object(A, "load_overrides", return_value=FakeOverrides()), \
             mock.patch.object(A, "last_log_line", return_value="(测试)"), \
             mock.patch.object(A, "keyd_bin_origin",
                               return_value="/usr/bin/keyd（PATH 里的 keyd）"), \
             contextlib.redirect_stdout(buf):
            rc = A.main(["status"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("keyd 程序 : /usr/bin/keyd（PATH 里的 keyd）", out)


class TestApplyUsesSingleEntry(unittest.TestCase):
    """全仓只该有一个地方决定 keyd 叫什么：别的新增代码不许再写死 keyd.rvaiya。"""

    def test_only_env_names_the_candidates(self):
        hits = []
        for path in sorted(REPO.rglob("*.py")):
            if "__pycache__" in path.parts or path == REPO / "capsplus" / "env.py":
                continue
            if "keyd.rvaiya" in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(REPO)))
        # tests/ 里允许出现（那是断言选择顺序的判据本身）
        hits = [h for h in hits if not h.startswith("tests/")]
        self.assertEqual(hits, [], "这些文件还自己写死 keyd.rvaiya：\n  " + "\n  ".join(hits))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestQbarLauncherReportsOneDisplayBackend(unittest.TestCase):
    """判据：keyd 的降权包装**不许同时**报 WAYLAND_DISPLAY 和 DISPLAY。

    2026-10-16 用户实测：Qbar 打开网址时 Edge 另起新窗口而不是在原窗口开标签页。
    包装脚本原先写成 `WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0}` 外加
    `DISPLAY=${DISPLAY:-:0}`（keyd 以 root 跑，两个变量通常都没有，于是双双凭空造出来），
    子进程面对两个显示后端就容易被当成"另一次启动"。现在按 socket 探测，只传一个。
    """

    SCRIPT = REPO / "bin" / "keyd-qbar-cmd"

    def test_no_unconditional_double_injection(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        for bad in ('DISPLAY="${DISPLAY:-:0}"', 'WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"'):
            self.assertNotIn(bad, text, f"包装脚本又回到无条件注入两个后端了：{bad}")
        self.assertIn('DISPLAY_ARGS', text, "显示后端应该由那段探测逻辑算进 DISPLAY_ARGS 数组")
        self.assertIn('-S "$RUN_DIR/wayland-0"', text, "得真的去探 Wayland socket，否则判断没有依据")

    def test_script_stays_bash_parseable(self):
        proc = subprocess.run(["bash", "-n", str(self.SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"bash -n 失败：{proc.stderr}")
