#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
# raise-browser 扩展的**登录后一条命令自证**：跑完给一张 PASS/FAIL 表。
#
# 为什么要有它（2026-10-20）：用户那天因为改 extension.js 重登了 3 次，其中一次登录后扩展直接
# 进了 ERROR 态 —— 而"重登完到底行不行"当时只能靠**按一下 Caps+F1 试试**。这条命令把"行不行"
# 变成七项可复核的读数，**不用鼠标、不用人眼看**（真机注入是有代价的，能免则免）。
#
# 什么时候跑：注销重登/重启**之后**的一条命令，作为"这次重登值得"的凭据；
#   排查时也可以直接跑（不用重登）—— 它会告诉你差在哪一项。
#
# 七项与各自的失败形态（细节与实测证据见 docs/SHOT-TOOL.md 第三节）：
#   ① shell 单元在跑 ② 本次开机有 ready 行（只认 gnome-shell 进程写的）
#   ③ 总线名有主（ERROR 态时是 false） ④ Probe 往返（发信号 + journal 增量）
#   ⑤ 真抓一帧 200×150 且**非纯色** ⑥ 贴图 + 清贴图（ack 要回来）
#   ⑦ 遮罩起停 —— 在 **xvfb** 里跑，绝不往用户的屏上盖东西
#
# 屏幕上的动作只有一件：⑥ 会把一张 200×150 的小图钉在左上角、随即清掉（约 1 秒）。
# 不想让它出现就加 `--no-screen`（那一项会记成 SKIP）。
#
# 退出码：0 = 七项没有 FAIL（证明这条链在这个会话里是活的）；1 = 有 FAIL（没证明）。
# 判词不在这里写第二遍：全在 `python3 -m capsplus.extverify`（那边能单测，不碰真屏）。
#
# 用法：
#   sh tools/verify-after-login.sh              # 全跑（贴图那一步会在屏幕上闪一下）
#   sh tools/verify-after-login.sh --no-screen   # 不碰屏幕
#   sh tools/verify-after-login.sh --keep DIR    # 把抓到的帧留下来看
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# `CAPSPLUS_NOTIFY=0`：自证失败时**别**再弹一条桌面通知 —— 表里已经写清楚了，
# 而且这条路常常是在终端里跑的（通知只会多打断一次）。
# `NO_AT_BRIDGE=1`：只为压掉 GTK 那句 `Couldn't connect to accessibility bus`（真机上 a11y 总线
# 没起来时它必出现）；那不是掩盖问题 —— 那句话与本脚本任何一项判据都无关，出现只会干扰读表。
exec env CAPSPLUS_NOTIFY=0 NO_AT_BRIDGE=1 PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m capsplus.extverify "$@"