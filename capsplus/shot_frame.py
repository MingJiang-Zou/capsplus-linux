# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""截图链路里**不碰 gi** 的那一半：信号实参、等产物、超时话术、journal 解析、选区几何。

为什么单独一层（2026-10-19）：这一层能在没有显示服务器、没有扩展、**没有真屏**的机器上单测
（`tests/test_shot_frame.py` 就是这么钉的）；碰 gi 的部分全在 `capsplus/shot_capture.py` 里。

链路（每一步都是实测定下来的，别改）：
  capsplus emit_signal 五实参发 `CaptureArea(x, y, w, h, outPath)`
    → shell 进程里的 raise-browser 扩展 `Shell.Screenshot.screenshot_area(...)`
      先写 `<outPath>.part`、成功后 rename 成 outPath（**outPath 一出现就是完整 PNG**）
    → capsplus 等文件出现（超时 3s）。
  没有回复通道（本机 GJS 不能 export D-Bus 对象、不能回值，实测见扩展头注）⇒「成没成」只认产物：
  文件出现=成功；超时=给可操作的三条排查话术（没装 / 没重载 / 没订阅）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

# 与 `extensions/raise-browser@capsplus.example/extension.js` 里的常量**必须逐字一致**：
# tests/test_shot_extension.py 拿这两个常量去扩展源码里找，找不到就红。
BUS_NAME = "io.github.capsplus.RaiseBrowser"
BUS_PATH = "/io/github/capsplus/RaiseBrowser"
BUS_IFACE = "io.github.capsplus.RaiseBrowser"
M_CAPTURE = "CaptureArea"
M_PIN = "PinImage"
M_CLEAR = "ClearPins"
CAPTURE_SIG = "(iiiis)"          # x, y, w, h, outPath
# pngPath, rawPath, w, h, x, y, ackPath —— 2026-10-19 加了开头那个 pngPath：
# 贴图工具条上的"复制/存盘"要的是**PNG 字节**（`St.Clipboard.set_content(…, 'image/png', bytes)`，
# 用法抄自 shell 自己的 screenshot.js），而 shell 侧拿到的原本只有裸 RGBA。
PIN_SIG = "(ssiiiis)"
CLEAR_SIG = "(s)"                # ackPath（可以为空）—— `Caps+Esc` 全局清空贴图
EXT_UUID = "raise-browser@capsplus.example"
LOG_MARK = "[raise-browser]"

CAPTURE_TIMEOUT_MS = 3000        # 抓一张 1080p 实测在百毫秒级；3s 是"明显不正常"的线
POLL_MS = 50
MIN_SIZE = 3                     # 小于这个不算"选了一块"（手抖点一下不该出图）
SETTLE_MS = 150                  # 遮罩藏起来后、下发抓帧前等合成器一帧的时间（见 shot_overlay.py）
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"IEND\xaeB`\x82"


def capture_signal_args(x: int, y: int, w: int, h: int, out_path) -> tuple:
    """emit_signal 的五个实参（最后一个是要包成 `GLib.Variant(CAPTURE_SIG)` 的元组）。

    实参顺序是这台机实测出来的：`emit_signal(destination, path, interface, member, parameters)`；
    `parameters=None` 那种无参信号是给 RaiseBrowser/Probe 用的，抓帧必须带参数。
    """
    return (None, BUS_PATH, BUS_IFACE, M_CAPTURE,
            (int(x), int(y), int(w), int(h), str(out_path)))


def pin_signal_args(png_path, raw_path, w: int, h: int, x: int, y: int, ack_path) -> tuple:
    """贴图信号：给扩展一张**已经是 RGBA 原图**的文件（PNG 解码留给 capsplus，见 shot_pin.py）。

    `png_path` 是同一张图的 PNG 版，只给工具条/菜单里的"复制""存盘"用（见 `PIN_SIG` 的注释）。
    """
    return (None, BUS_PATH, BUS_IFACE, M_PIN,
            (str(png_path), str(raw_path), int(w), int(h), int(x), int(y), str(ack_path)))


def clear_signal_args(ack_path: str = "") -> tuple:
    """清空所有贴图的信号（`Caps+Esc`）。没有贴图时调用也安全 —— 扩展侧只是清 0 张 + 写 ack。"""
    return (None, BUS_PATH, BUS_IFACE, M_CLEAR, (str(ack_path or ""),))


