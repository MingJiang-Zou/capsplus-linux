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

import re
import shutil
import subprocess
import sys

EXT_UUID = "raise-browser@capsplus.example"
EXT_DIR = "~/.local/share/gnome-shell/extensions/" + EXT_UUID

# 状态 → 下一步（写成人话，别让人再去翻文档）
HINTS = {
    "missing": "没装：跑 `sh tools/install-extension.sh --sync`，然后**注销重登**一次",
    "disabled": "装了但没启用：`gnome-extensions enable " + EXT_UUID + "`（改过代码仍要重登）",
    "ACTIVE": "正常：Qbar 打开网址前会先激活浏览器现有窗口",
    "ERROR": "加载失败：改过 extension.js 后 disable/enable **不会重新 import**，"
             "必须注销重登；具体原因看 `journalctl --user --since '-10 min' | grep raise-browser`。"
             "**先跑 `sh tools/ext-bench.sh` 自检**（不用重登，1~3 秒）：它绿了说明是 shell 里"
             "还跑着旧代码（那只差一次重登）；它红了才是代码本身坏了",
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


# ------------------------------------------------ headless 自检（tools/ext-bench.sh 的嘴）

# 判"扩加载起来了"只认这两样，都是**实测**出来的（2026-10-20）：
#   ① 扩展自己在 enable() 末尾写的那行（`console.log` → shell 的 journal/标准错误）；
#   ② 没有任何一条 "Extension <uuid>: …" —— 那是 shell 加载期报错的口径
#      （实测原样：`Extension raise-browser@capsplus.example: SyntaxError: unexpected token …`）。
# 为什么不用 D-Bus 判活：本机 gjs 的 register_object 是原始 C 绑定，扩展导出不了对象，
# 隔离会话里 `gdbus … Probe` 报 UnknownMethod「对象在路径处不存在」（2026-10-20 实测）。
BENCH_READY = "[raise-browser] ready"
BENCH_BAD_MARKS = ("启用失败", "malformed", "not a constructor")
BENCH_SHELL_STARTED = "Running GNOME Shell"
_BENCH_LOAD_ERROR = re.compile(r"Extension " + re.escape(EXT_UUID) + r":")

# 判据：ready 行必须报到"抓帧/贴图通道都挂上了"。这不是多余的严格 ——
# 扩展**加载成功但订阅没挂上**时，现象与"没装"一模一样（Caps+F1 没反应、也没有报错），
# 那正是用户反复重登都修不好的那种状态。
BENCH_READY_NEEDS = ("capture=on", "pin=on")


def bench_verdict(log_text) -> tuple:
    """把 headless gnome-shell 的日志判成 `(ok, 可操作原因)`。纯函数，不碰文件也不起进程。

    单测直接把日志喂进来（`tests/test_raise_browser.py`），真跑一次 shell 太慢而且依赖图形栈。
    """
    text = log_text or ""
    lines = text.splitlines()

    bad = [ln.strip() for ln in lines
           if any(mark in ln for mark in BENCH_BAD_MARKS) or _BENCH_LOAD_ERROR.search(ln)]
    if bad:
        return False, "\n".join(
            ["✗ shell 加载扩展时报错了 ⇒ 扩展不会加载，Caps+F1 会静默失效："]
            + ["   " + ln for ln in bad[:3]]
            + ["   按报错行里的文件:行号改 extensions/raise-browser@capsplus.example/extension.js，"
               "再跑一次本自检"])

    ready = next((ln.strip() for ln in lines if BENCH_READY in ln), None)
    if ready is not None:
        missing = [need for need in BENCH_READY_NEEDS if need not in ready]
        if not missing:
            return True, ""
        return False, "\n".join(
            ["✗ 扩展加载了，但抓帧/贴图通道没挂上（缺 %s）⇒ Caps+F1 还是会没反应："
             % "、".join(missing),
             "   ready 行：" + ready,
             "   查订阅那段（signal_subscribe / screenshot_area / addTopChrome）："
             "订阅失败在 shell 里**只留一行**，不会让扩展进 ERROR"])

    if not any(BENCH_SHELL_STARTED in ln for ln in lines):
        return False, "\n".join(
            ["✗ headless gnome-shell 没起来（日志里没有 'Running GNOME Shell'）⇒ 这次自检没有结论",
             "   先看日志末尾；常见原因：这台机没有图形栈（CI / 纯 SSH）、"
             "XDG_RUNTIME_DIR 不对、或者等太短（`--timeout 40` 试试）"])

    ext_lines = [ln.strip() for ln in lines if "raise-browser" in ln]
    if ext_lines:
        return False, "\n".join(
            ["✗ shell 起来了、也提到了这个扩展，但没有 ready 行 ⇒ enable() 里抛异常了？"]
            + ["   " + ln for ln in ext_lines[-3:]])

    return False, "\n".join(
        ["✗ shell 起来了，但根本没有加载这个扩展 ⇒ 没装 / 没启用 / uuid 对不上",
         "   装（写入只在这一侧）：sh tools/install-extension.sh --sync",
         "   启用：" + "gnome-extensions enable " + EXT_UUID,
         "   查：gnome-extensions list --enabled | grep " + EXT_UUID])


def main(argv=None) -> int:
    """`python3 -m capsplus.extcheck --bench-log FILE`：给 tools/ext-bench.sh 用的判词入口。

    判词只在 Python 这一处（sh 里再写一遍 grep 就是第二份真相）；退出码 0=绿、1=红、2=用法/读不了。
    """
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m capsplus.extcheck",
                                 description="headless 扩展自检的判词（被 tools/ext-bench.sh 调用）")
    ap.add_argument("--bench-log", metavar="FILE", help="ext-bench 跑出来的 shell 日志")
    args = ap.parse_args(argv)
    if not args.bench_log:
        ap.print_help(sys.stderr)
        return 2
    try:
        # errors="replace"：日志里可能有非法字节（8190 那类半截 UTF-8 就会留下），
        # 判据不许因为读日志本身而崩。
        with open(args.bench_log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"✗ 读不了日志 {args.bench_log}：{exc}", file=sys.stderr)
        return 2
    ok, why = bench_verdict(text)
    if ok:
        return 0
    print(why, file=sys.stderr)
    return 1


if __name__ == "__main__":                                    # pragma: no cover
    sys.exit(main())
