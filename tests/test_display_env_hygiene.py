# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：代码里**不许把 `WAYLAND_DISPLAY` 塞进环境**（读它、pop 它都合法）。

为什么值得单独钉一条：2026-10-16 一天之内三次栽在同一件事上 —— 只要任何一处代码把
`WAYLAND_DISPLAY` 设进环境（`env = dict(os.environ, WAYLAND_DISPLAY="wayland-0")`、
`os.environ.setdefault(...)`、`os.environ["WAYLAND_DISPLAY"] = ...`），GTK3 就会优先连
真实 Wayland 会话；于是"我明明用 xvfb-run 包了"照样把窗口弹到用户屏幕上、Qbar 还抢键盘焦点。

读侧不算违规：`os.environ.get("WAYLAND_DISPLAY")`、`env.pop(...)`、shell 里的
`-u WAYLAND_DISPLAY` 都是"摘掉/检查"，正是我们要的方向。判据只看**写侧**，且走 AST ——
注释与文档字符串里举的反例不会被误判。
"""

import ast
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

VAR = "WAYLAND_DISPLAY"
SCAN_DIRS = ("tests", "tools", "capsplus", "qbar", "tray", "settings")


def finds_setting_of_wayland(source: str) -> list[str]:
    """返回"把 WAYLAND_DISPLAY 塞进环境"的位置（纯函数：判据与反例具型共用）。"""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == VAR:              # dict(..., WAYLAND_DISPLAY=…)
            hits.append(f"line {node.lineno}: 关键字参数 {VAR}=…")
        elif isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            fname = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if fname == "setdefault" and isinstance(first, ast.Constant) and first.value == VAR:
                hits.append(f"line {node.lineno}: setdefault('{VAR}', …)")
        elif isinstance(node, ast.Assign):                                  # os.environ[VAR] = …
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) \
                        and target.slice.value == VAR:
                    hits.append(f"line {target.lineno}: {VAR} 被赋值")
        elif isinstance(node, ast.Dict):                                    # {"WAYLAND_DISPLAY": …}
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == VAR:
                    hits.append(f"line {key.lineno}: 字典里写死 {VAR}")
    return hits


class TestNoForcedWayland(unittest.TestCase):
    def test_repo_code_never_sets_wayland_display(self):
        bad = []
        for name in SCAN_DIRS:
            for path in sorted((REPO / name).rglob("*.py")):
                if "nogi" in path.parts:            # 屏蔽 gi 的 sitecustomize 与显示无关
                    continue
                bad += [f"{path.relative_to(REPO)}: {msg}"
                        for msg in finds_setting_of_wayland(path.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], "这些地方会把子进程拽到真实 Wayland 桌面（弹窗抢焦点）：\n  "
                                  + "\n  ".join(bad))

    def test_the_guard_catches_the_four_real_patterns(self):
        """反例具型：今天真踩过的写法一种都不能漏。"""
        cases = (
            ('env = dict(os.environ, WAYLAND_DISPLAY="wayland-0")\n', "dict 关键字"),
            ('os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")\n', "setdefault"),
            ('os.environ["WAYLAND_DISPLAY"] = "wayland-0"\n', "下标赋值"),
            ('{"WAYLAND_DISPLAY": "wayland-0"}\n', "字典字面量"),
        )
        for src, tag in cases:
            self.assertTrue(finds_setting_of_wayland(src), f"判据漏了这种：{tag}")

    def test_reading_and_removing_are_legal(self):
        """正例：读它、pop 它 —— 都是正确方向，不许误报。"""
        ok = ('v = os.environ.get("WAYLAND_DISPLAY")\n'
              'os.environ.pop("WAYLAND_DISPLAY", None)\n'
              'if not os.environ.get("WAYLAND_DISPLAY"):\n'
              '    pass\n')
        self.assertEqual(finds_setting_of_wayland(ok), [])

    def test_comments_do_not_false_positive(self):
        """注释/文档字符串里举的反例不许被当成违规（走 AST 才做得到）。"""
        src = '# 上一版写了 WAYLAND_DISPLAY="wayland-0"，那是错的\n'
        self.assertEqual(finds_setting_of_wayland(src), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
