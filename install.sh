#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
# capsplus 的装机入口。**真正实现在 `capsplus install`**（capsplus/install.py），这里只做转发。
#
# 为什么还留这个壳（2026-10-20）：
#   ① README、历史笔记、别人手边的肌肉记忆都是 `./install.sh` —— 换个名字只会让人找不到；
#   ② 全新机器上它还跑得动：`bin/capsplus` 只需要 python3，不需要任何已装的软链；
#   ③ 参数原样透传，所以 `./install.sh --dry-run` / `--no-keyd` / `--no-extension` / `--no-units`
#      与 `capsplus install --dry-run` 完全等价 —— **一份实现，两个叫法**，不再有第二份真相。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
  -h|--help)
    exec python3 "$REPO/bin/capsplus" help
    ;;
esac

exec python3 "$REPO/bin/capsplus" install "$@"
