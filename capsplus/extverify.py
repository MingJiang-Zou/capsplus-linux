# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""登录后**一条命令自证**：`tools/verify-after-login.sh` 的判词。

为什么要有它（2026-10-20）：明江那天因为改 `extension.js` 重登了 3 次，而且有一次登录后扩展直接
进了 ERROR 态 —— "重登完到底行不行"当时只能靠**按 Caps+F1 试一下**（手的记忆，没有证据）。
这条命令把"行不行"变成一张 PASS/FAIL 表，一条命令跑完，不用鼠标、不用人眼看。

七项（每一项都对应一个真出过的失败形态）：
  ① shell 单元在跑           —— 没有 shell 就没有扩展，后面的结论都没意义
  ② 本次开机有 `ready` 行     —— **有且只有** gnome-shell 进程写的算数（`_COMM=gnome-shell` 过滤）
  ③ 总线名有主               —— 扩展 `own_name` 成功了才算启用；ERROR 态时是 `(false,)`
  ④ `Probe` 往返             —— 发一个信号，journal 里必须**多出一行** `[raise-browser] probe …`
  ⑤ 真抓一帧 200×150         —— 产物是完整 PNG **且像素不是纯色**（纯色 = 抓了个空/黑）
  ⑥ 贴图 + 清贴图             —— PIN 与 CLEAR 两条路的 ack 都要回来（贴图会短暂出现在屏幕上）
  ⑦ 遮罩起停                 —— 在 **xvfb 里**建窗→量尺寸→收窗，**不碰明江的屏**

两条口径上的讲究（都是踩出来的，别改回去）：
  * **判活不认 `gdbus call …Probe`**：本机 GJS 导出不了 D-Bus 对象，那条命令只会回
    `UnknownMethod`/`ServiceUnknown`（2026-10-20 实测）。能用的形态是"**发**信号 + 读 journal"，
    以及"总线名有没有主"（`NameHasOwner`）。本文件两者都用。
  * **`gdbus emit` 的返回码不算证据**：扩展没加载时它照样 exit 0（信号广播给没人听）。
    只有 journal 里的**增量**算数。
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

EXT_UUID = "raise-browser@capsplus.example"
BUS_NAME = "io.github.capsplus.RaiseBrowser"
BUS_PATH = "/io/github/capsplus/RaiseBrowser"
READY_MARK = "[raise-browser] ready"
PROBE_MARK = "[raise-browser] probe "
READY_NEEDS = ("capture=on", "pin=on")
FRAME_W, FRAME_H = 200, 150
PROBE_TRIES = 12          # 每 250ms 一次 ⇒ 最多 3 秒
PROBE_INTERVAL = 0.25

GNOME_SHELL_UNIT = "org.gnome.Shell@*.service"


# --------------------------------------------------------------------- 取原始信息

def _run(argv, timeout=15, runner=None):
    if runner is not None:
        return runner(argv)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{exc}")


def journal_text(runner=None) -> str:
    """本次开机里 **gnome-shell 进程**说的话。

    `_COMM=gnome-shell` 不是可选的：裸 `journalctl | grep raise-browser` 会把**我自己这条命令**
    也算进去（"你命令行里的字符串也会被算进去"是本仓反复踩过的坑，docs/SHOT-TOOL.md 有记）。
    """
    proc = _run(["journalctl", "--user", "-b", "_COMM=gnome-shell", "--no-pager"], runner=runner)
    return proc.stdout or ""


def shell_unit_state(runner=None) -> tuple:
    proc = _run(["systemctl", "--user", "list-units", "--type=service", "--no-legend",
                 "--plain", GNOME_SHELL_UNIT], runner=runner)
    text = (proc.stdout or "").strip()
    if not text:
        return FAIL, f"没有 {GNOME_SHELL_UNIT} 在跑 ⇒ 这不是一个 GNOME 会话（或 shell 已挂）"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "active" and parts[3] == "running":
            return PASS, f"{parts[0]} {parts[2]}/{parts[3]}"
    return FAIL, f"单元在但没跑起来：{text.splitlines()[0].strip()}"


