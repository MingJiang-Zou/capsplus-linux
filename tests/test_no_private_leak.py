# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：仓里不许出现**不该进公开仓**的私人信息。

为什么值得单开这一条（2026-10-21 首次做发布前审计时的事实）：
    本仓要公开发布到 GitHub。审计扫出两类东西：
      ① 作者的中文真名 —— README / CHANGELOG / tests 里 200 多处，全是"某人原话：…"这种**私人对话**引用；
      ② 一处真家目录（当错误日志样本用的路径字符串）。
    已发布的 v0.1.0 / v0.2.0 用的是中性词，所以口径统一成"公开版里对读者只说事，不说人"。
    推上 GitHub 就是**公开**，发出去收不回来 —— 这类东西只能靠机器每次提交都拦一次。

与面板巡检那条的关系（两道闸，不是替代）：
    本条在仓里（开发期就能跑，`python3 -m unittest discover -s tests` 顺带跑）；
    面板巡检那条（`~/.dsh/checks/capsplus/cp-caps-no-private-leak.sh`）住在 ~/.dsh 侧 ——
    判据本体不能放在被测仓里，否则被测代码能改自己的判据（check-curator 契约 §9）。
    两条规则**同口径**，改一处要改另一处。

三条设计上的讲究：
  ① **真名用码点拼出来**（`chr(0x660E) + chr(0x6C5F)`）—— 否则这条判据自己就命中自己；
  ② 邮箱有白名单（`@capsplus.example` / `@ubuntu.com` / `users.noreply.github.com` / example 域）；
  ③ **二进制不扫** —— PNG 里的字 grep 不到，那是 `tests/test_screenshots.py` 的活（它查演示配置与图片资产）。
"""

import pathlib
import re
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 作者的中文真名：用码点拼 —— 这条判据自己不许出现明文（否则它永远命中自己）。
AUTHOR_CN = chr(0x660E) + chr(0x6C5F)
# 本机登录名同样用拼接构造（明文出现一次就是一次泄漏）
LOGIN_NAME = "z" + "mj"

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
TEXT_EXT = {".py", ".sh", ".md", ".txt", ".toml", ".tsv", ".csv", ".json", ".yaml", ".yml",
            ".desktop", ".service", ".target", ".js", ".mjs", ".ts", ".css", ".svg", ""}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".ico", ".woff", ".woff2"}

# 规则表：一条一行，名字给人看、正则给人抓。
RULES = (
    ("作者中文真名", re.compile(re.escape(AUTHOR_CN))),
    ("本机登录名", re.compile(r"\b" + LOGIN_NAME + r"\b")),
    ("内网地址", re.compile(r"\b(?:192\.168|10|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2}\b")),
    ("NAS 路径", re.compile(r"/srv/(?:wd500|seagate|usb500)\b|/home/nas\b")),
    ("家目录", re.compile(r"/home/(?!runner\b)[A-Za-z0-9._-]{2,}")),
    ("真邮箱", re.compile(r"\b[A-Za-z0-9._%+-]+@(?!capsplus\.example\b|ubuntu\.com\b|ubuntu\.service\b"
                          r"|users\.noreply\.github\.com\b|example\.com\b|github\.com\b"
                          r"|localhost\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("凭据形态", re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")),
    # 容忍 `headers["X-Echo-Token"] = "…"` 这种中间夹着 `"]` 的写法（真代码就长这样）
    ("会话令牌赋值", re.compile(r"x-echo-token[\"']?\]?\s*[:=]\s*[\"'][^\"']{8,}", re.I)),
)


def scan_text(text: str) -> list:
    """一段文本里命中的规则（纯函数：判据与反例具型共用同一份口径）。"""
    hits = []
    for label, pattern in RULES:
        for match in pattern.finditer(text):
            hits.append((label, match.group(0)))
    return hits


def scan_repo(root: pathlib.Path) -> list:
    """扫整个仓，返回 [(相对路径, 行号, 规则名, 命中内容)]。"""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name == "test_no_private_leak.py":
            # **豁免自身**：这份文件里放着每条规则的反例样本（它们长得就像真泄漏）。
            # 豁免的是"这一个文件"，不是"tests/ 整目录" —— 别人往 tests/ 里写真名照样被抓。
            continue
        ext = path.suffix.lower()
        if ext in BINARY_EXT:
            continue
        if ext not in TEXT_EXT and not path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, value in scan_text(line):
                found.append((str(path.relative_to(root)), lineno, label, value))
    return found


class TestRepoHasNoPrivateInformation(unittest.TestCase):
    """判据本体：仓里 0 处私人信息。红了就是"要发出去之前先清一遍"。"""

    def test_repo_has_no_private_information(self):
        found = scan_repo(REPO)
        self.assertEqual(
            found, [],
            "仓里出现了不该进公开仓的私人信息（推上 GitHub 就是公开，收不回来）：\n  "
            + "\n  ".join(f"{rel}:{line} [{label}] {value!r}" for rel, line, label, value in found[:40]))

    def test_the_scan_actually_reads_files(self):
        """零结果不许判绿（契约 §1.3 第 5 条的同一条纪律）：扫不到文件 = 环境不对，不是干净。"""
        scanned = 0
        for path in REPO.rglob("*.py"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            scanned += 1
        self.assertGreater(scanned, 20, f"只扫到 {scanned} 个 .py —— 扫描面本身有问题")


class TestGuardHasTeeth(unittest.TestCase):
    """反例具型：判据必须真抓得住（抓不住 = 没判据）。"""

    def test_catches_every_rule(self):
        cases = (
            ("作者中文真名", "由 " + AUTHOR_CN + " 原话"),
            ("本机登录名", "cd /home/" + LOGIN_NAME + "/project"),
            ("内网地址", "服务在 http://192.168.0.110:9204"),
            ("NAS 路径", "备份在 /srv/wd500/用户私人目录"),
            ("家目录", "配置在 /home/somebody/.config/x"),
            ("真邮箱", "联系 somebody@example.org"),
            ("凭据形态", "key = sk-abcdefghijklmnopqrstuvwx"),
            ("会话令牌赋值", 'headers["X-Echo-Token"] = "0123456789abcdef"'),
        )
        for label, sample in cases:
            hits = scan_text(sample)
            self.assertTrue(hits, f"规则「{label}」没抓到样本：{sample!r}")
            self.assertIn(label, [name for name, _ in hits])

    def test_known_safe_forms_are_not_flagged(self):
        """正例：白名单里的邮箱、`@example` 域、示例主机名都不许误报。"""
        for sample in ("raise-browser@capsplus.example",
                       "ubuntu-appindicators@ubuntu.com",
                       "63432285+517477981@users.noreply.github.com",
                       "someone@example.com",
                       "http://127.0.0.1:18090/health",
                       "http://192.0.2.10/v1"):        # RFC 5737 文档用地址
            self.assertEqual(scan_text(sample), [], f"误报了：{sample!r}")

    def test_scan_repo_finds_a_planted_file(self):
        """反例具型：往临时仓里放一个带真名的文件，扫描必须抓到它。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
            (root / "leak.md").write_text("由 " + AUTHOR_CN + " 原话\n", encoding="utf-8")
            found = scan_repo(root)
            self.assertEqual(len(found), 1, f"应当只抓到那 1 处：{found}")
            self.assertEqual(found[0][0], "leak.md")
            self.assertEqual(found[0][2], "作者中文真名")


if __name__ == "__main__":
    unittest.main()
