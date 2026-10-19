# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""冻结帧（第二批 P0）：**先抓帧、再选**。遮罩拿它当**不透明背景**，选区从它裁剪。

为什么必须这样（2026-10-19 明江真机反馈「框选的时候全黑屏是盲选」）：
旧做法是"全屏半透明窗 + 压暗 + 指望合成器把桌面透出来"。**Wayland 下拿不到 ARGB 表面时，
那层"透明"就是全黑** ⇒ 盲选。而这条在 xvfb 里永远复现不了（X11 的 ARGB 视觉好拿），
所以当时的像素判据全绿也拦不住。治本做法（Snipaste 也是这个思路）：

  1. 按热键后**立刻抓一张**（联合矩形，一次 D-Bus 往返）；
  2. 每块屏切出自己那一块冻结图，当作该屏遮罩窗口的**不透明背景** ——
     就算 alpha 一点都拿不到，用户看到的也是真实屏幕画面，**盲选从结构上不可能再发生**；
  3. 选区用**全局逻辑坐标**，可以跨屏；确认后**从冻结图裁剪**（跨屏就每屏各裁一块再拼），
     **不需要第二次抓帧** ⇒ 顺带解决"抓帧把自己拍进去"（那正是旧 settle 想遮的问题）。

`$XDG_RUNTIME_DIR/capsplus-shot/`：冻结帧与贴图原图的落点（tmpfs，注销即清）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# 「同一时刻只允许一个截图操作」是 shell 侧的硬限制：并发抓帧时后一个会直接失败
# （2026-10-19 真机实测：`why=Only one screenshot operation at a time is permitted`）。
BUSY_MARKER = "Only one screenshot operation"
BUSY_RETRIES = 4
BUSY_DELAY_MS = 200
SNAP_PX = 8                     # 贴边吸附的容差


def runtime_dir() -> Path:
    """冻结帧/原图的落点：优先 XDG_RUNTIME_DIR（tmpfs），否则退回家目录缓存。"""
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    return Path(base) / "capsplus-shot"


KEEP_FRAMES = 2                     # 留最近几个给排障（其余立刻清）


