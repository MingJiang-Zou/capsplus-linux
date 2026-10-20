#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
# raise-browser 扩展的**部署前预检**：坏文件挡在登录之外。
#
# 为什么单独一个文件而不是塞进 install-extension.sh（2026-10-20 的选择，理由摆在这儿）：
#   ① 预检必须能在**任意目录**上跑（反例举证时给它一个故意弄坏的副本），而 install-extension.sh
#      是"写侧"，它的路径是钉死的部署目录 —— 两件事混在一个脚本里，反例就得先污染部署目录；
#   ② "一个字节都不写"这句承诺，靠**这个脚本根本不碰目标目录**来成立，比靠人读代码相信更硬；
#   ③ 三个人/三条路都要用它：`--sync` 写入前（拦坏文件）、人手排查、CI。
#   `install-extension.sh --sync` 把它当**强制闸门**调用，红了就一个字节不写。
#
# 判词不在这里写第二遍：全在 `python3 -m capsplus.extpreflight`（那边能单测，不用起 node）。
#
# 退出码（与 tools/ext-bench.sh 同一套语义）：
#   0 全绿可部署 ／ 1 有红（**不许部署**）／ 2 环境不齐（没有 node / 目录不存在 —— 同样不许部署）
#
# 用法：
#   sh tools/preflight-extension.sh                       # 预检仓内那份扩展
#   sh tools/preflight-extension.sh --ext-dir DIR         # 预检任意一份（反例举证用这个）
#   sh tools/preflight-extension.sh --quiet               # 只打结论行（给脚本/CI 用）
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

exec env PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m capsplus.extpreflight "$@"