def ready_line_state(journal: str) -> tuple:
    line = next((ln.strip() for ln in journal.splitlines() if READY_MARK in ln), None)
    if line is None:
        return FAIL, (f"本次开机 journal 里**没有** `{READY_MARK}` 行 ⇒ 扩展没被加载"
                      f"（挂载/启用/代码坏都会长这样）。跑 `sh tools/ext-bench.sh` 分辨是"
                      f"「文件坏了」还是「只差一次重登」")
    missing = [need for need in READY_NEEDS if need not in line]
    if missing:
        return FAIL, f"ready 行缺 {'、'.join(missing)} ⇒ 通道没挂上：{line}"
    return PASS, line.split(": ", 1)[-1]


def name_owned_state(runner=None) -> tuple:
    """总线名有没有主 —— 比 `gnome-extensions info` 更接近事实（壳说 ACTIVE 也可能没导出）。"""
    proc = _run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                 "--object-path", "/org/freedesktop/DBus",
                 "--method", "org.freedesktop.DBus.NameHasOwner", BUS_NAME], runner=runner)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "(true" in out:
        return PASS, f"{BUS_NAME} 有主（扩展 enable() 跑到了 own_name）"
    return FAIL, (f"{BUS_NAME} 没人占（回 {out or '空'}）⇒ 扩展没启用或进了 ERROR 态 ⇒ "
                  f"Caps 热键会静默失效")


def count_probes(journal: str) -> int:
    return sum(1 for ln in journal.splitlines() if PROBE_MARK in ln)


def probe_roundtrip_state(journal_fn, emit_fn, sleep_fn=time.sleep, on_tick=None) -> tuple:
    """发 `Probe` 之前/之后数 journal 行；**只有增量算数**（emit 的返回码不算）。"""
    before = count_probes(journal_fn())
    emit_fn()
    for i in range(PROBE_TRIES):
        sleep_fn(PROBE_INTERVAL)
        after = count_probes(journal_fn())
        if on_tick:
            on_tick(i, after - before)
        if after > before:
            return PASS, f"Probe 往返成立：journal 里 probe 行 {before} → {after}"
    return FAIL, (f"发了 Probe 但 journal 里的 probe 行没有增加（仍是 {before} 行）"
                  f" ⇒ 没有人在听这个信号（扩展没加载 / 订阅没挂上）")


# --------------------------------------------------------------------- 抓帧与像素

def distinct_pixels(raw: bytes, width: int, height: int) -> int:
    """裸 RGBA 里有几种不同像素。**纯色 = 抓了个空/全黑** —— 这正是要看的那一眼。"""
    if not raw or width <= 0 or height <= 0:
        return 0
    return len({raw[i:i + 4] for i in range(0, min(len(raw), width * height * 4), 4)})


def regions_to_try(monitors) -> list:
    """先打**屏的左上角**（GNOME 顶栏那儿有 "Activities" 字样，几乎不可能纯色），
    万一那块真是一片纯色，退到屏中心再试一次 —— 不让人因为"选了个空region"吃假红。"""
    out = []
    for mon in monitors or []:
        x, y, w, h = mon
        out.append((x + 8, y + 8, FRAME_W, FRAME_H))
        out.append((x + max(0, w // 2 - FRAME_W // 2), y + max(0, h // 2 - FRAME_H // 2),
                    FRAME_W, FRAME_H))
    if not out:
        out.append((0, 0, FRAME_W, FRAME_H))
    return out[:4]


def capture_state(out_dir: Path, capture_fn, monitor_fn, pixel_fn) -> tuple:
    """真抓一帧：产物必须是完整 PNG **且非纯色**。

    每次都用**新的文件名**：扩展侧有 `duplicateRequest` 去重（同一个 outPath 只认第一次），
    沿用旧路径会让"第二次跑"永远等不到产物 —— 那是判据自己的 bug，不是扩展的。

    重试的**只在一种情况下**才换 region：抓到了但**是纯色**（那是"选了个空 region"，换一块就有）。
    抓帧**超时**说明通道本身就是死的（扩展没加载 / 订阅没挂上），换十块屏也一样超时 ——
    所以在超时那一刻就收手，别让人白等 4×5 秒（2026-10-20 实测：ERROR 态下第一版会耗 20 秒，
    这是被自己的重试逻辑拖的，不是被测对象慢）。
    """
    try:
        monitors = monitor_fn()
    except Exception as exc:                                  # noqa: BLE001（没有显示服务器）
        return FAIL, f"拿不到屏幕几何：{type(exc).__name__}: {exc}"
    tried = []
    for (x, y, w, h) in regions_to_try(monitors):
        out = out_dir / f"verify-frame-{int(time.time() * 1000)}-{x}-{y}.png"
        ok, why = capture_fn(x, y, w, h, out)
        if not ok:
            first = why.splitlines()[0] if why else "(没有说明)"
            return FAIL, (f"抓帧没成（@({x},{y})）：{first}\n"
                          f"      超时不换 region 再试 —— 通道没挂上时换哪块屏都一样超时")
        try:
            raw, dw, dh = pixel_fn(out)
            n = distinct_pixels(raw, dw, dh)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"({x},{y}) 读不出像素：{type(exc).__name__}: {exc}")
            continue
        if n < 2:
            tried.append(f"({x},{y}) 抓到了但**是纯色**（只有 1 种像素）⇒ 像空屏/黑屏")
            continue
        return PASS, (f"实抓 {w}×{h} @({x},{y})：{dw}×{dh}、{n} 种像素（非纯色）"
                      f" → {out.name}")
    return FAIL, "没抓到一张像样的帧：" + "；".join(tried or ["(没有可试的 region)"])