def cleanup_runtime(keep: int = KEEP_FRAMES, out_dir=None) -> dict:
    """清掉冻结帧，只留最近 `keep` 个。返回 `{"removed": n, "freed": bytes, "left": n}`。

    ⚠ 2026-10-19 真机事故（lead 量的）：`~/.dsh/.../capsplus-shot/` **41 个文件、108MB** —— 而且
    它在 `$XDG_RUNTIME_DIR`（`/run/user/<uid>/`）下，是 **tmpfs，吃的是内存**（每张 6400×1440 PNG ≈ 3.4MB）。截图流程
    跑完不删，按一次攒一张，按几百次就把内存吃掉一块。所以：**流程结束即清**（成功/失败/异常/取消
    四条路都清），只留最近 `keep` 个用于排障，另给 `--clean` 手动清。

    为什么不按"删除一律 mv 进焚化炉"的老规矩走：焚化炉（`/tmp`）**也是 tmpfs** —— 把 108MB 挪过去
    等于没释放（而且和这里的目的是矛盾的）。这些冻结帧是**一次性中间产物**（每次抓都有新的、
    用户要留的图早就存到 `~/Pictures/Screenshots/` 了），所以直接 `unlink`，并在注释里写明这条例外。
    """
    out_dir = Path(out_dir) if out_dir else runtime_dir()
    if not out_dir.is_dir():
        return {"removed": 0, "freed": 0, "left": 0}
    files = sorted((f for f in out_dir.iterdir() if f.is_file()),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    removed = freed = 0
    for old_file in files[max(0, int(keep)):]:
        try:
            size = old_file.stat().st_size
            old_file.unlink()
            removed += 1
            freed += size
        except OSError:
            pass
    return {"removed": removed, "freed": freed, "left": len(list(out_dir.iterdir()))}


def runtime_usage(out_dir=None) -> dict:
    """看一眼现状（`--clean`/体检脚本用）。"""
    out_dir = Path(out_dir) if out_dir else runtime_dir()
    if not out_dir.is_dir():
        return {"files": 0, "bytes": 0, "oldest": None, "newest": None}
    files = [f for f in out_dir.iterdir() if f.is_file()]
    times = [f.stat().st_mtime for f in files] or [0]
    return {"files": len(files), "bytes": sum(f.stat().st_size for f in files),
            "oldest": min(times), "newest": max(times)}


class Frame:
    """一块屏的冻结图：`rect` 是全局逻辑矩形，`surface` 是像素图。"""

    def __init__(self, rect, surface, scale: float = 1.0):
        self.rect = tuple(int(v) for v in rect)
        self.surface = surface
        self.scale = float(scale)

    @property
    def pixel_size(self) -> tuple:
        return (self.surface.get_width(), self.surface.get_height())

    def __repr__(self):
        return f"Frame(rect={self.rect}, px={self.pixel_size}, scale={self.scale})"


def derive_scale(union_rect, surface) -> float:
    """从抓回来的图推"逻辑 → 像素"的一致缩放（本机实测 = 1）。

    不硬编码、也不问 shell 要：**看到多大的图就是多大**。
    """
    w = union_rect[2]
    return (surface.get_width() / w) if w else 1.0


def slice_frames(union_surface, monitors, scale: float) -> list:
    """按每块屏在联合图里的位置，切出各自的冻结图。

    跨屏选区就是靠这些碎片拼出来的（每屏一块，互不干扰）。
    屏矩形先夹到联合矩形里，避免算到图外面去。
    """
    cairo = __import__("cairo")
    ux, uy, uw, uh = union_rect(monitors)
    frames = []
    for rect in monitors:
        x = max(rect[0], ux)
        y = max(rect[1], uy)
        w = min(rect[0] + rect[2], ux + uw) - x
        h = min(rect[1] + rect[3], uy + uh) - y
        if w <= 0 or h <= 0:
            continue
        pw = max(1, int(round(w * scale)))
        ph = max(1, int(round(h * scale)))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
        cr = cairo.Context(surface)
        cr.set_source_surface(union_surface, -(x - ux) * scale, -(y - uy) * scale)
        cr.paint()
        frames.append(Frame(rect, surface, scale))
    return frames


def union_rect(monitors) -> tuple:
    """所有屏的包围盒（全局逻辑坐标）。"""
    if not monitors:
        return (0, 0, 0, 0)
    x0 = min(m[0] for m in monitors)
    y0 = min(m[1] for m in monitors)
    x1 = max(m[0] + m[2] for m in monitors)
    y1 = max(m[1] + m[3] for m in monitors)
    return (x0, y0, x1 - x0, y1 - y0)


def frame_at(frames, x: int, y: int):
    """点在哪块屏的冻结图上。"""
    for frame in frames:
        fx, fy, fw, fh = frame.rect
        if fx <= x < fx + fw and fy <= y < fy + fh:
            return frame
    return None


def freeze(monitors, capture_fn, out_dir=None, retries: int = BUSY_RETRIES,
           sleep=time.sleep) -> tuple:
    """抓一张联合图 → 切成每屏一块。返回 `(frames, union_rect, 说明)`；失败时 frames 为空。

    `capture_fn(x, y, w, h, path) -> (ok, msg)`（注入点：判据用假的，真跑走 D-Bus 那条）。
    「同一时刻只允许一个截图操作」是**会发生的正常情况**（用户自己连着按、或热键连击），
    所以这里按标记重试几次，而不是一次失败就告诉用户"抓不了"。
    """
    union = union_rect(monitors)
    if union[2] <= 0 or union[3] <= 0:
        return [], union, "✗ 读不到显示器几何（没有屏幕？）"
    out_dir = Path(out_dir) if out_dir else runtime_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"freeze-{os.getpid()}-{int(time.time() * 1000)}.png"
    attempts = max(1, int(retries))
    last = ""
    for attempt in range(attempts):
        ok, msg = capture_fn(union[0], union[1], union[2], union[3], str(path))
        if ok:
            break
        last = msg
        if BUSY_MARKER in msg and attempt + 1 < attempts:
            sleep(BUSY_DELAY_MS / 1000.0)
            continue
        return [], union, msg
    else:                                   # pragma: no cover - 循环里已经 return
        return [], union, last
    cairo = __import__("cairo")
    surface = cairo.ImageSurface.create_from_png(str(path))
    scale = derive_scale(union, surface)
    frames = slice_frames(surface, monitors, scale)
    # **尽早放掉联合大图**（性能体检 B2）：切片是各自独立的 surface（`slice_frames` 里
    # `set_source_surface` + `paint` 拷了一份），所以这里 `finish()` 是安全的 ——
    # 6400×1440 那张解出来就 35 MB，早点还回去，别和切片一起占着（峰值能少一半）。
    try:
        surface.finish()
    except Exception:
        pass
    return frames, union, f"✓ 冻结 {union[2]}×{union[3]} @{scale:g}x"


def crop_from_frames(rect, frames):
    """按全局逻辑矩形从冻结图裁剪；跨屏时每屏各裁一块拼起来。**不透明，不碰 alpha。**"""
    cairo = __import__("cairo")
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        raise ValueError(f"选区不合法：{rect}")
    scale = frames[0].scale if frames else 1.0
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(round(w * scale))),
                             max(1, int(round(h * scale))))
    cr = cairo.Context(out)
    covered = 0
    for frame in frames:
        fx, fy, fw, fh = frame.rect
        ix, iy = max(x, fx), max(y, fy)
        ix2, iy2 = min(x + w, fx + fw), min(y + h, fy + fh)
        if ix2 <= ix or iy2 <= iy:
            continue                      # 这块屏与选区不相交
        sx = (ix - fx) * frame.scale      # 冻结图里对应的像素位置
        sy = (iy - fy) * frame.scale
        cr.set_source_surface(frame.surface, -sx + (ix - x) * scale, -sy + (iy - y) * scale)
        cr.rectangle((ix - x) * scale, (iy - y) * scale, (ix2 - ix) * scale, (iy2 - iy) * scale)
        cr.fill()
        covered += 1
    if covered == 0:
        raise ValueError(f"选区 {rect} 不落在任何冻结屏上")
    return out


