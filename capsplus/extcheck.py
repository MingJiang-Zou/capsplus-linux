# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""`capsplus status` 里的 raise-browser 扩展体检。

为什么要有这个函数（2026-10-16）：扩展**加载失败是静默的** —— shell 只在 journal 里留一行，
Qbar 侧因为走信号（没有回复通道）完全察觉不到，用户看到的只是"还是新建窗口"。
我今天就因此白折腾了一次注销重登。所以状态必须能在命令行一眼看到，并且**给出下一步动作**。

这里刻意不读中文标签（`gnome-extensions info` 的输出会跟着系统语言变），
只认 `ACTIVE` / `ERROR` 这类不会翻译的状态字，以及 `list --enabled` 的 uuid 行。
"""

import shutil
import subprocess

EXT_UUID = "raise-browser@capsplus.example"
EXT_DIR = "~/.local/share/gnome-shell/extensions/" + EXT_UUID

# 状态 → 下一步（写成人话，别让人再去翻文档）
HINTS = {
    "missing": "没装：跑 `sh tools/install-extension.sh`，然后**注销重登**一次",
    "disabled": "装了但没启用：`gnome-extensions enable " + EXT_UUID + "`（改过代码仍要重登）",
    "ACTIVE": "正常：Qbar 打开网址前会先激活浏览器现有窗口",
    "ERROR": "加载失败：改过 extension.js 后 disable/enable **不会重新 import**，"
             "必须注销重登；具体原因看 `journalctl --user --since '-10 min' | grep raise-browser`",
    "no-gnome": "这台机没有 gnome-extensions（非 GNOME？）—— 该功能不适用，Qbar 会照常打开网址",
    "unknown": "读不到状态：手动查 `gnome-extensions info " + EXT_UUID + "`",
}


def _run(argv, runner):
    """跑一条外部命令；没有该命令返回 None，其他失败返回空串。"""
    if runner is not None:
        return runner(argv)
    exe = argv[0]
    if shutil.which(exe) is None:
        return None
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def extension_status(runner=None, which=None):
    """返回 (state, 一行说明)。任何异常都归成状态，绝不抛 —— status 不能因为体检而崩。"""
    which = which or shutil.which
    if which("gnome-extensions") is None:
        return "no-gnome", HINTS["no-gnome"]
    enabled = _run(["gnome-extensions", "list", "--enabled"], runner) or ""
    listed = EXT_UUID in enabled
    info = _run(["gnome-extensions", "info", EXT_UUID], runner) or ""
    if "ACTIVE" in info:
        state = "ACTIVE"
    elif "ERROR" in info:
        state = "ERROR"
    elif listed:
        state = "unknown"
    else:
        state = "missing" if "不存在" in info or "does not exist" in info or not info.strip() else "disabled"
    return state, HINTS.get(state, HINTS["unknown"])


QBAR_STATE = "~/.local/state/capsplus/raise-browser.json"


def qbar_observed(path=None):
    """Qbar 跑起来会问一次 Probe，扩展应答与否写在 raise-browser.json 里。

    这是**现场实测**，和 `gnome-extensions info` 的"shell 说它 ACTIVE"是两回事：
    shell 说 ACTIVE 但 Qbar 收不到应答，才说明链路真断了（反过来也一样）。两个都报出来最好查。
    """
    import json
    import os
    target = os.path.expanduser(path or QBAR_STATE)
    if not os.path.exists(target):
        return None, "还没问过（Qbar 没起来过？）"
    try:
        data = json.loads(open(target, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        return None, f"读不了：{type(exc).__name__}"
    alive = data.get("alive")
    if alive is True:
        return True, ("应答过：windows=%s on-current=%s default=%s（%s 秒前）"
                      % (data.get("windows"), data.get("on_current"),
                         data.get("default"), _age(data.get("at"))))
    if alive is False:
        return False, "Qbar 确认扩展没应答（发信号没报错，但没人回）"
    return None, "问过但还没等到应答（扩展大概没加载）"


def _age(ts):
    import time
    try:
        return int(time.time() - float(ts))
    except (TypeError, ValueError):
        return -1