def wait_for_file(path, timeout_ms: int = CAPTURE_TIMEOUT_MS, *, poll_ms: int = POLL_MS,
                  exists=None, sleep=time.sleep, now=time.monotonic) -> tuple:
    """等产物出现；返回 `(ok, waited_ms)`。

    三个注入点（`exists` / `sleep` / `now`）是给判据用的：假时钟不用真等 3 秒。
    另加一条**兜底次数上限**：即便 `now` 不前进（判据写错、时钟被 mock 坏）也不会死循环，
    超时线到了就返回 False —— 挂在 CI 上的判据比红着的判据更糟。
    """
    exists = exists or (lambda p: Path(p).exists())
    step = max(poll_ms, 1) / 1000.0
    limit = max(timeout_ms, 0) / 1000.0
    t0 = now()
    for _ in range(int(limit / step) + 2):
        if exists(path):
            return True, int((now() - t0) * 1000)
        if now() - t0 >= limit:
            return False, int((now() - t0) * 1000)
        sleep(step)
    return False, int((now() - t0) * 1000)


def is_complete_png(path) -> tuple:
    """`(完整?, 一句话原因)`：PNG 魔数 + 结尾 IEND。

    为什么要单看一眼：扩展是"写完 .part 再 rename"，理论上看不到半张图；
    真看到半张图就说明那条 rename 链路坏了，得当场说出来，不能拿出去当截图用。
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        return False, f"读不到 {p}：{exc}"
    if size == 0:
        return False, f"{p} 是 0 字节"
    try:
        with p.open("rb") as fh:
            head = fh.read(8)
            fh.seek(max(0, size - 12))
            tail = fh.read(12)
    except OSError as exc:
        return False, f"读不到 {p}：{exc}"
    if not head.startswith(PNG_MAGIC):
        return False, f"{p} 开头不是 PNG 魔数（前 8 字节 {head!r}）"
    if PNG_IEND not in tail:
        return False, f"{p} 结尾没有 IEND，是半张图（{size} 字节）"
    return True, f"{size} 字节"


def journal_command(since_iso: str) -> list:
    """读 shell 那侧的痕迹。`journalctl --user` 就够（扩展跑在用户会话的 gnome-shell 里）。"""
    return ["journalctl", "--user", "--no-pager", "--since", since_iso, "-n", "200"]


_LOG_RE = re.compile(r"result=(?P<result>\S+)(?P<rest>.*)$")   # 抓帧与贴图共用一条格式
_KV_RE = re.compile(r"(?P<k>[a-z_]+)=(?P<v>\S+)")


def _parse_marked(text: str, marker: str) -> list:
    """journal 里 `… <marker> result=…` 那几行 → `[{result, raw, <其它 key=value>}]`。"""
    out = []
    for line in text.splitlines():
        if LOG_MARK not in line or marker not in line:
            continue
        m = _LOG_RE.search(line[line.index(marker):])
        if not m:
            continue
        rec = {"result": m.group("result"), "raw": line.strip()}
        for kv in _KV_RE.finditer(m.group("rest")):
            rec[kv.group("k")] = kv.group("v")
        out.append(rec)
    return out


def parse_shot_log(text: str) -> list:
    """从 journal 尾巴里挑出抓帧那几行 → `[{result, path, ms, rect, scale, why, raw}]`。

    格式由扩展写死（`console.log` 一行）：
      `[raise-browser] shot result=ok path=/… rect=0,0,1920,1080 scale=1 ms=42`
    判据用的就是这条格式：扩展改了格式而这里没跟着改 ⇒ 判据红（跨语言契约）。
    """
    return _parse_marked(text, "shot result=")


def parse_pin_log(text: str) -> list:
    """贴图那几行（`[raise-browser] pin result=ok path=… at=… left=1 ms=12`）。"""
    return _parse_marked(text, "pin result=")


# ------------------------------------------------------- 失败必须出声（桌面通知）

# 为什么要通知（2026-10-20）：`Caps+F1` 那条路是 keyd 的 `command(...)` 起的进程，stdout/stderr
# 落在 journal 里没人看。扩展没加载 / 抓帧超时的**唯一现象**就是"按了没反应"，于是用户只能靠
# 反复重登来试探（他原话：「老是要重登 感觉也不好吧」）。所以失败时除了 stderr，还得**弹一下**，
# 并且通知里必须写"下一步跑什么"，不然只是把"没反应"换成"看不懂"。
NOTIFY_APP = "capsplus"
NOTIFY_TITLE = "Capslock+ 截图没成"

# 同一进程里同一条失败只喊一次：抓帧失败后调用方可能连着念两遍话术，别弹两条一样的通知。
_notified: set = set()


def _notify_allowed() -> bool:
    """现在该不该弹桌面通知 —— 三条闸，任何一条不满足都不弹。

    * `CAPSPLUS_NOTIFY=0`：开发/脚本里显式关掉；
    * 进程里已经 import 了 `unittest`：这是单测，**绝不许碰屏幕**（跑一次全量能踩出十几条通知，
      和仓库里 `CAPSPLUS_GUI` 那条纪律同源：界面类的副作用默认不许发生）；
    * stderr 是终端：人在看着，话术已经打在屏幕上了，再弹窗只是打断他干活。
    """
    if os.environ.get("CAPSPLUS_NOTIFY") == "0":
        return False
    if "unittest" in sys.modules:
        return False
    try:
        if sys.stderr is not None and sys.stderr.isatty():
            return False
    except (AttributeError, ValueError):        # 被替换成怪对象（测试夹具）时当作"不是终端"
        pass
    return True


def _send_notification(title: str, body: str, *, timeout_ms: int = 6000) -> None:
    """发一条桌面通知：GIO 优先，没有 gi 才退回 `notify-send`（别为一个通知引新依赖）。

    GIO 这条路是 `Gio.DBusProxy` 直接调 `org.freedesktop.Notifications.Notify`：不建 Application、
    不接管进程生命周期，一次同步调用就完事。**注意 gi 必须迟到导入** —— 本模块要被"没装
    python3-gi"的 CI 导入（`tests/test_no_gi_smoke.py`），顶层裸写 `import gi` 会让整套测试
    在收集阶段就崩。
    任何异常都吞掉：通知发不出去不该比失败本身更严重，更不许因此让 capsplus 返回非 0。
    """
    try:
        from gi.repository import Gio, GLib                     # noqa: WPS433（迟到导入，见上）
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", None)
        proxy.call_sync(
            "Notify",
            GLib.Variant("(susssasa{sv}i)",
                         (NOTIFY_APP, 0, "", title, body, [], {}, timeout_ms)),
            Gio.DBusCallFlags.NONE, 3000, None)
        return
    except Exception:                                            # noqa: BLE001（没有 gi / 没有 GUI）
        pass
    try:
        subprocess.run(["notify-send", "-a", NOTIFY_APP, title, body],
                       capture_output=True, timeout=3)
    except Exception:                                            # noqa: BLE001
        pass


def notify_failure(text: str, *, title: str = NOTIFY_TITLE, sender=None, force=False) -> bool:
    """把一次失败喊出声；返回**是否真的发了**（单测就靠这个返回值断言）。

    `text` 是给通知栏看的短话（一句话 + 一个能跑的动作），完整话术仍旧走 stderr ——
    通知栏放不下五条排查建议。
    """
    if not (force or _notify_allowed()):
        return False
    key = (title, text)
    if key in _notified:
        return False
    _notified.add(key)
    try:
        (sender or _send_notification)(title, text)
    except Exception:                                            # noqa: BLE001
        # 通知这条路自己坏了，也不能把"报告失败"这个正事带崩（更不许让 capsplus 变非 0）
        pass
    return True


def timeout_report(out_path, waited_ms: int, journal_hint: str = "", what: str = "抓帧",
                   log_grep: str = "shot") -> str:
    """超时话术：**必须可操作**（用户讨厌被打断，给他能自己跑的一条命令，而不是"失败了"）。

    这里顺带**发一条桌面通知**（`notify_failure`）：用户是按 `Caps+F1` 触发的，那条路是 keyd
    起的进程，stdout/stderr 谁都不看 —— 只打一行 `✗ …` 等于什么都没发生，他只会再来一句
    "截图又没反应"。抓帧 / 贴图 / 清空贴图三条失败路径都过这个函数，所以通知挂在这里而不是
    调用方（挂在调用方会漏掉其中一条，也容易漏掉后来新加的那条）。
    """
    lines = [
        f"✗ {waited_ms / 1000:.1f}s 内没等到 {what} 的产物 {out_path}",
        "  按可能性排序的三条：",
        f"   1) 扩展没装/没启用：gnome-extensions list --enabled | grep {EXT_UUID}",
        "   2) 装了但 shell 里还是旧代码：改过 extension.js 后 `gnome-extensions "
        "disable/enable` **不会重新 import**（2026-10-19 实测），要注销重登一次",
        f"   3) 信号没人订/{what}抛了异常：{journal_hint or 'journalctl --user -n 200'} "
        f"| grep '{LOG_MARK} {log_grep}'（成功与失败都会留一行）",
    ]
    notify_failure(
        f"{what}没等到产物（{waited_ms / 1000:.1f}s）⇒ Caps 热键会没反应。"
        f"在仓目录跑 `sh tools/ext-bench.sh` 自检（不用重登）",
        title=f"Capslock+ {what}没成")     # 标题带上具体动作：贴图和抓帧是两条不同的排查路
    return "\n".join(lines)


def failure_report(out_path, records: list) -> str:
    """产物没出来、但 journal 里有 `shot result=fail` 时，把扩展那边的原因原样端出来。"""
    fails = [r for r in records if r.get("result") == "fail"]
    if not fails:
        return ""
    last = fails[-1]
    why = last.get("why", "(没写原因)")
    return (f"  扩展那边报的失败原因：{why}\n"
            f"  原始行：{last.get('raw', '')}")


# --------------------------------------------------------------- 选区几何（第 2 步用）

def normalize_rect(x0: int, y0: int, x1: int, y1: int) -> tuple:
    """橡皮筋两端 → `(x, y, w, h)`；**反向拖**（从右下往左上）也必须给出正数宽高。"""
    return (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))


def accept_rect(rect, min_size: int = MIN_SIZE) -> bool:
    """够不够大：手抖点一下（<3px）不该出图，否则会得到一张 1×1 的垃圾。"""
    return rect[2] >= min_size and rect[3] >= min_size


def clamp_rect(rect, screen) -> tuple:
    """把选区夹进屏幕范围（`screen` 是同序的 `(x, y, w, h)`）。

    不做这一步的话：跨屏拖拽松开时鼠标可能已经在屏幕外，抓帧会带一条黑边或整块失败。
    """
    sx, sy, sw, sh = screen
    x = max(sx, min(rect[0], sx + sw))
    y = max(sy, min(rect[1], sy + sh))
    x2 = max(sx, min(rect[0] + rect[2], sx + sw))
    y2 = max(sy, min(rect[1] + rect[3], sy + sh))
    return (x, y, max(0, x2 - x), max(0, y2 - y))


def union_rect(rects: list) -> tuple:
    """多块屏的包围盒：遮罩要盖住所有屏，抓帧的坐标系是**全局逻辑坐标**。"""
    if not rects:
        return (0, 0, 0, 0)
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return (x0, y0, x1 - x0, y1 - y0)


# --------------------------------------------------------------------- 落盘位置

def save_dir() -> Path:
    """默认存图目录。`CAPSPLUS_SHOT_DIR` 可覆盖（判据与演示靠它注入，别写死某人的家目录）。"""
    import os
    override = os.environ.get("CAPSPLUS_SHOT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Pictures" / "Screenshots"


def shot_filename(now=None, ext: str = ".png") -> str:
    """带时间戳的文件名（同一秒内连拍也不会撞：`unique_path` 负责避重）。"""
    now = now or __import__("datetime").datetime.now()
    return f"shot-{now:%Y-%m-%d-%H%M%S}{ext}"


def unique_path(directory, name: str) -> Path:
    """`a.png` 已存在就 `a-1.png`（老实说一句：**不覆盖**别人的图）。"""
    d = Path(directory)
    candidate = d / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for idx in range(1, 1000):
        candidate = d / f"{stem}-{idx}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"{d}/{name} 连 -999 都被占了")