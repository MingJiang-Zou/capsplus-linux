# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：`raise_browser` 这条链路（配置键 → GI 发信号 → Qbar 调用 → 仓内扩展的形状）。

背景（2026-10-16 实测定位）：焦点不在浏览器时，Qbar 打开网址会让浏览器**另起一扇窗**。
根因是 GTK3 递不出 Wayland 激活令牌（`libgdk-3` 里 `XDG_ACTIVATION_TOKEN` 出现 0 次）；
不是双报显示后端（修完现象不变），也不是另起进程（`msedge` 主进程数实测恒为 1）。
借 GNOME 扩展的特权先激活浏览器窗口，才谈得上"在原窗口开标签页"。

这里钉的每一条都是**今天真踩过的坑**，不是想象中的规矩：
* 扩展必须 `export default class`（写成模块级 `export function enable` 会被 shell
  报 `not a constructor` **静默不加载**，现象与没装一样）；
* 扩展不许用 `export_object_path` / `register_object`（本机 GJS：前者 `undefined`，
  后者只剩原始 C 绑定、JS 造不出 vtable）；只用实测跑通的 `signal_subscribe`（回调在最后一位）；
* Qbar 侧必须"先激活再打开"，且**任何失败都不许挡住打开动作**（吞掉，不抛）。
"""

import ast
import json
import os
import pathlib
import shutil
import re
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar import search_config as SC          # noqa: E402
from qbar.logic import (RAISE_BUS_IFACE, RAISE_BUS_PATH, RAISE_JOURNAL_WAIT_MS,   # noqa: E402
                        RAISE_MEMBER, RAISE_PROBE_MEMBER, RAISE_SETTLE_MS,
                        journal_probe_command, latest_probe_report, parse_probe_report,
                        raise_browser_signal, should_settle)

from capsplus.extcheck import (EXT_UUID, bench_verdict,   # noqa: E402  体检函数（不碰 gi）
                               extension_status)
_HAS_EXE = lambda _x: "/usr/bin/gnome-extensions"          # 假装系统里有 gnome-extensions
_NO_EXE = lambda _x: None

APP = REPO / "qbar" / "qbar_app.py"
EXT_DIR = REPO / "extensions" / "raise-browser@capsplus.example"


def _functions(tree, name):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name]


class FakeBus:
    """假总线：判据不需要真 D-Bus，也不需要扩展在跑。"""

    def __init__(self):
        self.calls = []

    def emit_signal(self, *args):
        self.calls.append(args)


class TestSignalSender(unittest.TestCase):
    def test_emits_the_documented_five_arguments(self):
        bus = FakeBus()
        raise_browser_signal(bus)
        self.assertEqual(bus.calls, [(None, RAISE_BUS_PATH, RAISE_BUS_IFACE, RAISE_MEMBER, None)],
                         "emit_signal 的实参顺序是 (destination, path, interface, member, parameters)"
                         " —— 这台机实测过，别改顺序")

    def test_probe_member_is_a_different_member(self):
        self.assertNotEqual(RAISE_MEMBER, RAISE_PROBE_MEMBER, "Probe 与 Raise 不能同名")

    def test_settle_is_a_frame_not_a_second(self):
        self.assertLess(RAISE_SETTLE_MS, 200, "单位是毫秒：超过 200ms 用户会觉得 Caps+Enter 变慢")
        self.assertLess(RAISE_JOURNAL_WAIT_MS, 2_000, "判活那一下在后台线程，但也不许拖到秒级")


class TestConfigKey(unittest.TestCase):
    def test_roundtrip_keeps_the_value(self):
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "qbar.toml"
        SC.save_config(SC.BUILTIN_LINKS, "b", "", True, path=f, raise_browser=False)
        self.assertFalse(SC.load_config(f).raise_browser)
        self.assertIn("raise_browser = false", f.read_text(encoding="utf-8"))

    def test_legacy_config_without_the_key_defaults_to_on(self):
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "qbar.toml"
        f.write_text('[general]\ndefault_search = "b"\n\n[[link]]\ntrigger = "b"\n'
                     'name = "必应"\nurl = "https://www.bing.com/search?q={q}"\ntype = "search"\n',
                     encoding="utf-8")
        cfg = SC.load_config(f)
        self.assertTrue(cfg.raise_browser)
        self.assertEqual(cfg.error, "")

    def test_bad_type_gives_a_human_message_and_keeps_working(self):
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "qbar.toml"
        f.write_text('[general]\nraise_browser = "yes"\n', encoding="utf-8")
        cfg = SC.load_config(f)
        self.assertTrue(cfg.raise_browser, "写错了要退回默认值，不能让界面瘫")
        self.assertIn("raise_browser 必须是 true / false", cfg.error)


class TestQbarActsBeforeOpening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def test_open_url_calls_the_raise_helper(self):
        target = _functions(self.tree, "_open_url")
        self.assertTrue(target, "没有 _open_url 了？")
        calls = {n.func.attr for n in ast.walk(target[0])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn("_raise_browser_window", calls,
                      "打开网址前不再先激活浏览器 —— 会退回『另起新窗口』那个老毛病")

    def test_helper_uses_the_signal_and_never_blocks_the_open(self):
        helpers = _functions(self.tree, "_raise_browser_window")
        self.assertTrue(helpers, "_raise_browser_window 不见了")
        src = ast.unparse(helpers[0])
        self.assertIn("raise_browser_signal(", src, "得走 GI 原生 emit_signal，别再退回起 gdbus 进程")
        self.assertIn("except Exception", src, "任何异常都必须吞掉")
        tail = src.split("except Exception", 1)[1]
        self.assertNotIn("raise ", tail, "except 分支里不许再抛 —— 那会直接拦下『打开网址』这件正事")

    def test_the_guard_itself_is_not_vacuous(self):
        """反例具型：缺调用 / 又去起子进程 / except 里抛 —— 都得被抓到。"""
        missing = ast.parse("class W:\n    def _open_url(self, url):\n        return 1\n")
        calls = {n.func.attr for n in ast.walk(missing)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("_raise_browser_window", calls)

        bad_src = ("def _raise_browser_window(self):\n"
                   "    try:\n        subprocess.run(['gdbus'])\n"
                   "    except Exception:\n        raise\n")
        bad = ast.unparse(ast.parse(bad_src))
        self.assertNotIn("raise_browser_signal(", bad)
        self.assertRegex(bad.split("except Exception", 1)[1], r"\braise\b",
                         "except 里 bare raise 也算往外抛，判据得认")


class TestExtensionShape(unittest.TestCase):
    """扩展文件本身：形状错了就是静默不加载，所以逐条钉住。"""

    @classmethod
    def setUpClass(cls):
        cls.js = (EXT_DIR / "extension.js").read_text(encoding="utf-8")

    def test_metadata_uuid_matches_directory_name(self):
        meta = json.loads((EXT_DIR / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["uuid"], EXT_DIR.name, "uuid 必须等于目录名，否则 shell 不认")
        self.assertTrue(meta["shell-version"], "shell-version 不能空着")

    def test_exports_a_default_class(self):
        """GNOME 45+ 的加载形状。旧形态的实测下场：journal 里
        `TypeError: extensionModule.default is not a constructor`，扩展不加载，
        现象与没装一模一样，而 Qbar 侧完全看不出来。"""
        self.assertIn("export default class", self.js)
        self.assertIsNone(re.search(r"^export function (enable|disable)", self.js, re.M),
                          "不许用**模块级** export function enable/disable（GNOME 42 之前的旧形态）")

    def test_does_not_use_the_dead_dbus_apis(self):
        """本机 GJS 实测：这些要么 undefined，要么只剩没法用的 C 绑定。"""
        # 只禁**带括号的调用**：文件头注释里要解释这些 API 为什么不能用，
        # 裸子串匹配会打到注释上（这个坑我今天踩过两次了）。
        for dead in ("export_object_path(", "register_object(", "return_error_literal("):
            self.assertNotIn(dead, self.js,
                             f"{dead} 在本机 GJS 上不可用（undefined 或只剩 C 绑定），不许再调用它")

    def test_uses_signal_subscribe(self):
        self.assertIn("signal_subscribe(", self.js)
        # 7 个实参：sender, interface, member, object_path, arg0, user_data, callback（回调在最后）
        # 本机实测的正确形状：6 个前置实参之后**回调在最后**
        #   signal_subscribe(null, IFACE, MEMBER, null, null, null, () => …)
        hits = re.findall(r"signal_subscribe\(\s*null,\s*IFACE,\s*M_\w+,\s*null,\s*null,\s*null,\s*\(\)\s*=>",
                          self.js)
        self.assertEqual(len(hits), 2,
                         "两处订阅（Raise / Probe）都必须是「7 个实参、回调在最后」那一形；"
                         f"实际匹配到 {len(hits)} 处")

    def test_get_windows_is_called_without_arguments(self):
        """GNOME 50 的 `Shell.App.get_windows()` 是 0 参：带参数会在 journal 刷
        `Too many arguments … expected 0, got 1`（本机 18:47 实测），噪音之外还可能漏掉窗口。"""
        js = (EXT_DIR / "extension.js").read_text(encoding="utf-8")
        compact = js.replace(" ", "").replace("\n", "")
        self.assertNotIn("get_windows(false", compact, "get_windows 又带参数了")
        self.assertNotIn("get_windows(true", compact, "同上")
        self.assertIn("get_windows()", compact, "应该是无参调用")

    def test_only_touches_current_workspace_windows(self):
        self.assertIn("get_workspace", self.js, "必须只激活当前工作区的窗口（不许把人从别处拽来）")
        self.assertIn("off-current-workspace", self.js, "不在当前工作区时要有明确的拒绝路径")

    def test_logs_both_outcomes(self):
        for needed in ("console.log", "console.error", "ready "):
            self.assertIn(needed, self.js, f"少了 {needed}：加载/执行失败必须在 journal 里留话")

    def test_actually_calls_activate_and_resolves_default_browser(self):
        for needed in ("get_tab_list", "activate(", "x-scheme-handler/https", "lookup_app"):
            self.assertIn(needed, self.js, f"扩展里少了 {needed} —— 它就不干这活了")


class TestExtensionStatusReport(unittest.TestCase):
    """`capsplus status` 的扩展体检：状态要读得到，且每种状态都带下一步动作。

    存在理由：扩展加载失败是**静默**的（只在 journal 留一行），Qbar 走信号又没有回复通道，
    用户只能看到"还是新窗口"。2026-10-16 我因此白让明江重登了一次 —— 现场必须能自己说清。
    """

    # 判据用的是模块级名字，不写 `cls.fn = extension_status`：
    # 函数赋给类属性会走描述符协议变成绑定方法，调用时 self 抢占第一个形参，
    # 报 "got multiple values for argument 'runner'"（`lambda` 包一层也一样会被绑定）。

    def fake(self, mode):
        def runner(argv):
            if argv[1] == "list":
                # missing 那种现场 uuid 本来就不在启用列表里 —— 夹具不能自相矛盾
                # （第一版三种 mode 都返回"已启用"，于是"不存在"被读成 unknown）。
                return "" if mode == "missing" else EXT_UUID + "\n"
            if argv[1] == "info":
                if mode == "active":
                    return "  已启用: 是\n  状态: ACTIVE\n"
                if mode == "error":
                    return "  已启用: 是\n  状态: ERROR\n"
                return '扩展"%s"不存在\n' % EXT_UUID
            return ""
        return runner

    def test_active_reads_active(self):
        state, hint = extension_status(runner=self.fake("active"), which=_HAS_EXE)
        self.assertEqual(state, "ACTIVE")
        self.assertIn("正常", hint)

    def test_error_state_points_at_the_relogin_trap(self):
        """ERROR 是最坑的一种：disable/enable 不会重新 import，必须提示重登与 journal。"""
        state, hint = extension_status(runner=self.fake("error"), which=_HAS_EXE)
        self.assertEqual(state, "ERROR")
        for needed in ("重登", "journalctl"):
            self.assertIn(needed, hint, f"提示里少了「{needed}」，用户就不知道该干什么")

    def test_missing_extension_says_install_and_relogin(self):
        state, hint = extension_status(runner=self.fake("missing"), which=_HAS_EXE)
        self.assertEqual(state, "missing")
        self.assertIn("install-extension.sh", hint)
        self.assertIn("重登", hint)

    def test_non_gnome_is_not_an_error(self):
        state, hint = extension_status(runner=self.fake("active"), which=_NO_EXE)
        self.assertEqual(state, "no-gnome")
        self.assertIn("不适用", hint)

    def test_status_prints_the_row(self):
        src = (REPO / "capsplus" / "apply.py").read_text(encoding="utf-8")
        self.assertIn("extension_status()", src, "status 里没有扩展体检行 —— 静默失效又看不见了")
        self.assertIn("浏览器扩展", src)

    def test_sync_unit_exists_and_is_oneshot(self):
        p = REPO / "packaging" / "capsplus-extension-sync.service"
        self.assertTrue(p.exists(), "登录校验 unit 不见了 ⇒ 扩展落后时又没人说")
        t = p.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", t)
        # 2026-10-20 统一入口：登录校验不再单独 enable，而是由 `capsplus.target` 的 Wants= 带起来
        # （两个开关各管一半，正是"关不干净"的来源）。target 自己才挂在 graphical-session 上。
        self.assertIn("WantedBy=capsplus.target", t,
                      "登录校验没挂到 capsplus.target ⇒ 它得被单独 enable，又变成两个开关")
        target = (REPO / "packaging" / "capsplus.target").read_text(encoding="utf-8")
        self.assertIn("capsplus-extension-sync.service", target, "target 没把校验 unit 收进来")
        self.assertIn("WantedBy=graphical-session.target", target,
                      "整组单元没有挂在图形会话上 ⇒ 登录后不会自启")
        self.assertTrue(unit_is_verify(t),
                        "登录那一侧必须**只校验**（--verify）：写文件正是 8190 半截文件那个竞态的根源，"
                        f"实际 ExecStart：{[l for l in t.splitlines() if l.startswith('ExecStart')]}")

    def test_install_script_honours_quiet(self):
        t = (REPO / "tools" / "install-extension.sh").read_text(encoding="utf-8")
        # 2026-10-19：这条原先钉字面量 `"$1" = "--quiet"`，那个写法在 `set -eu` 下**无参运行会当场崩**
        # （`parameter not set`，脚本一个文件都不复制 —— 实测过）。
        # 2026-10-20 改成 `for _arg in "$@"` 逐个解析（要同时认 --sync / --verify / --quiet）：
        # 逐个展开 `"$@"` 本身就安全（无参时循环零次），这条钉的形态跟着改，牙口不变——
        # 谁把参数解析换回裸 `$1`，上面的 TestInstallScriptModes 会先红（它真跑无参模式）。
        self.assertIn('for _arg in "$@"', t, "参数解析回到裸 $1 了？无参运行会在 set -eu 下崩")
        self.assertIn("--quiet) QUIET=1", t, "--quiet 分支不见了")
        self.assertIn("say()", t, "输出没走 say()，--quiet 就是假的")


def unit_is_verify(text: str) -> bool:
    """装配的 unit 是不是**只校验**：ExecStart 里有 --verify，且没有 --sync。

    拆成纯函数是为了能在同一处给出反例具型（`TestUnitIsVerifyNotWrite`）——
    "判据抓不住旧写法"就是没有判据。
    """
    execs = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("ExecStart=")]
    return any("install-extension.sh" in ln and "--verify" in ln and "--sync" not in ln
               for ln in execs)


class TestUnitIsVerifyNotWrite(unittest.TestCase):
    """A 条：登录那一侧只校验、一个字节都不写（2026-10-20 的 8190 半截文件事故就是登录时 `cp` 引起的）。"""

    OLD_WRITE_UNIT = ("[Service]\nType=oneshot\n"
                      "ExecStart=@REPO_DIR@/tools/install-extension.sh --quiet\n")

    def test_old_write_semantics_is_rejected(self):
        self.assertFalse(unit_is_verify(self.OLD_WRITE_UNIT),
                         "旧的『登录时同步』写法必须被判红（那次事故就是它）")

    def test_explicit_sync_is_rejected(self):
        self.assertFalse(unit_is_verify(
            "[Service]\nExecStart=@REPO_DIR@/tools/install-extension.sh --sync\n"),
            "unit 里出现 --sync（写入）就该红：写入只允许在安装侧")

    def test_both_flags_together_is_rejected(self):
        self.assertFalse(unit_is_verify(
            "[Service]\nExecStart=/x/tools/install-extension.sh --verify --sync\n"),
            "同时带 --verify 与 --sync 是自相矛盾，不许蒙过去")

    def test_current_unit_passes(self):
        t = (REPO / "packaging" / "capsplus-extension-sync.service").read_text(encoding="utf-8")
        self.assertTrue(unit_is_verify(t))


# ------------------------------------------------------------------- 三种模式真跑一遍

EXT_UUID_ = EXT_UUID            # 本文件上面已从 capsplus.extcheck 导入同名常量


def _md5(path: str) -> str:
    import hashlib
    return hashlib.md5(pathlib.Path(path).read_bytes()).hexdigest()


class InstallScriptSandbox:
    """把 `tools/install-extension.sh` 放进一个**什么都碰不到**的沙箱里真跑。

    三条不碰真机的要求（穷举过）：
      * `HOME` 指到临时目录 ⇒ 部署副本写不到明江的 ~/.local；
      * `PATH` 最前面放假 `gsettings` ⇒ 绝不去读/写他真实的 dconf（`--sync` 会写启用列表！）；
      * 假 `gdbus` / `notify-send` ⇒ 失败路径的桌面通知只落在夹具日志里（**不许弹到他屏幕上**）。
    """

    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.bindir = tmp / "bin"
        self.home.mkdir(parents=True, exist_ok=True)
        self.bindir.mkdir(parents=True, exist_ok=True)
        self.gsettings_log = tmp / "gsettings.log"
        self.notify_log = tmp / "notify.log"
        self.cur = "[]"
        self.get_fails = False
        self._write_stubs()

    def _write_stubs(self) -> None:
        (self.bindir / "gsettings").write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$*" >> "$STUB_GSETTINGS"\n'
            'case "$1" in\n'
            '  get) [ "$STUB_GET_FAIL" = 1 ] && exit 1\n'
            '       printf "%s\\n" "$STUB_CUR"; exit 0 ;;\n'
            '  set) exit 0 ;;\n'
            'esac\n'
            'exit 0\n', encoding="utf-8")
        for name in ("gdbus", "notify-send"):
            (self.bindir / name).write_text(
                '#!/bin/sh\n'
                'printf "%s %s\\n" "' + name + '" "$*" >> "$STUB_NOTIFY"\n'
                'exit 0\n', encoding="utf-8")
        for p in self.bindir.iterdir():
            p.chmod(0o755)

    def env(self) -> dict:
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "PATH": str(self.bindir) + os.pathsep + os.environ.get("PATH", ""),
            "STUB_GSETTINGS": str(self.gsettings_log),
            "STUB_NOTIFY": str(self.notify_log),
            "STUB_CUR": self.cur,
            "STUB_GET_FAIL": "1" if self.get_fails else "0",
            # 双保险：就算夹具里的假 gdbus 没被用到，也绝不把通知送到真会话总线上
            "DBUS_SESSION_BUS_ADDRESS": "",
        })
        return env

    def run(self, *args: str):
        return subprocess.run(["sh", str(REPO / "tools" / "install-extension.sh"), *args],
                              capture_output=True, text=True, env=self.env(), cwd=str(REPO),
                              timeout=60)

    @property
    def dst(self) -> pathlib.Path:
        return self.home / ".local/share/gnome-shell/extensions" / EXT_UUID_

    def src(self, name: str) -> pathlib.Path:
        return REPO / "extensions" / EXT_UUID_ / name

    def notified(self) -> str:
        return self.notify_log.read_text(encoding="utf-8") if self.notify_log.exists() else ""


class TestInstallScriptModes(unittest.TestCase):
    """`install-extension.sh` 的三种模式（默认 / --sync / --verify）真跑一遍。

    为什么必须真跑：这条脚本的三种语义全在 `if` 分支里，源码级判据只能证明"字在那儿"。
    2026-10-20 之前 `--verify` 根本不被识别（脚本只认 `--quiet`，其余参数当没看见）⇒
    登录 unit 说"只校验"、实际每次登录都在写文件。
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-ext-modes-"))
        # 夹具自己清场（2026-10-19 的教训：一天攒了 143 个临时目录没人收）
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.box = InstallScriptSandbox(self.tmp)

    def test_default_mode_deploys_and_enables(self):
        """无参 = --sync：真的复制 + md5 一致 + 把 uuid 写进启用列表。"""
        proc = self.box.run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("extension.js", "metadata.json", "stylesheet.css"):
            self.assertTrue((self.box.dst / name).exists(), f"{name} 没落盘")
            self.assertEqual(_md5(str(self.box.src(name))), _md5(str(self.box.dst / name)),
                             f"{name} 部署副本与仓内不一致")
        self.assertIn("set org.gnome.shell enabled-extensions", self.box.gsettings_log.read_text())
        self.assertIn(EXT_UUID_, self.box.gsettings_log.read_text())

    def test_sync_is_idempotent_and_leaves_no_temp_files(self):
        """内容一致就**一个字节都不写**：第二次跑不许改 mtime（稳态登录不该碰这些文件）。"""
        self.assertEqual(self.box.run("--sync").returncode, 0)
        before = {n: (self.box.dst / n).stat().st_mtime_ns for n in ("extension.js", "metadata.json")}
        self.assertEqual(self.box.run("--sync").returncode, 0)
        after = {n: (self.box.dst / n).stat().st_mtime_ns for n in ("extension.js", "metadata.json")}
        self.assertEqual(before, after, "内容没变却又写了一遍：稳态下这会给 shell 读到半截文件的机会")
        leftovers = [p.name for p in self.box.dst.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [], f"原子落盘留下了临时文件：{leftovers}")

    def test_verify_passes_when_in_sync_and_enabled(self):
        self.box.run("--sync")
        self.box.cur = "['" + EXT_UUID_ + "']"
        proc = self.box.run("--verify")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("已就位", proc.stdout)
        self.assertIn("ext-bench", proc.stdout, "校验通过时也要告诉人下一步是自检，不是重登")

    def test_verify_quiet_is_silent_on_success(self):
        self.box.run("--sync")
        self.box.cur = "['" + EXT_UUID_ + "']"
        proc = self.box.run("--verify", "--quiet")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", "--quiet 下成功还说话，unit 日志就成了噪音")

    def test_verify_reports_lag_and_writes_nothing(self):
        """**核心判据**：部署副本落后于仓时，verify 只报告、绝不写回（写回就是 8190 事故重演）。"""
        self.box.run("--sync")
        self.box.cur = "['" + EXT_UUID_ + "']"
        lagging = self.box.dst / "extension.js"
        lagging.write_text(lagging.read_text(encoding="utf-8") + "\n// 落后者\n", encoding="utf-8")
        before = _md5(str(lagging))
        self.box.gsettings_log.write_text("", encoding="utf-8")
        proc = self.box.run("--verify")
        self.assertEqual(proc.returncode, 1, "落了后必须非 0（unit 才会红在面板上）")
        self.assertIn("落后", proc.stderr)
        self.assertIn("install-extension.sh --sync", proc.stderr, "要给出可执行的下一步")
        self.assertEqual(_md5(str(lagging)), before, "verify 居然改了文件 —— 这正是当初的竞态根源")
        self.assertNotIn("set ", self.box.gsettings_log.read_text(),
                         "verify 不许写 enabled-extensions（只读）")
        self.assertIn("install-extension.sh --sync", self.box.notified(),
                      "失败必须弹通知，且通知里要带可执行的动作")

    def test_verify_reports_missing_deployment(self):
        proc = self.box.run("--verify")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("还没部署", proc.stderr)
        self.assertFalse(self.box.dst.exists(), "verify 不许顺手把目录建出来（一个字节都不写）")

    def test_verify_reports_not_enabled(self):
        self.box.run("--sync")
        self.box.cur = "[]"
        proc = self.box.run("--verify")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("enabled-extensions", proc.stderr)
        self.assertIn("不会加载", proc.stderr)

    def test_sync_survives_without_gnome(self):
        """读不到 gsettings（非 GNOME / 没 schema）不许把安装带崩 —— 那正是 CI 的形态。"""
        self.box.get_fails = True
        proc = self.box.run("--sync")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("读不到", proc.stderr)
        self.assertTrue((self.box.dst / "extension.js").exists())
        proc = self.box.run("--verify")
        self.assertEqual(proc.returncode, 0,
                         "读不到启用列表 ≠ 没启用：verify 不许因此假红（CI 上会整片红）")

    def test_unknown_argument_is_refused(self):
        proc = self.box.run("--wat")
        self.assertEqual(proc.returncode, 2, "不认识的参数要显式拒绝，别默默当 --sync 干活")
        self.assertIn("--sync", proc.stderr, "错误信息里要列可用参数")



