# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：`raise_browser` 这一整条链路（配置键 → 纯逻辑 argv → Qbar 调用 → 仓内扩展文件）。

背景（2026-10-16 实测）：焦点不在浏览器时，Qbar 打开网址会让浏览器**另起一扇窗**。
根因是 GTK3 递不出 Wayland 激活令牌（`libgdk-3` 里 `XDG_ACTIVATION_TOKEN` 出现 0 次），
不是双报显示后端（修完现象不变），也不是另起进程（`msedge` 主进程数实测恒为 1）。
借 GNOME 扩展的特权先激活浏览器窗口，才谈得上"在原窗口开标签页"。

这条链上有三处特别容易被改坏、而且坏了**不会报错、只会静默退回旧行为**，所以都钉住：
1. 配置键的读/写/容错（写坏了要有人话提示，旧配置没有这个键要默认开）；
2. Qbar 必须"先激活再打开"，且**扩展不在时绝不拦下打开动作**（吞错误，不抛）；
3. 扩展文件本身（uuid 与目录名一致、真的调用了 `activate`）——不然装了个空的。
"""

import ast
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar import search_config as SC          # noqa: E402
from qbar.logic import (RAISE_BUS_IFACE, RAISE_BUS_NAME, RAISE_BUS_PATH,   # noqa: E402
                        RAISE_TIMEOUT_S, raise_browser_command)

APP = REPO / "qbar" / "qbar_app.py"
EXT_DIR = REPO / "extensions" / "raise-browser@capsplus.example"


def _functions(tree, name):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name]


class TestRaiseArgv(unittest.TestCase):
    def test_argv_is_a_list_without_shell_strings(self):
        argv = raise_browser_command()
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:2], ["gdbus", "call"])
        self.assertIn("--session", argv)
        self.assertIn(RAISE_BUS_NAME, argv)
        self.assertIn(RAISE_BUS_PATH, argv)
        self.assertEqual(argv[argv.index("--method") + 1], f"{RAISE_BUS_IFACE}.RaiseBrowser")

    def test_timeout_is_short_enough_not_to_stall_the_ui(self):
        self.assertLess(RAISE_TIMEOUT_S, 0.5, "超过 0.5s 用户就会觉得 Caps+Enter 变慢了")


class TestConfigKey(unittest.TestCase):
    def test_roundtrip_keeps_the_value(self):
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "qbar.toml"
        SC.save_config(SC.BUILTIN_LINKS, "b", "", True, path=f, raise_browser=False)
        self.assertFalse(SC.load_config(f).raise_browser)
        self.assertIn("raise_browser = false", f.read_text(encoding="utf-8"))

    def test_legacy_config_without_the_key_defaults_to_on(self):
        """改这个键之前写的配置（没有 raise_browser 这一行）必须照常工作并默认开。"""
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "qbar.toml"
        # 补一条 [[link]]：否则 error 里会有"没有定义任何 link"那句，把断言强度稀释掉
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
    """源码级：`_open_url` 必须先试激活；`_raise_browser_window` 必须吞错误不抛。"""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(APP.read_text(encoding="utf-8"))

    def test_open_url_calls_the_raise_helper(self):
        target = _functions(self.tree, "_open_url")
        self.assertTrue(target, "没有 _open_url 了？")
        calls = {n.func.attr for n in ast.walk(target[0])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn("_raise_browser_window", calls,
                      "打开网址前不再先激活浏览器 —— 会退回'另起新窗口'那个老毛病")

    def test_raise_helper_swallows_errors_and_bounds_time(self):
        helpers = _functions(self.tree, "_raise_browser_window")
        self.assertTrue(helpers, "_raise_browser_window 不见了")
        body = helpers[0]
        src = ast.unparse(body)
        self.assertIn("timeout=RAISE_TIMEOUT_S", src, "D-Bus 调用必须有超时上限")
        self.assertIn("except Exception", src, "扩展没装时必须吞掉异常")
        self.assertNotIn("raise ", src.split("except Exception")[-1],
                         "except 分支里不许再抛出去 —— 那会直接拦下'打开网址'这件正事")

    def test_the_guard_itself_is_not_vacuous(self):
        """反例具型：把两处破坏写法喂给同样的检查，必须被抓到。"""
        missing = ast.parse("class W:\n    def _open_url(self, url):\n        return 1\n")
        calls = {n.func.attr for n in ast.walk(missing)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("_raise_browser_window", calls)          # 检查器真的会判它缺
        bad = ast.parse("def _raise_browser_window(self):\n"
                        "    subprocess.run(x, timeout=99)\n"
                        "    try:\n        pass\n    except ValueError:\n        raise\n")
        src = ast.unparse(bad)
        self.assertNotIn("except Exception", src)                  # 窄 except 会被上面的判据拒
        self.assertIn("timeout=99", src)                            # 超时无上限的那一种能被看见


class TestExtensionFiles(unittest.TestCase):
    def test_metadata_uuid_matches_directory_name(self):
        import json
        meta = json.loads((EXT_DIR / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["uuid"], EXT_DIR.name, "uuid 必须等于目录名，否则 shell 不认")
        self.assertTrue(meta["shell-version"], "shell-version 不能空着")

    def test_extension_actually_activates_a_window(self):
        js = (EXT_DIR / "extension.js").read_text(encoding="utf-8")
        for needed in ("get_tab_list", "activate(", "RaiseBrowser", "x-scheme-handler/https"):
            self.assertIn(needed, js, f"扩展里少了 {needed} —— 它就不干了")
        self.assertIn("get_workspace", js, "必须只激活当前工作区的窗口（不许把人从别的工作区拽走）")

    def test_install_script_parses_with_its_own_shebang(self):
        script = REPO / "tools" / "install-extension.sh"
        interp = script.read_text(encoding="utf-8").splitlines()[0].lstrip("#!").strip() or "/bin/sh"
        proc = subprocess.run([interp, "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"{interp} -n 失败：{proc.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
