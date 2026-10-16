# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""跨发行版的落点：keyd 二进制名 / systemd 单元名 / 用户 bin 目录 / fd 命令名。

为什么要有这个模块（2026-10-16）：原先各处写死 `keyd.rvaiya`、`/home/<某个人>/.local/bin`，
只有打包者那台机器能跑。两件事实测过：

  1. **Debian/Ubuntu** 因为 `keyd` 这个名字被旧包 onak 占了，把 keyd 装成 `keyd.rvaiya`
     （`/usr/bin/keyd.rvaiya`）；**Arch / Fedora / openSUSE** 装的就是 `keyd`。
     所以只能"两个名字都试"，不能只认一个。
  2. systemd 单元名反倒一致：Ubuntu 的 keyd.rvaiya 包装的也是 `keyd.service`
     （本机 `systemctl list-unit-files | grep keyd` → `keyd.service`）。
     所以 `systemctl` / `journalctl -u` 一律用 `keyd`，只有**可执行文件名**要猜。

覆盖口子：`CAPSPLUS_KEYD_BIN`（指到任意路径）、`CAPSPLUS_BINDIR`（用户 bin 目录）。
测试靠这两个变量注入，不需要真的装 keyd。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# 依次尝试的二进制名（Debian/Ubuntu 的改名版排在前面，因为本机与常见 Ubuntu 都是它）
KEYD_BIN_NAMES = ("keyd.rvaiya", "keyd")
# PATH 里一个都没找到时的兜底：非 Debian 系的标准位置（**不代表它一定存在**）
KEYD_BIN_FALLBACK = "/usr/bin/keyd"
# systemd 单元名（各发行版一致，见模块 docstring 第 2 条）
KEYD_UNIT = "keyd"
# fd 的 Debian/Ubuntu 名与其余发行版的通用名
FD_BIN_NAMES = ("fdfind", "fd")

# mapping_data 里代表"用户 bin 目录"的记号，渲染时才换成真实路径（见 resolve_bindir_token）
BINDIR_TOKEN = "@CAPSPLUS_BINDIR@"


def resolve_keyd_bin() -> str:
    """本机 keyd 可执行文件路径。

    顺序：`CAPSPLUS_KEYD_BIN` → PATH 里的 `keyd.rvaiya` → PATH 里的 `keyd` → `KEYD_BIN_FALLBACK`。
    取不到也返回一个字符串（兜底值），调用方**别**拿它的真假当"装没装 keyd"的判据，
    那个判断要用 `find_keyd_bin()`。
    """
    override = os.environ.get("CAPSPLUS_KEYD_BIN")
    if override:
        return override
    for name in KEYD_BIN_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return KEYD_BIN_FALLBACK


def find_keyd_bin() -> str | None:
    """装了才返回路径，没装返回 None —— "这台机器有没有 keyd"只能问这个。"""
    override = os.environ.get("CAPSPLUS_KEYD_BIN")
    if override:
        return override if Path(override).exists() else None
    for name in KEYD_BIN_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def keyd_bin_origin() -> str:
    """给人看的一句话：这个二进制是怎么定下来的（`capsplus status` 里那一行）。"""
    if os.environ.get("CAPSPLUS_KEYD_BIN"):
        return f"{resolve_keyd_bin()}（环境变量 CAPSPLUS_KEYD_BIN）"
    for name in KEYD_BIN_NAMES:
        if shutil.which(name):
            return f"{shutil.which(name)}（PATH 里的 {name}）"
    return f"{KEYD_BIN_FALLBACK}（PATH 里没有 {' / '.join(KEYD_BIN_NAMES)}，用兜底路径）"


def bindir() -> Path:
    """用户级的可执行文件目录：`CAPSPLUS_BINDIR` 覆盖，否则 `~/.local/bin`。

    家目录一律 `Path.home()` 现算 —— 仓里**不许**出现任何具体用户的绝对路径。
    """
    override = os.environ.get("CAPSPLUS_BINDIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "bin"


def resolve_bindir_token(text: str) -> str:
    """把动作字符串里的 `@CAPSPLUS_BINDIR@` 换成真实目录，并守住"不许含空白"这条 keyd 边界。

    keyd 的 `command(...)` 把行尾剩余内容整段当"命令 + 参数"，实测加引号也会被原样发送，
    所以路径里带空格就是坏的（2026-10-15 实测，见 docs/KNOWN-LIMITS.md）。
    """
    if BINDIR_TOKEN not in text:
        return text
    path = str(bindir())
    if any(ch.isspace() for ch in path):
        raise ValueError(
            f"用户 bin 目录带空格，keyd 的 command() 装不下：{path!r}\n"
            "        改法：给 CAPSPLUS_BINDIR 指一个不含空格的目录（软链过去也行），"
            "例如 CAPSPLUS_BINDIR=/usr/local/bin，再重跑 capsplus apply。")
    return text.replace(BINDIR_TOKEN, path)


def keyd_cli_hint() -> str:
    """报错文案里该说哪个命令名：本机装了 keyd.rvaiya 就说 keyd.rvaiya，装了 keyd 就说 keyd。"""
    found = find_keyd_bin()
    return Path(found).name if found else KEYD_BIN_NAMES[-1]


def find_fd_binary() -> str | None:
    """fd 的可执行名：Debian/Ubuntu 叫 `fdfind`，别处多半叫 `fd`。没装返回 None。"""
    for name in FD_BIN_NAMES:
        if shutil.which(name):
            return name
    return None