class TestExtensionAwareness(unittest.TestCase):
    """A 方案：扩展能应答（ProbeReport），Qbar 据此决定要不要等那一帧，并把实测状态写盘。

    为什么不能只靠 `gnome-extensions info`：那说的是 shell 的观点。shell 说 ACTIVE 而 Qbar
    收不到应答，链路照样是断的（反之亦然）⇒ 两个读数都要有，别用一个替代另一个。
    """

    def test_the_dead_reply_path_is_gone(self):
        """`ProbeReport` 那条路实测走不通 ⇒ 符号必须真的不存在，别留半成品。"""
        import qbar.logic as L
        self.assertFalse(hasattr(L, "RAISE_REPORT_MEMBER"), "死常量还留着")
        self.assertFalse(hasattr(L, "subscribe_probe_report"), "发不通的订阅助手还留着")
        self.assertNotIn("ProbeReport", (RAISE_MEMBER, RAISE_PROBE_MEMBER))

    def test_parse_probe_report_reads_numbers(self):
        info = parse_probe_report("default=microsoft-edge.desktop windows=3 on-current=2")
        self.assertEqual((info["default"], info["windows"], info["on_current"]),
                         ("microsoft-edge.desktop", 3, 2))

    def test_parse_probe_report_never_raises(self):
        for junk in ("", None, "garbage", "windows=abc", "windows="):
            info = parse_probe_report(junk)
            self.assertIsInstance(info, dict)

    def test_no_blind_wait_when_extension_known_absent(self):
        self.assertTrue(should_settle(None), "还没问过要等一帧（第一次不能赌）")
        self.assertTrue(should_settle(True))
        self.assertFalse(should_settle(False), "已知没扩展还等 80ms，就是白等")

    def test_journal_helpers_are_pure_and_lenient(self):
        """判活改走 journal：命令 argv 与"取最后一条 probe 行"都必须是纯函数、不抛。"""
        argv = journal_probe_command("2026-10-16 18:00:00")
        self.assertEqual(argv[:3], ["journalctl", "--user", "--since"])
        self.assertIn("-o", argv)
        text = ("噪声\n[raise-browser] probe windows=1 on-current=1\n"
                "[raise-browser] probe windows=3 on-current=2\n")
        self.assertEqual(latest_probe_report(text), "windows=3 on-current=2")
        self.assertIsNone(latest_probe_report(""))
        self.assertIsNone(latest_probe_report(None))

    def test_open_url_still_asks_then_opens(self):
        tree = ast.parse(APP.read_text(encoding="utf-8"))
        helper = _functions(tree, "_raise_browser_window")[0]
        src = ast.unparse(helper)
        self.assertIn("raise_browser_signal(", src)
        self.assertIn("RAISE_PROBE_MEMBER", src, "发完激活还得问一次活着没")
        self.assertIn("should_settle(", src, "等不等要按实测状态，不许写死")

    def test_extension_does_not_try_to_reply_from_inside_the_shell(self):
        """扩展侧不许再 `emit_signal`：本机实测在 shell 进程里发不出包
        （外部发的 Probe 它收得到、journal 也有 probe 行，但 dbus-monitor 抓 0 条回包，
        且没有任何 JS ERROR）⇒ 判活一律读 journal，别留一条走不通的路在里面。"""
        js = (EXT_DIR / "extension.js").read_text(encoding="utf-8")
        self.assertNotIn("emit_signal(", js, "扩展里不要试图发 D-Bus 包（实测上不了总线）")
        self.assertIn("journal", js.lower(), "应答路径要写清楚：靠 journal")

    def test_state_payload_is_valid_json_and_roundtrips(self):
        """写盘格式必须能被读回来 —— 之前手拼 JSON 把 None 写进去，status 直接读不了。
        这条是**写→读闭环**，只测读的那一半拦不住。"""
        import json
        sys.path.insert(0, str(REPO))
        from capsplus.extcheck import qbar_observed
        from qbar.logic import ext_state_payload

        for alive, info in ((True, {"windows": 3, "on_current": 2, "default": "microsoft-edge.desktop"}),
                            (False, {}), (None, {})):
            text = ext_state_payload(alive, info, 1700000000)
            data = json.loads(text)                    # 非法 JSON 在这一步就炸
            self.assertEqual(data["alive"], alive)
            self.assertIn("at", data)

        d = pathlib.Path(tempfile.mkdtemp()) / "raise-browser.json"
        d.write_text(ext_state_payload(True, {"windows": 7, "on_current": 1,
                                              "default": "microsoft-edge.desktop"}, 1700000000),
                     encoding="utf-8")
        state, shown = qbar_observed(path=str(d))
        self.assertTrue(state)
        self.assertIn("windows=7", shown)

    def test_status_shows_both_readings(self):
        src = (REPO / "capsplus" / "apply.py").read_text(encoding="utf-8")
        self.assertIn("qbar_observed()", src, "status 只有 shell 的观点，链路断了看不见")
        self.assertIn("Qbar 实测", src)

    def test_qbar_observed_reads_state_file(self):
        sys.path.insert(0, str(REPO))
        from capsplus.extcheck import qbar_observed
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "raise-browser.json"
        state, text = qbar_observed(path=str(f))
        self.assertIsNone(state, "文件不存在要报'还没问过'，不能瞎猜 True")
        f.write_text('{"alive": true, "windows": 2, "on-current": 0, "on_current": 1, '
                     '"default": "microsoft-edge.desktop", "at": 1}', encoding="utf-8")
        state, text = qbar_observed(path=str(f))
        self.assertTrue(state)
        self.assertIn("windows=2", text)
        f.write_text("{坏掉的 json", encoding="utf-8")
        state, text = qbar_observed(path=str(f))
        self.assertIsNone(state, "读不了也要给一句人话，不能抛")


