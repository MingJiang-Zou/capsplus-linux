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
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar import search_config as SC          # noqa: E402
from qbar.logic import (RAISE_BUS_IFACE, RAISE_BUS_PATH, RAISE_MEMBER,   # noqa: E402
                        RAISE_PROBE_MEMBER, RAISE_SETTLE_US, raise_browser_signal)

from capsplus.extcheck import EXT_UUID, extension_status  # noqa: E402  体检函数（不碰 gi）
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
        self.assertLess(RAISE_SETTLE_US, 200_000, "等太久用户会觉得 Caps+Enter 变慢")


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
        self.assertTrue(p.exists(), "登录同步 unit 不见了 ⇒ 改过扩展又得手工 cp")
        t = p.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", t)
        self.assertIn("install-extension.sh --quiet", t)
        self.assertIn("WantedBy=graphical-session.target", t)

    def test_install_script_honours_quiet(self):
        t = (REPO / "tools" / "install-extension.sh").read_text(encoding="utf-8")
        self.assertIn('"$1" = "--quiet"', t, "install-extension.sh 不支持 --quiet，unit 会刷屏")
        self.assertIn("say()", t, "输出没走 say()，--quiet 就是假的")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
