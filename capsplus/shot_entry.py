#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""capsplus-shot 的 **python 入口**。

`bin/capsplus-shot` 现在是 **sh 启动器**：keyd 的 `command(...)` 以 root 执行，直接跑 python
会在"要显示服务授权"那一关静默失败（实测见那个文件的头注），所以启动器先降权 + 注入会话环境，
`exec` 的就是本文件。为什么入口单独一个文件：`bin/` 下那个位置只能放一个文件，
而它必须是 sh（要能跑 root 分支）—— 两者不能合一。

参数与用法看 `--help`（实现全在 `capsplus/shot_capture.py`）。
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from capsplus.shot_capture import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))