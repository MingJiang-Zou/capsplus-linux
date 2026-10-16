#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""A/B 实测「先激活浏览器再开网址」到底有没有效 —— 不靠人眼数窗口。

原理：Wayland 下外部拿不到窗口列表（截图接口 AccessDenied、没有可用的取窗口 API），
但**扩展在 mutter 进程里能拿到**。扩展把窗口数写进 journal
（`[raise-browser] probe … windows=N`），本工具发 `Probe` 信号取数、开一个网址、再取一次：
  **N 变大 ⇒ 浏览器另画了一扇窗**；**N 不变 ⇒ 在原窗口开了标签页**（我们要的结果）。

两轮各开一个不同网址，避免同网址被去重：
  A 轮：不激活直接开 ⇒ 预期 +1（用来证明复现条件成立）
  B 轮：先发 RaiseBrowser 再开 ⇒ 预期 0（激活起作用了）

**前提**：跑的时候焦点**不在**浏览器上（否则本来就会开标签页，A 轮测不出东西）。
从终端/脚本跑天然满足。

用法：
    python3 tools/verify-raise-browser.py            # 两轮都跑
    python3 tools/verify-raise-browser.py --rounds 1 # 只跑 B 轮
"""
import argparse
import datetime
import re
import subprocess
import sys
import time

IFACE = "io.github.capsplus.RaiseBrowser"
PATH = "/io/github/capsplus/RaiseBrowser"
LINE = re.compile(r"\[raise-browser\].*?windows=(\d+)")
BASE_WAIT = 0.8           # 发信号 → shell 写 journal → 再读，留点余量
OPEN_WAIT = 2.5           # 浏览器建窗口/标签要时间


def emit(member):
    """发一发信号。本机实测：PyGI 没有 `Gio.DBus.session`，得用 `Gio.bus_get_sync`；
    `emit_signal(destination, path, interface, member, parameters)` 五参，parameters 可为 None。"""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    conn.emit_signal(None, PATH, IFACE, member, None)
    pump(150)


def pump(ms: int) -> None:
    """把 GLib 主循环真跑 ms 毫秒 —— **`emit_signal` 只是入队，靠主循环派发才写得出去**。

    2026-10-16 实测三种写法：
      * `time.sleep()`：送不到（睡过去主循环根本没转）；
      * `MainContext.iteration(False)` 空转：也送不到；
      * `GLib.MainLoop()` + `timeout_add` 退出：**每次都送到** ✓
    所以别把这里"优化"回 sleep/iteration —— 那不是慢，那是没发。
    """
    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
    loop = GLib.MainLoop()
    GLib.timeout_add(max(20, int(ms)), loop.quit)
    loop.run()


def windows_since(when_iso):
    """取 `when_iso` 之后 journal 里最新一条 windows= 计数；取不到返回 None。"""
    proc = subprocess.run(["journalctl", "--user", "--since", when_iso,
                           "-o", "cat", "--no-pager"],
                          capture_output=True, text=True, timeout=20)
    hits = LINE.findall(proc.stdout)
    return int(hits[-1]) if hits else None


def now_iso():
    # journalctl 的 --since 用本地时间字符串，秒级就够
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def probe_count():
    """发一次 Probe 并取回窗口数（时间窗开在发信号之前，保证取到的是这一条）。"""
    stamp = now_iso()
    emit("Probe")
    time.sleep(BASE_WAIT)
    return windows_since(stamp)


def measure(label, url, raise_first):
    before = probe_count()
    if before is None:
        print(f"  {label}: 取不到窗口数 —— 扩展没加载？"
              "查 journalctl --user --since '-5 min' | grep raise-browser")
        return None
    if raise_first:
        emit("RaiseBrowser")
        time.sleep(0.15)                      # 激活落地约一帧
    subprocess.run(["xdg-open", url], capture_output=True, text=True, timeout=15)
    time.sleep(OPEN_WAIT)
    after = probe_count()
    if after is None:
        print(f"  {label}: 事后取不到窗口数")
        return None
    delta = after - before
    verdict = "原窗口开标签页 ✓" if delta == 0 else f"新画了一扇窗（+{delta}）"
    print(f"  {label}: 窗口 {before} → {after}（{delta:+d}）⇒ {verdict}")
    return delta


def main():
    ap = argparse.ArgumentParser(description="A/B 验证浏览器窗口复用")
    ap.add_argument("--rounds", type=int, default=2, choices=(1, 2), help="1 = 只测激活那一轮")
    ap.add_argument("--stamp", default=str(int(time.time())), help="网址唯一标记，避免同网址去重")
    args = ap.parse_args()

    try:
        n = probe_count()
    except Exception as exc:                  # 连不上总线
        print(f"✗ 发不出 Probe：{type(exc).__name__} {exc}", file=sys.stderr)
        return 2
    if n is None:
        print("✗ 扩展不可用：装了吗（tools/install-extension.sh）、重登了吗、journal 里有报错吗")
        print("  查：journalctl --user --since '-10 min' | grep raise-browser")
        return 2
    print(f"• 扩展在跑，当前浏览器窗口数 = {n}")

    results = {}
    if args.rounds == 2:
        print("A 轮（**不**先激活 —— 期望复现『另起新窗口』）：")
        results["A"] = measure("A", f"https://example.com/capsplus-no-raise-{args.stamp}", False)
    print("B 轮（先让扩展激活浏览器 —— 期望『原窗口开标签页』）：")
    results["B"] = measure("B", f"https://example.com/capsplus-with-raise-{args.stamp}", True)

    print("\n结论：")
    if results.get("B") is None:
        print("  ✗ 测不了（取数失败）")
        return 2
    if results["B"] == 0:
        print("  ✅ 激活后浏览器在原窗口开标签页 —— 修法有效")
    else:
        print(f"  ✗ 激活之后仍新画窗口（+{results['B']}）：扩展的 activate() 没能改变浏览器的决定，")
        print("     得换思路（DevTools 那条路，或查浏览器自身的窗口策略）")
    if results.get("A") == 0:
        print("  ⚠ A 轮也没新窗口 ⇒ 复现条件没满足（焦点可能还在浏览器上），这轮数据不算数")
    return 0 if results["B"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
