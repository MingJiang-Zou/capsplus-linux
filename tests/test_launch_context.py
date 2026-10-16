# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：Qbar 打开任何东西都**必须带激活上下文**，不许再传 `None`。

症状具体到不能不钉住（2026-10-16 用户实测）：默认浏览器（Edge）不回现有窗口，而是
**另起一个窗口**，之后 Qbar 打开的页面全堆进那个新窗口；只有焦点正好在浏览器上时才像"正常"。
根因是 `Gio.AppInfo.launch_default_for_uri(uri, None)` —— 第二个参数传 None 等于告诉合成器
"这不是一次用户主动启动"，拿不到 activation 上下文，浏览器侧就按"外部新起"处理。

这种**行为随焦点变**的毛病没法用单测直接断言（要人眼看浏览器），所以钉在源码层：
任何一处 `launch_default_for_uri` / `AppInfo.launch` 少了上下文就判红。headless 也跑。
"""

import ast
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TARGET = REPO / "qbar" / "qbar_app.py"
CALLS = {"launch_default_for_uri": 1, "launch": 1}     # 函数名 → launch context 的实参下标


def find_contextless_launches(source: str) -> list[str]:
    """返回"没带 launch context"的调用位置（纯函数：判据与反例具型都走它）。"""
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if name not in CALLS:
            continue
        index = CALLS[name]
        args = list(node.args)
        for kw in node.keywords or []:                  # 关键字写法 launch_context=… 也算带上
            if kw.arg == "launch_context" and len(args) <= index:
                args = args + [None] * (index - len(args)) + [kw.value]
        if len(args) <= index:
            bad.append(f"line {node.lineno}: {name}() 没传 launch context")
            continue
        given = args[index]
        is_none = (isinstance(given, ast.Constant) and given.value is None) or \
                  (isinstance(given, ast.Name) and given.id == "None")
        if is_none:
            bad.append(f"line {node.lineno}: {name}() 的 launch context 是 None")
    return bad


class TestQbarLaunchesCarryContext(unittest.TestCase):
    def test_qbar_app_has_no_contextless_launch(self):
        bad = find_contextless_launches(TARGET.read_text(encoding="utf-8"))
        self.assertEqual(bad, [],
                         "这些打开动作没带激活上下文（浏览器会另起窗口）：\n  " + "\n  ".join(bad))

    def test_the_guard_really_catches_the_old_code(self):
        """反例具型：把修之前的写法喂给判据必须被抓到，否则这判据是摆设。"""
        self.assertTrue(find_contextless_launches(
            "def f(self, url):\n    Gio.AppInfo.launch_default_for_uri(url, None)\n"),
            "判据抓不住 launch_default_for_uri(url, None)")
        self.assertTrue(find_contextless_launches(
            "def f(self, info):\n    info.launch(None, None)\n"),
            "判据抓不住 info.launch(None, None)")
        self.assertTrue(find_contextless_launches(
            "def f(self, url):\n    Gio.AppInfo.launch_default_for_uri(url)\n"),
            "判据抓不住压根没传上下文那一种")

    def test_the_guard_does_not_false_positive(self):
        """正例：带上下文的两种写法（位置参数 / 关键字）都不许误报。"""
        ok = ("def f(self, url):\n"
              "    ctx = self._launch_context()\n"
              "    Gio.AppInfo.launch_default_for_uri(url, ctx)\n"
              "    info.launch(files, launch_context=ctx)\n")
        self.assertEqual(find_contextless_launches(ok), [])

    def test_launch_context_helper_really_sets_a_timestamp(self):
        """上下文得真的带时间戳 —— Wayland 的 activation 认的就是这个。"""
        text = TARGET.read_text(encoding="utf-8")
        self.assertIn("get_app_launch_context()", text)
        self.assertIn("set_timestamp(", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
