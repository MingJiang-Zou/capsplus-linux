#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""由 README.template.md + 映射数据生成 README.md（键表不手写，改映射即改文档）。"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus.render import render_key_table  # noqa: E402


def main() -> int:
    template = (REPO / "README.template.md").read_text(encoding="utf-8")
    out = (template
           .replace("@@CAPL_TABLE@@", render_key_table("capl"))
           .replace("@@ALT_TABLE@@", render_key_table("capl+alt")))
    target = REPO / "README.md"
    target.write_text(out, encoding="utf-8")
    print(f"已生成 {target}（{len(out.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