def pin_clear_state(png: Path | None, pin_fn, clear_fn) -> tuple:
    if png is None:
        return SKIP, "没抓到帧，没有可贴的图 ⇒ 跳过（先把第 ⑤ 项修好）"
    ok, why = pin_fn(png)
    if not ok:
        return FAIL, "贴图没成：" + "\n      ".join(why.splitlines()[:3])
    ok2, why2 = clear_fn()
    if not ok2:
        return FAIL, "清贴图没成（贴图可能还留在屏幕上）：" + "\n      ".join(why2.splitlines()[:3])
    return PASS, f"贴图→清贴图都成了（贴图 {why.splitlines()[0][:60]}…）"


# --------------------------------------------------------------------- 遮罩（在 xvfb 里）

# 为什么在 xvfb 里跑：遮罩是**全屏置顶窗口**，真跑会在明江屏幕上盖住一切 —— 那正是他抱怨过的事
# （"老把 qbar 和设置页面换出来"）。xvfb 里建窗→量尺寸→收窗，验的是同一段真代码路径。
# 三条安全阀（本仓的硬口径，写进判据里）：① `-u WAYLAND_DISPLAY` 不够，还要 `GDK_BACKEND=x11`
# 且显式 `CAPSPLUS_GUI=1`；② 驱动里**先查后端**，是 Wayland 就拒跑（不许赌环境变量）；③ 跑完收窗。
MASK_DRIVER = r'''
import sys
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib
cls = Gdk.Display.get_default().__class__.__name__
if "Wayland" in cls:
    print("MASK_REFUSE 这个显示后端是 " + cls + "（真机！）—— 拒绝在真屏上弹遮罩")
    sys.exit(2)
from capsplus.shot_overlay import Overlay, monitor_rects
from capsplus.shot_overlay import _cairo          # cairo 的引法照抄仓里那一处，别自己发明
from capsplus.shot_frames import Frame            # 冻结帧的真类型：遮罩靠 frame.rect 找自己那块屏
cairo = _cairo()
monitors, display = monitor_rects()
if not monitors or display is None:
    print("MASK_FAIL 拿不到显示器几何")
    sys.exit(1)
# ⚠ 帧必须是 `Frame`（不是裸 cairo.ImageSurface）：`_frame_for()` 要读 `frame.rect`，
#   给裸 surface 会让**每帧的 draw 回调抛 AttributeError** —— 而窗口照样能 map、几何自检照样过，
#   于是判据会得到一个**假绿**（2026-10-20 实测踩到：PASS 的同时 stderr 里在刷 traceback）。
frames = []
for mon in monitors:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, mon[2]), max(1, mon[3]))
    frames.append(Frame(mon, surface))
ov = Overlay(frames=frames, monitors=monitors)
loop = GLib.MainLoop()
ov.build(display, loop.quit)
GLib.timeout_add(700, lambda: (ov.cancel(), False)[1])
loop.run()
if ov.outcome == "cancel" and ov.geometry_ok:
    print("MASK_OK %d 块屏：建窗 → 几何自检通过 → 收窗（outcome=cancel）" % len(monitors))
    sys.exit(0)
print("MASK_FAIL outcome=%s geometry_ok=%s failure=%s" % (ov.outcome, ov.geometry_ok, ov.failure))
sys.exit(1)
'''