class TestPumpNotSleep(unittest.TestCase):
    """信号要发得出去，必须**跑主循环**；`GLib.usleep` 那种写法等于没发。

    2026-10-16 血案：A/B 工具用真 MainLoop 测出 +0（原窗口开标签页），
    而 Qbar 里写的是 `GLib.usleep(80_000)` —— 主线程睡过去，主循环不转，
    队列里的信号一条都没写出去，所以明江重登后手动测"还是一样"。
    """

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(APP.read_text(encoding="utf-8"))
        cls.src = APP.read_text(encoding="utf-8")

    def test_raise_path_pumps_instead_of_sleeping(self):
        helper = _functions(self.tree, "_raise_browser_window")[0]
        body = ast.unparse(helper)
        self.assertIn("_pump(", body, "激活之后要跑主循环把信号发出去")
        self.assertNotIn("usleep", body, "GLib.usleep 等于没发：不许回到那种写法")
        self.assertIn("RAISE_SETTLE_MS", body)

    def test_pump_helper_exists_and_uses_mainloop(self):
        pump = _functions(self.tree, "_pump")
        self.assertTrue(pump, "少了 _pump")
        body = ast.unparse(pump[0])
        self.assertIn("GLib.MainLoop()", body)
        self.assertIn("loop.run()", body)
        self.assertIn("timeout_add", body, "要有超时退出，否则主循环回不来")

    def test_liveness_reads_journal_not_dbus_reply(self):
        helper = _functions(self.tree, "_start_ext_watch")
        self.assertTrue(helper, "判活方法不见了")
        body = ast.unparse(helper[0])
        for needed in ("journal_probe_command", "latest_probe_report", "threading.Thread"):
            self.assertIn(needed, body, f"判活里少了 {needed}")
        self.assertNotIn("signal_subscribe", body, "不要再订阅 D-Bus 应答（扩展回不了包）")

    def test_no_uppercase_constant_used_without_import(self):
        """引用了 `RAISE_*` 这类大写常量却没导入/没定义 ⇒ 一走到那条分支就 NameError。

        今天真实发生过：常量改名（RAISE_SETTLE_US → RAISE_SETTLE_MS）时 qbar_app 没跟上，
        工作树是破的，而当时所有判据都还是绿的 —— 因为没人执行过那条分支。
        """
        tree = ast.parse(self.src)
        defined = set()
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for tg in targets:
                    if isinstance(tg, ast.Name):
                        defined.add(tg.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for al in node.names:
                    imported.add(al.asname or al.name)
            elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.ClassDef):
                defined.add(node.name)
        logic_names = {n for n in dir(__import__("qbar.logic", fromlist=["x"]))}
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id.isupper() and len(node.id) > 4:
                if node.id not in defined and node.id not in imported and node.id not in logic_names:
                    bad.append((node.id, node.lineno))
        self.assertFalse(bad, f"这些大写常量既没定义也没导入（一走到就是 NameError）：{bad}")