def snap_rect(rect, monitors, tolerance: int = SNAP_PX) -> tuple:
    """贴边吸附：离某块屏的边（或整屏边界）在容差内就吸上去。

    这是"全屏选择要能一拖就到"的那一半（另一半是 Ctrl+A 一键整屏）。
    """
    if not rect or rect[2] <= 0 or rect[3] <= 0:
        return rect
    ux, uy, uw, uh = union_rect(monitors)
    x, y, w, h = rect
    # 候选边**排序**后再试：set 的迭代顺序不定，同样的输入可能吸到不同的边 ——
    # 那种"有时吸有时不吸"的行为比不吸更糟（判据也没法钉）。
    lefts = sorted({ux} | {m[0] for m in monitors})
    rights = sorted({ux + uw} | {m[0] + m[2] for m in monitors})
    tops = sorted({uy} | {m[1] for m in monitors})
    bottoms = sorted({uy + uh} | {m[1] + m[3] for m in monitors})
    for edge in lefts:
        if abs(x - edge) <= tolerance:
            w += x - edge
            x = edge
            break
    for edge in rights:
        if abs(x + w - edge) <= tolerance:
            w = edge - x
            break
    for edge in tops:
        if abs(y - edge) <= tolerance:
            h += y - edge
            y = edge
            break
    for edge in bottoms:
        if abs(y + h - edge) <= tolerance:
            h = edge - y
            break
    return (x, y, max(1, w), max(1, h))


def union_hit(monitors, x: int, y: int):
    """点在不在任何一块屏上（跨屏拖拽时用来判断"还在屏幕上"）。"""
    ux, uy, uw, uh = union_rect(monitors)
    return ux <= x < ux + uw and uy <= y < uy + uh