def mask_state(runner=None, which=None) -> tuple:
    which = which or shutil.which
    for tool in ("xvfb-run", "Xvfb"):
        if which(tool) is None:
            return SKIP, f"没有 {tool} ⇒ 遮罩那一步跳过（它必须在隔离屏里跑，不许在真屏上弹）"
    argv = ["xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
            "env", "-u", "WAYLAND_DISPLAY", "GDK_BACKEND=x11", "CAPSPLUS_GUI=1",
            sys.executable, "-c", MASK_DRIVER]
    try:
        proc = _run(argv, timeout=60, runner=runner)
    except Exception as exc:                                  # noqa: BLE001
        return FAIL, f"xvfb 里跑遮罩失败：{type(exc).__name__}: {exc}"
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # ⚠ 光看 `MASK_OK` 不够：GTK 的回调抛异常**不会**让进程非零退出（PyGObject 打一份 traceback
    #   就继续跑），窗口照样 map、几何自检照样过 —— 那就是一个假绿。2026-10-20 实测踩到过：
    #   帧给错了类型，draw 回调每帧抛 AttributeError，而这一项当时报的是 PASS。
    if "Traceback (most recent call last)" in text:
        bad = [ln for ln in text.splitlines() if ln.strip().startswith(("File ", "AttributeError",
                                                                       "TypeError", "ValueError"))]
        return FAIL, ("遮罩起停：进程活着但**有回调在抛异常**（假绿陷阱）⇒ 遮罩其实是坏的\n      "
                      + "\n      ".join(bad[-3:] or text.splitlines()[-3:]))
    if proc.returncode == 0 and "MASK_OK" in text:
        return PASS, text.split("MASK_OK", 1)[1].strip() or "建窗/收窗都成了"
    if proc.returncode == 2 and "MASK_REFUSE" in text:
        return FAIL, text
    return FAIL, ("遮罩起停失败：" + "\n      ".join(text.splitlines()[-4:] or ["(没有输出)"]))


def mask_deps_present(which=None) -> bool:
    """遮罩这一步要不要跑。CI 上没有 xvfb 就跳过，但真机上**必须**跑。"""
    which = which or shutil.which
    return all(which(t) is not None for t in ("xvfb-run", "Xvfb"))


# --------------------------------------------------------------------- 汇总与表格

CIRCLED = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨"]
MARKS = {PASS: "✓", FAIL: "✗", SKIP: "•"}
LABELS = ["shell 单元在跑", "本次开机有 ready 行", "扩展总线名有主", "Probe 往返",
          "真抓一帧 200×150 且非纯色", "贴图 + 清贴图", "遮罩起停（xvfb 里）"]


def render_table(rows, when=None, stream=None) -> str:
    """PASS/FAIL 表。`rows` 是 `[(state, detail)]`，顺序与 LABELS 对应。"""
    stream = stream or sys.stdout
    when = when or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    width = max(len(label) for label in LABELS[:len(rows)]) + 2
    lines = [f"登录后自证 · raise-browser 扩展 · {when}",
             "─" * 66]
    for idx, (state, detail) in enumerate(rows):
        label = LABELS[idx] if idx < len(LABELS) else f"第 {idx + 1} 项"
        head = f"{CIRCLED[idx]} {label}".ljust(width)
        lines.append(f"  {MARKS[state]} {head} {state:<4} {detail}")
    n_fail = sum(1 for state, _ in rows if state == FAIL)
    n_skip = sum(1 for state, _ in rows if state == SKIP)
    n_pass = len(rows) - n_fail - n_skip
    verdict = ("PASS —— 抓帧/贴图/遮罩这条链在**这个会话**里是活的"
               if n_fail == 0 else
               "FAIL —— 有 %d 项没过，Caps+F1 会静默失效或行为不对" % n_fail)
    lines.append("─" * 66)
    lines.append(f"  {verdict}（PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}）")
    if n_fail:
        lines.append("  下一步：`sh tools/install-extension.sh --verify`（文件对不对）→ "
                     "`sh tools/ext-bench.sh`（能不能加载，不用重登）→ 都绿了再注销重登")
    text = "\n".join(lines)
    print(text, file=stream)
    return text