class TestTools(unittest.TestCase):
    def test_install_script_parses_with_its_own_shebang(self):
        script = REPO / "tools" / "install-extension.sh"
        interp = script.read_text(encoding="utf-8").splitlines()[0].lstrip("#!").strip() or "/bin/sh"
        proc = subprocess.run([interp, "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"{interp} -n 失败：{proc.stderr}")

    def test_verifier_compiles(self):
        script = REPO / "tools" / "verify-raise-browser.py"
        proc = subprocess.run([sys.executable, "-m", "py_compile", str(script)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ------------------------------------------------- 无重登自检的判词（喂日志，不起 shell）

# 这份样本是**真跑出来的**（2026-10-20 用 `sh tools/ext-bench.sh --keep-log` 抓的 HEAD 版本日志），
# 特意保留了两条会让人误判的噪音行：`Failed to register AuthenticationAgent` 与别的扩展的警告。
# 判据必须只认扩展自己的记号，不许因为"日志里出现 Failed/ERROR 就红"。
BENCH_OK_LOG = """libmutter-Message: 18:56:23.889: Running GNOME Shell (using mutter 50.1) as a Wayland display server
GNOME Shell-Message: 18:56:24.375: Failed to register AuthenticationAgent
GNOME Shell-Message: 18:56:24.382: [raise-browser] ready capture=on pin=on clear=on toolbar+menu+modal=on promisified=true default=microsoft-edge.desktop windows=0 on-current=0
GNOME Shell-Message: 18:56:24.382: [raise-browser] 已启用 iface=io.github.capsplus.RaiseBrowser path=/io/github/capsplus/RaiseBrowser
(gnome-shell:35410): Gjs-WARNING **: 18:56:24.589: Type GITypeInfo of property NM.Object::client does not match return type
"""

BENCH_SYNTAX_ERROR_LINE = (
    "(gnome-shell:36283): GNOME Shell-CRITICAL **: 18:56:38.408: Extension "
    "raise-browser@capsplus.example: SyntaxError: unexpected token: identifier @ "
    "file:///home/zmj/.local/share/gnome-shell/extensions/raise-browser@capsplus.example/"
    "extension.js:990:6")


class TestBenchVerdict(unittest.TestCase):
    """B 条：`extcheck.bench_verdict` 的判词（把日志喂进来，单测里**不起** shell）。

    为什么这一刻意分出去（2026-10-20）：判"扩展加载成功没"只有日志一条路（本机 gjs 导出不了
    D-Bus 对象，`gdbus … Probe` 报 UnknownMethod），而日志解析最容易被写成"grep 到某个词就算绿"。
    所以判词做成纯函数，正例/反例全部在单测里钉住；`tools/ext-bench.sh` 只负责起 shell 和收尸。
    """

    def test_healthy_log_is_green(self):
        ok, why = bench_verdict(BENCH_OK_LOG)
        self.assertTrue(ok, why)
        self.assertEqual(why, "", "绿的时候不该有理由")

    def test_shell_load_error_is_red_with_the_offending_line(self):
        """实测口径：shell 自己报 `Extension <uuid>: …`，任何一类错误都长这样。"""
        ok, why = bench_verdict("Running GNOME Shell\n" + BENCH_SYNTAX_ERROR_LINE + "\n")
        self.assertFalse(ok)
        self.assertIn("SyntaxError", why, "把 shell 的原话端点出来，人才知道改哪")
        self.assertIn("extension.js", why)

    def test_the_three_documented_failure_words_are_red(self):
        for bad in ("启用失败", "malformed UTF-8 character sequence at offset 8190",
                    "TypeError: extensionModule.default is not a constructor"):
            with self.subTest(bad=bad):
                ok, why = bench_verdict("Running GNOME Shell\n" + bad + "\n")
                self.assertFalse(ok, f"「{bad}」必须判红")
                self.assertTrue(why)

    def test_loaded_but_channels_not_hooked_is_red(self):
        """加载成功但订阅没挂上：现象与没装一模一样，这是最容易漏的一条。"""
        log = ("Running GNOME Shell\n"
               "GNOME Shell-Message: [raise-browser] ready capture=off pin=off clear=on\n")
        ok, why = bench_verdict(log)
        self.assertFalse(ok)
        self.assertIn("capture", why)

    def test_shell_never_started_says_so(self):
        ok, why = bench_verdict("")
        self.assertFalse(ok)
        self.assertIn("没起来", why)

    def test_extension_absent_gives_the_install_and_enable_actions(self):
        ok, why = bench_verdict("libmutter-Message: Running GNOME Shell (using mutter 50.1)\n")
        self.assertFalse(ok)
        self.assertIn("install-extension.sh --sync", why)
        self.assertIn(EXT_UUID, why)

    def test_mentioned_but_no_ready_line_points_at_enable(self):
        ok, why = bench_verdict("Running GNOME Shell\n"
                                "GNOME Shell-Message: [raise-browser] 开始 enable\n")
        self.assertFalse(ok)
        self.assertIn("ready", why)

    def test_the_green_verdict_has_teeth(self):
        """反例具型：把 ready 行拿掉，同一条日志必须立刻变红（否则"绿"只是日志里有字）。"""
        broken = "\n".join(ln for ln in BENCH_OK_LOG.splitlines() if "ready" not in ln)
        self.assertNotIn("[raise-browser] ready", broken)
        ok, _ = bench_verdict(broken)
        self.assertFalse(ok)

    def test_none_and_garbage_never_raise(self):
        for junk in (None, "", "\x00\xff", "随便几行"):
            with self.subTest(junk=junk):
                ok, why = bench_verdict(junk)
                self.assertFalse(ok)
                self.assertIsInstance(why, str)


class TestBenchCliContract(unittest.TestCase):
    """`python3 -m capsplus.extcheck --bench-log` 是 sh 与 Python 之间的**契约**（退出码是判据）。"""

    def _run(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        return subprocess.run([sys.executable, "-m", "capsplus.extcheck", *args],
                              capture_output=True, text=True, env=env, cwd=str(REPO), timeout=30)

    def test_exit_codes(self):
        d = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-bench-cli-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        good = d / "good.log"
        good.write_text(BENCH_OK_LOG, encoding="utf-8")
        bad = d / "bad.log"
        bad.write_text("Running GNOME Shell\n" + BENCH_SYNTAX_ERROR_LINE + "\n", encoding="utf-8")

        proc = self._run("--bench-log", str(good))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", "绿的时候别往 stdout 打东西（EXT_OK 由 sh 打）")

        proc = self._run("--bench-log", str(bad))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SyntaxError", proc.stderr, "红的时候原因必须在 stderr 里")

        proc = self._run("--bench-log", str(d / "nope.log"))
        self.assertEqual(proc.returncode, 2, "读不了日志是用法/环境问题，不许混成判据红（1）")

        proc = self._run()
        self.assertEqual(proc.returncode, 2)

def sh_code(text: str) -> str:
    """去掉整行注释后的脚本内容（注释里解释「为什么不许 pkill」是解释，不是实现）。"""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


class TestBenchScriptShape(unittest.TestCase):
    """`tools/ext-bench.sh` 自己的形状（CI 那条 `for f in tools/*.sh` 的 shebang 检查也得过）。"""

    SCRIPT = REPO / "tools" / "ext-bench.sh"

    def test_parses_with_its_own_shebang(self):
        interp = self.SCRIPT.read_text(encoding="utf-8").splitlines()[0].lstrip("#!").strip() or "/bin/sh"
        self.assertTrue(interp.endswith("sh"), f"shebang 应该是 /bin/sh，实际：{interp}")
        proc = subprocess.run([interp, "-n", str(self.SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"{interp} -n 失败：{proc.stderr}")

    def test_no_bashisms(self):
        """dash 不是 bash：数组 / `[[` / `((` / `${A[@]}` 在这台机上会**静默**炸掉整段脚本。"""
        code = sh_code(self.SCRIPT.read_text(encoding="utf-8"))
        for bad in ("=(", "shopt", "[[ ", "${A[@]}"):
            self.assertNotIn(bad, code, f"POSIX sh 脚本里出现了 bash 专用写法：{bad}")
        # `$(( … ))` 是 POSIX 算术展开，合法；不带 `$` 的 `(( … ))` 才是 bash 的算术命令
        self.assertNotRegex(code, r"(?<!\$)\(\( ", "用了 bash 的 `(( ))` 算术命令，dash 会整段炸")

    def test_only_kills_its_own_pid(self):
        """绝不许 `pkill -f gnome-shell`：那会杀掉明江正在用的会话（本仓踩过同类坑）。"""
        code = sh_code(self.SCRIPT.read_text(encoding="utf-8"))
        self.assertNotIn("pkill", code)
        self.assertNotIn("killall", code)
        self.assertIn('kill -TERM "$shell_pid"', code)
        self.assertIn('kill -KILL "$shell_pid"', code)

    def test_runs_in_an_isolated_session_bus(self):
        """自检必须关在 dbus-run-session 里：否则 headless shell 激活的那些服务会挂在真会话上。"""
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("dbus-run-session", text)
        self.assertIn("CAPSPLUS_BENCH_INNER", text, "外层/内层的分界不见了：重定向与收尸都会错位")

    def test_verdict_is_not_reimplemented_in_sh(self):
        """判词只许有一份（Python 那侧）；sh 里再 grep 一遍就是第二份真相。"""
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("capsplus.extcheck", text)
        self.assertNotIn("not a constructor", text, "sh 里在重复实现判词")
        self.assertNotIn("启用失败", text, "sh 里在重复实现判词")


if __name__ == "__main__":
    unittest.main(verbosity=2)
