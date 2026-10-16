# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""CI 判据：没有 python3-gi 的机器上，测试收集阶段**不许**崩。

为什么单独一条判据（2026-10-16）：GitHub 的 `ubuntu-latest` runner 默认不装 `python3-gi`。
只要有任何一个测试模块在**顶层**裸写 `import gi`，`unittest discover` 就会在收集阶段报
collection error —— 表现是"整套红、一条用例都没跑"，而本地（装了 gi 的开发机）永远复现不了。
这类问题必须由机器守着，不能靠人记得加 try。

跑法（本机装了 gi 也能复现 CI 环境）：

    PYTHONPATH=tools/ci/nogi python3 -m unittest discover -s tests

三条用例：
  ① 逐个测试模块在"没有 gi"的子进程里必须能被导入；
  ② 整套 `discover` 在"没有 gi"的子进程里退出码必须是 0；
  ③ 反例具型：造一个顶层 `import gi` 的模块，①②的手法必须把它判红（证明判据不是摆设）。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NOGI_DIR = REPO / "tools" / "ci" / "nogi"

# 子进程里再跑 discover 时用它挡递归（见 test_discover_returns_zero_without_gi）
SUB_FLAG = "CAPSPLUS_NOGI_SUB"


def nogi_env(**extra) -> dict:
    """一份"假装没装 PyGObject"的环境：PYTHONPATH 前置屏蔽器 + 挡递归 + 不落真用户目录。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(NOGI_DIR), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env[SUB_FLAG] = "1"
    env.update(extra)
    return env


class TestModulesImportWithoutGi(unittest.TestCase):
    """① 每个测试模块都得能在没有 gi 的机器上被 import（= discover 的收集阶段）。"""

    def test_each_test_module_is_importable(self):
        self.assertTrue(NOGI_DIR.is_dir(), f"缺少屏蔽器目录 {NOGI_DIR}")
        code = ("import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "try:\n"
                "    import gi\n"
                "except Exception:\n"
                "    pass\n"
                "else:\n"
                "    sys.exit('屏蔽器没生效：还能 import gi')\n")
        bad = []
        for path in sorted((REPO / "tests").glob("test_*.py")):
            proc = subprocess.run([sys.executable, "-c", code, str(path)],
                                  cwd=str(REPO), env=nogi_env(),
                                  capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
                bad.append(f"{path.name}: rc={proc.returncode} " + " | ".join(tail))
        self.assertEqual(bad, [],
                         "这些测试模块在没有 python3-gi 的机器上过不了收集阶段：\n  "
                         + "\n  ".join(bad))


class TestFullDiscoveryWithoutGi(unittest.TestCase):
    """② `discover` 整体退出码为 0（CI 就只跑这一条命令）。"""

    def test_discover_returns_zero_without_gi(self):
        if os.environ.get(SUB_FLAG) == "1":
            self.skipTest("这条就是被嵌套跑的那个内层，跳过以免无限递归")
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                              cwd=str(REPO), env=nogi_env(),
                              capture_output=True, text=True, timeout=600)
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-4:]
        self.assertEqual(proc.returncode, 0,
                         f"无 gi 的 discover 退出码 {proc.returncode}\n  " + "\n  ".join(tail))
        self.assertTrue(any(line.startswith("OK") for line in tail),
                        f"没看到 OK：\n  " + "\n  ".join(tail))


class TestGuardHasTeeth(unittest.TestCase):
    """③ 反例具型：判据必须能红。"""

    def test_bare_top_level_gi_import_is_caught(self):
        """一个顶层裸写 `import gi` 的模块，必须被同一套手法判失败。"""
        code = ("import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n")
        with tempfile.TemporaryDirectory() as tmp:
            bad_file = Path(tmp) / "test_bare_import_gi.py"
            bad_file.write_text("import gi\ngi.require_version('Gtk', '3.0')\n", encoding="utf-8")
            proc = subprocess.run([sys.executable, "-c", code, str(bad_file)],
                                  cwd=str(REPO), env=nogi_env(),
                                  capture_output=True, text=True, timeout=120)
            self.assertNotEqual(proc.returncode, 0, "裸 import gi 居然过了 = 判据是摆设")
            self.assertIn("No module named", proc.stderr)

    def test_unhandled_valueerror_is_also_caught(self):
        """gi 在但 typelib 不在 → require_version 抛 ValueError，同样得算失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "sitecustomize.py"
            site.write_text("import sys\n"
                            "class B:\n"
                            "    def find_spec(self, name, path=None, target=None):\n"
                            "        if name == 'gi':\n"
                            "            raise ValueError('typelib 缺失')\n"
                            "        return None\n"
                            "sys.meta_path.insert(0, B())\n", encoding="utf-8")
            code = ("import importlib.util, sys\n"
                    "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
                    "mod = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(mod)\n")
            bad_file = Path(tmp) / "test_valueerror.py"
            bad_file.write_text("import gi\n", encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=tmp, **{SUB_FLAG: "1"})
            proc = subprocess.run([sys.executable, "-c", code, str(bad_file)],
                                  cwd=str(REPO), env=env,
                                  capture_output=True, text=True, timeout=120)
            self.assertNotEqual(proc.returncode, 0, "ValueError 漏网 = 判据只挡了 ImportError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
