# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""截图资产的判据：图在、不是空图、README 里指得到，而且**指不到人的家目录**。

为什么值得锁：README 顶部那三张图是别人对项目的第一印象，链接坏了、图是灰的、或者把
`/home/<人名>` 画进像素，都是"公开即翻车"——而 PNG 里的文字**不会**被 grep 个人路径的判据抓到。
"""

import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SHOTS = REPO / "assets" / "screenshots"
NAMES = ("links.png", "general.png", "keys.png")
MIN_BYTES = 20_000          # 实测最小 57KB；空图/半张图远小于这个数


class TestScreenshotFiles(unittest.TestCase):
    def test_all_three_exist_and_are_not_trivial(self):
        for name in NAMES:
            path = SHOTS / name
            self.assertTrue(path.exists(), f"缺截图：{path}")
            size = path.stat().st_size
            self.assertGreater(size, MIN_BYTES, f"{name} 只有 {size} 字节，八成是空图")
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n", f"{name} 不是 PNG")

    def test_demo_config_is_the_only_kind_of_data_shown(self):
        """演示配置必须存在、且只指向 example —— 别把真人的搜索前缀/域名画进公开图。"""
        demo = (SHOTS / "demo-qbar.toml").read_text(encoding="utf-8")
        self.assertIn("example.com", demo)
        for leak in ("/home/", "192.168.", ".qq.com"):
            self.assertNotIn(leak, demo, f"演示配置里出现了 {leak!r}")


class TestReadmeLinks(unittest.TestCase):
    def test_readme_references_every_screenshot(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for name in NAMES:
            self.assertIn(f"assets/screenshots/{name}", readme, f"README 没引用 {name}")

    def test_no_markdown_image_points_at_a_missing_file(self):
        """README/模板/docs 里所有 `![...](相对路径)` 必须指到真实文件（坏图=公开即尴尬）。"""
        broken = []
        for doc in sorted(list(REPO.glob("*.md")) + list((REPO / "docs").glob("*.md"))):
            text = doc.read_text(encoding="utf-8")
            for target in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text):
                if target.startswith(("http://", "https://", "data:")):
                    continue
                if not (doc.parent / target).exists():
                    broken.append(f"{doc.name} -> {target}")
        self.assertEqual(broken, [], "坏掉的图片链接：\n  " + "\n  ".join(broken))


class TestShotToolRefusesRealDesktop(unittest.TestCase):
    """截图工具必须先问后端：真实 Wayland 上拒跑（否则又把窗口弹到用户屏幕上）。

    判据只测那个纯函数 —— 不需要显示服务器，CI 上也能跑。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "capsplus_make_screenshots", REPO / "tools" / "make-screenshots.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def test_refuses_wayland_without_the_flag(self):
        reason = self.tool.refuse_real_desktop("GdkWaylandDisplay", allow_wayland=False)
        self.assertIn("真实桌面", reason)
        self.assertIn("xvfb-run", reason, "拒跑理由得给出正确跑法，不然只会让人再试一次")

    def test_allows_wayland_when_explicitly_flagged(self):
        self.assertEqual(self.tool.refuse_real_desktop("GdkWaylandDisplay", allow_wayland=True), "")

    def test_allows_x11_always(self):
        self.assertEqual(self.tool.refuse_real_desktop("GdkX11Display", allow_wayland=False), "")

    def test_neutral_bindir_is_the_default(self):
        """渲染出来的 `q` 行动作不许带真人家目录（PNG 里的字逃过 grep 判据）。"""
        self.assertEqual(self.tool.NEUTRAL_BINDIR, "/opt/demo-bin")


