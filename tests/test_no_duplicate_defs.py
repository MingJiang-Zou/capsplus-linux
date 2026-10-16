# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：同一个类里**不许出现两个同名方法**。

为什么专门钉这一条（2026-10-16 实测踩到）：`tests/test_settings_window.py` 的
`TestSettingsWindow` 里定义了**两次** `setUpClass` —— 前一次是"`CAPSPLUS_GUI=1` 却跑不了就判红"
那道防假绿保险，后一次只是顺手 `cls.app_module = …` 一行。Python 按名字绑定、后者直接遮蔽前者，
于是保险变成**死代码**：环境不对时不再判红，而是走别的路子悄悄绿过去。这类事人眼扫一遍
根本看不出来（两处都"看着都对"），只有 AST 数得清。

范围只收**类体里的同名 `def`**：模块顶层的重复定义常常是 `try/except` 里的合法退路
（导入失败时给个替身），别把那种也判红。
"""

import ast
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
SCAN_DIRS = ("capsplus", "qbar", "tray", "settings", "tests", "tools", "bin")


def duplicate_class_methods(source: str) -> list[str]:
    """返回"同一个类里定义了两次以上的名字"（纯函数：判据与反例具型共用）。"""
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        seen = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen.setdefault(item.name, []).append(item.lineno)
            elif isinstance(item, ast.If):
                # 类体里 `if 某条件: def f(): …` 是故意的退路写法，不算重复；只在"无条件那一支"里数
                for sub in item.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        seen.setdefault(sub.name, []).append(sub.lineno)
        for name, lines in seen.items():
            if len(lines) > 1:
                bad.append(f"class {node.name}: {name}() 定义了 {len(lines)} 次（行 {lines}）")
    return bad


class TestNoDuplicateClassMethods(unittest.TestCase):
    def test_repo_has_no_duplicate_class_methods(self):
        bad = []
        for d in SCAN_DIRS:
            root = REPO / d
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.py")):
                if "nogi" in path.parts:
                    continue
                bad += [f"{path.relative_to(REPO)}: {m}"
                        for m in duplicate_class_methods(path.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], "同一个类里有同名方法（后一个会静默遮蔽前一个）：\n  "
                                  + "\n  ".join(bad))

    def test_the_guard_catches_the_real_accident(self):
        """反例具型：真踩过的那一种 —— 两个 setUpClass，第二个把保险顶掉。"""
        src = ("class T(unittest.TestCase):\n"
               "    @classmethod\n"
               "    def setUpClass(cls):\n"
               "        raise AssertionError('防假绿保险')\n"
               "    @classmethod\n"
               "    def setUpClass(cls):\n"
               "        cls.x = 1\n")
        hits = duplicate_class_methods(src)
        self.assertTrue(hits, "两个同名 setUpClass 都没抓到，这判据是摆设")
        self.assertIn("setUpClass", hits[0])

    def test_the_guard_allows_legit_shapes(self):
        """正例：不同类里同名、以及模块顶层 try/except 退路，都不许误报。"""
        ok = ("class A(unittest.TestCase):\n"
              "    def setUp(self):\n"
              "        pass\n"
              "class B(unittest.TestCase):\n"
              "    def setUp(self):\n"
              "        pass\n"
              "try:\n"
              "    import gi\n"
              "except Exception:\n"
              "    gi = None\n")
        self.assertEqual(duplicate_class_methods(ok), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
