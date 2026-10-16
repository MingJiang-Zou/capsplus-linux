# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""GPL-2.0 合规护栏：LICENSE 全文 + 每个源文件顶部的声明块。

为什么值得单测锁住：本仓的按键映射表译自上游 `wo52616111/capslock-plus`（GPL-2.0），
属**衍生作品**。公开分发时少一个声明块、少一份 LICENSE 全文，就是违反 GPL-2.0 §2 的
"保留署名 + 声明改动"要求 —— 这类问题人眼扫一遍很难发现，机器一条就够。

反例判据在 `TestGuardHasTeeth` 里：故意造一份"缺声明"的源码，`offenders()` 必须抓到
（判据抓不住东西 = 没有判据）。
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SPDX_LINE = "# SPDX-License-Identifier: GPL-2.0-only"
COPYRIGHT_LINE = "# Copyright (c) 2026 capsplus-linux contributors"
UPSTREAM_URL = "https://github.com/wo52616111/capslock-plus"


def source_files(repo: Path = REPO) -> list[Path]:
    """要带声明块的文件：所有 .py + bin/ 下的脚本 + install.sh（不含 LICENSE / md / 资源）。"""
    files = [p for p in sorted(repo.rglob("*.py")) if "__pycache__" not in p.parts]
    bin_dir = repo / "bin"
    if bin_dir.is_dir():
        files += [p for p in sorted(bin_dir.iterdir()) if p.is_file()]
    install = repo / "install.sh"
    if install.exists():
        files.append(install)
    return files


def offenders(text: str) -> list[str]:
    """一段源码缺哪几行声明（空 = 合规）。只看**前 8 行**，声明块必须在顶部。"""
    head = text.splitlines()[:8]
    joined = "\n".join(head)
    bad: list[str] = []
    if SPDX_LINE not in joined:
        bad.append(f"缺 {SPDX_LINE}")
    if COPYRIGHT_LINE not in joined:
        bad.append(f"缺 {COPYRIGHT_LINE}")
    if UPSTREAM_URL not in joined:
        bad.append(f"缺上游出处链接 {UPSTREAM_URL}")
    return bad


class TestLicenseFile(unittest.TestCase):
    def test_license_is_the_gpl_2_full_text(self):
        """LICENSE 必须是 GPL **v2** 全文（不是 v3，也不是自己写的摘要）。"""
        path = REPO / "LICENSE"
        self.assertTrue(path.exists(), "仓库根缺少 LICENSE")
        text = path.read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 2, June 1991", text)
        self.assertIn("How to Apply These Terms to Your New Programs", text,
                      "不像全文：缺结尾的「如何把本许可用到你的新程序」一节")
        self.assertNotIn("GNU GENERAL PUBLIC LICENSE Version 3", text)
        self.assertGreater(len(text), 15000, "GPL-2 全文约 18 KB，太短说明被动过")

    def test_no_duplicate_license_file(self):
        """GPL-2.0 用 LICENSE 一份就够，别再造 COPYING（两份真相迟早漂）。"""
        self.assertFalse((REPO / "COPYING").exists())


class TestSourceHeaders(unittest.TestCase):
    def test_every_source_file_carries_the_notice(self):
        files = source_files()
        self.assertGreater(len(files), 20, "扫到的源文件太少，扫描逻辑可能坏了")
        missing = []
        for path in files:
            problems = offenders(path.read_text(encoding="utf-8"))
            if problems:
                rel = path.relative_to(REPO)
                missing.append(f"{rel}: " + "; ".join(problems))
        self.assertEqual(missing, [], "这些文件顶部没有完整的 GPL 声明块：\n  " + "\n  ".join(missing))

    def test_generated_keyd_config_header_declares_upstream(self):
        """`capsplus render` 生成的 keyd 配置头也要带许可与上游（生成的文件是分发物的一部分）。"""
        from capsplus.render import render
        head = "\n".join(render().splitlines()[:12])
        for needle in ("GPL-2.0-only", UPSTREAM_URL, "wo52616111", "lib_keysSet.ahk"):
            self.assertIn(needle, head, f"生成的配置头注释里少了 {needle}")


class TestGuardHasTeeth(unittest.TestCase):
    """判据得能红：自己造一份"缺声明"的源码，护栏必须抓到。"""

    def test_detects_a_file_without_any_notice(self):
        self.assertEqual(offenders('"""新写的模块。"""\nimport os\n'),
                         [f"缺 {SPDX_LINE}", f"缺 {COPYRIGHT_LINE}", f"缺上游出处链接 {UPSTREAM_URL}"])

    def test_detects_a_notice_buried_past_the_head(self):
        buried = "\n".join([f"#  filler {i}" for i in range(10)] + [SPDX_LINE, COPYRIGHT_LINE, UPSTREAM_URL])
        self.assertTrue(offenders(buried), "声明块跑到文件中段还算合规 = 判据失效")

    def test_detects_missing_copyright_line_only(self):
        partial = "\n".join([SPDX_LINE, f"#   上游 {UPSTREAM_URL}"])
        self.assertEqual(offenders(partial), [f"缺 {COPYRIGHT_LINE}"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