def run_all(*, journal_fn=None, unit_fn=None, owned_fn=None, emit_fn=None,
            capture_fn=None, monitor_fn=None, pixel_fn=None, pin_fn=None,
            clear_fn=None, sleep_fn=time.sleep, mask_runner=None, which=None,
            out_dir=None, no_screen=False) -> list:
    """跑完七项，返回 `[(state, detail)]`。所有外部依赖都可注入（判据不碰真屏、不起真进程）。"""
    journal_fn = journal_fn or journal_text
    rows = []
    rows.append(unit_fn() if unit_fn else shell_unit_state())
    journal = journal_fn()
    rows.append(ready_line_state(journal))
    rows.append(owned_fn() if owned_fn else name_owned_state())
    if emit_fn is None:
        def emit_fn():
            _run(["gdbus", "emit", "--session", "--object-path", BUS_PATH,
                  "--signal", BUS_NAME + ".Probe"], timeout=10)
    rows.append(probe_roundtrip_state(journal_fn, emit_fn, sleep_fn=sleep_fn))

    # 中间产物的落点：调用方给了就用它（`--keep DIR` 走这条），没给就自建**并自己清掉**。
    # ⚠ 为什么要强调"自己清"（2026-10-20 实测踩到）：第一版每跑一次就 `mkdtemp` 一个
    #   `/tmp/capsplus-verify-*` 且从不回收 —— 跑全量单测几轮就攒了 35 个。本仓为这类事吃过亏
    #   （`/tmp/capsplus-extjs-*` 一天 141 个），所以"谁建的谁清"写进代码，并且有判据钉住。
    own_tmp = None
    if out_dir is None:
        own_tmp = Path(tempfile.mkdtemp(prefix="capsplus-verify-"))
        out_dir = own_tmp
    tmp = Path(out_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    png = None
    if capture_fn is None:
        from capsplus import shot_capture as SC
        from capsplus import shot_overlay as SO
        from capsplus import shot_pin as SP

        def capture_fn(x, y, w, h, out):
            return SC.capture(x, y, w, h, out, timeout_ms=5000)

        def monitor_fn():
            rects, _display = SO.monitor_rects()
            return rects

        def pixel_fn(path):
            raw_path, dw, dh = SP.to_raw_rgba(path, tmp)
            return Path(raw_path).read_bytes(), dw, dh

        def pin_fn(path):
            return SP.pin(path, 40, 40, out_dir=tmp,
                          ack_path=tmp / "pin.ack")

        def clear_fn():
            return SP.clear(ack_path=tmp / "clear.ack")
    else:
        monitor_fn = monitor_fn or (lambda: [(0, 0, 1920, 1080)])
        pixel_fn = pixel_fn or (lambda _p: (b"", 0, 0))

    # 抓帧的产物路径每次都是新的（扩展侧有 duplicateRequest 去重），并且要能从这一层看到
    ok_capture, why_capture = capture_state(tmp, capture_fn, monitor_fn, pixel_fn)
    rows.append((ok_capture, why_capture))
    if ok_capture:
        m = re.search(r"→ (\S+\.png)", why_capture)
        if m:
            png = tmp / m.group(1)

    if no_screen:
        rows.append((SKIP, "--no-screen：贴图会在明江屏幕上短暂出现，这次跳过"))
    else:
        rows.append(pin_clear_state(png, pin_fn, clear_fn))

    if which is not None or mask_runner is not None:
        rows.append(mask_state(runner=mask_runner, which=which))
    else:
        rows.append(mask_state())

    if own_tmp is not None:
        # 自建的临时目录由自己收掉；顺手把行里那些已经失效的绝对路径抹成文件名
        # （留着"死了的路径"比给文件名更误导人）。
        rows = [(state, detail.replace(str(own_tmp) + "/", "").replace(str(own_tmp), ""))
                for state, detail in rows]
        shutil.rmtree(own_tmp, ignore_errors=True)
    return rows


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m capsplus.extverify",
        description="登录后一条命令自证：扩展 + 抓帧 + 贴图 + 遮罩（跑完给一张 PASS/FAIL 表）")
    ap.add_argument("--no-screen", action="store_true",
                    help="跳过贴图那一步（它会让一张小图在屏幕上短暂出现）")
    ap.add_argument("--keep", metavar="DIR",
                    help="把抓到的帧等中间产物留在这个目录（默认 tmpfs 临时目录，跑完就没了）")
    args = ap.parse_args(argv)

    rows = run_all(out_dir=args.keep, no_screen=args.no_screen)
    render_table(rows)
    if args.keep:
        print(f"  中间产物留在：{args.keep}")
    return 0 if all(state != FAIL for state, _ in rows) else 1


if __name__ == "__main__":                                    # pragma: no cover
    sys.exit(main())