#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""A/B 验收「先激活浏览器再开网址」到底有没有效 —— 不靠人眼数窗口。

原理：仓内扩展 `raise-browser` 的 `Probe` 方法会报出默认浏览器现在有几扇窗。
开一个网址之后窗口数 **+1 ⇒ 新画了一扇窗**；**不变 ⇒ 在原窗口开了标签页**（我们要的结果）。

两轮各开一个不同网址，避免"同网址被去重"干扰：
  A 轮：什么都不做直接开 ⇒ 预期 +1（这就是坏行为，用来证明问题还在/复现条件成立）
  B 轮：先调 RaiseBrowser 再开 ⇒ 预期 0（扩展的激活起作用了）

**前提**：跑的时候焦点**不在**浏览器上（否则本来就会开标签页，A 轮也测不出东西）。
从终端/脚本跑就天然满足；如果你在浏览器里开着终端跑，先把焦点点到别处。

用法：
    python3 tools/verify-raise-browser.py                  # 默认两轮
    python3 tools/verify-raise-browser.py --rounds 1       # 只测 B 轮（已经确认 A 轮能复现时）
"""
import argparse
import subprocess
import sys
import time

PROBE = ["gdbus", "call", "--session",
         "--dest", "io.github.capsplus.RaiseBrowser",
         "--object-path", "/io/github/capsplus/RaiseBrowser",
         "--method", "io.github.capsplus.RaiseBrowser.Probe"]
RAISE = ["gdbus", "call", "--session",
         "--dest", "io.github.capsplus.RaiseBrowser",
         "--object-path", "/io/github/capsplus/RaiseBrowser",
         "--method", "io.github.capsplus.RaiseBrowser.RaiseBrowser"]
SETTLE_S = 2.5          # 浏览器开窗口/建标签要时间，量太早会误判


def probe():
    """返回 (窗口数, 原始报告字符串)。扩展没装 → None。"""
    try:
        out = subprocess.run(PROBE, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"✗ Probe 失败：{exc}", file=sys.stderr)
        return None
    if "windows=" not in out:
        print(f"✗ Probe 没回窗口数：{out.strip()[:200]}\n  （扩展装了吗？先跑 tools/install-extension.sh 再注销重登）",
              file=sys.stderr)
        return None
    try:
        return int(out.split("windows=")[1].split()[0]), out.strip()
    except (ValueError, IndexError):
        return None


def open_url(url):
    subprocess.run(["xdg-open", url], capture_output=True, text=True, timeout=15)


def raise_browser():
    try:
        out = subprocess.run(RAISE, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"调用失败：{exc}"
    return out


def measure(label, url, first=None):
    before = probe()
    if before is None:
        return None
    if first is not None:
        first()
    open_url(url)
    time.sleep(SETTLE_S)
    after = probe()
    if after is None:
        return None
    delta = after[0] - before[0]
    verdict = "原窗口开标签页 ✓" if delta == 0 else f"新画了一扇窗（+{delta}）"
    print(f"  {label}: 窗口 {before[0]} → {after[0]}（{delta:+d}）⇒ {verdict}")
    print(f"      {after[1][:150]}")
    return delta


def main():
    ap = argparse.ArgumentParser(description="A/B 验证浏览器窗口复用")
    ap.add_argument("--rounds", type=int, default=2, choices=(1, 2), help="1 = 只测激活那一轮")
    ap.add_argument("--stamp", default=str(int(time.time())), help="网址里的唯一标记，避免同网址去重")
    args = ap.parse_args()

    print(f"• 探针: {probe()[1] if probe() else '不可用'}")
    results = {}
    if args.rounds == 2:
        print("A 轮（**不**先激活 —— 期望复现「另起新窗口」）：")
        results["A"] = measure("A", f"https://example.com/capsplus-no-raise-{args.stamp}")
    print("B 轮（先让扩展激活浏览器 —— 期望「原窗口开标签页」）：")
    results["B"] = measure("B", f"https://example.com/capsplus-with-raise-{args.stamp}",
                            first=lambda: (print(f"      RaiseBrowser → {raise_browser()}"),
                                           time.sleep(0.15)))

    print("\n结论：")
    if results.get("B") is None:
        print("  ✗ 测不了：扩展的 Probe 不可用（装了吗？重登了吗？）")
        return 2
    if results["B"] == 0:
        print("  ✅ 激活后浏览器在原窗口开标签页 —— 修法有效")
    else:
        print(f"  ✗ 激活之后浏览器仍新画窗口（+{results['B']}）：扩展的 activate() 没能改变浏览器的决定，")
        print("     得换思路（比如 DevTools 那条路，或查浏览器自身的窗口策略）")
    if results.get("A") == 0:
        print("  ⚠ A 轮也没新窗口 ⇒ 复现条件没满足（焦点可能还在浏览器上），这轮数据不算数")
    return 0 if results["B"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
