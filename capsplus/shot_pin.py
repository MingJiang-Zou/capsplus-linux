# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""贴图 / 钉屏（第一批第 4 步）：把一张图交给扩展钉成**真置顶**的图层 actor。

为什么要绕这一道（2026-10-19 实测）：Wayland 下 GTK3 没有置顶 API，`set_keep_above` 是 X11 时代的
hint、在这台机上什么都不做。GNOME 里唯一"真的在所有窗口之上"的地方是 shell 的图层 actor
（`Main.layoutManager.addTopChrome`）⇒ 贴图只能由扩展建，capsplus 只负责把图准备好、把信号发出去。

两个实现口径：

  1. **PNG → RGBA 原图由 capsplus 解码**（PIL），扩展只做"读字节 + set_bytes"：
     GJS 侧没有现成的 PNG 解码器，把解码放这边少一整条会出错的路。
  2. **原图放 `$XDG_RUNTIME_DIR/capsplus-pin/`**（本机是 tmpfs）：它与 pin 的寿命天然对齐
     （注销就等于清空），也不用在"贴图成功后要不要删文件"上纠结 —— 不删，就不碰删除那条红线。
  3. **成功只认 ack 文件**（扩展建的，内容带 `count=`）：与抓帧等产物同一条口径，
     因为这条链路没有回复通道（详见 docs/SHOT-TOOL.md）。
"""

from __future__ import annotations

import os
from pathlib import Path

from capsplus import shot_frame as SF

PIN_TIMEOUT_MS = SF.CAPTURE_TIMEOUT_MS


def raw_dir() -> Path:
    """RGBA 原图的落点。优先 `$XDG_RUNTIME_DIR`（tmpfs，注销即清），退回家目录下的缓存。"""
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("CAPSPLUS_PIN_DIR")
    if not base:
        base = str(Path.home() / ".cache")
    return Path(base) / "capsplus-pin"


def to_raw_rgba(png_path, out_dir=None) -> tuple:
    """PNG → RGBA 原始字节文件；返回 `(Path, w, h)`。用 PIL 解码（本机车站得住）。"""
    from PIL import Image
    img = Image.open(str(png_path))
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    width, height = img.size
    directory = Path(out_dir) if out_dir else raw_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (Path(png_path).stem + ".rgba")
    target.write_bytes(img.tobytes())
    return target, width, height


def parse_ack(text: str) -> dict:
    """ack 文件内容 → dict（扩展写的是 `key=value` 行）。"""
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def default_position(monitors=None) -> tuple:
    """默认钉在主屏**中间偏上**（跟通知一个习惯位置），之后可以随便拖。"""
    if monitors is None:
        try:
            from capsplus import shot_overlay as OV
            monitors, _display = OV.monitor_rects()
        except Exception:
            monitors = []
    if not monitors:
        return (200, 200)
    x, y, w, h = monitors[0]
    return (x + max(0, w // 2 - 200), y + max(0, h // 4))


def shell_journal() -> str:
    """读 shell 那侧的痕迹（延迟 import：本模块的纯逻辑要能在没有 gi 的机器上单测）。"""
    from capsplus import shot_capture as SC
    return SC.journal_lines()


def pin(png_path, x: int = None, y: int = None, *, emit_fn=None, wait_fn=None,
        out_dir=None, ack_path=None) -> tuple:
    """把 `png_path` 钉到屏幕上；返回 `(ok, 说明)`。三个注入点给判据（不必有真屏/真扩展）。"""
    src = Path(png_path)
    if not src.exists():
        return False, f"✗ 找不到 {src}"
    if x is None or y is None:
        dx, dy = default_position()
        x = dx if x is None else x
        y = dy if y is None else y
    try:
        raw_path, width, height = to_raw_rgba(src, out_dir)
    except Exception as exc:                    # PIL 打不开的图：如实说，不假装
        return False, f"✗ 读不了 {src}：{exc}"
    # ack 默认也放 tmpfs：**不许**把 `.pin-ack` 落在他的 ~/Pictures/Screenshots 里（那是他的相册）
    ack = Path(ack_path) if ack_path else (raw_path.parent / (raw_path.stem + ".ack"))
    try:
        if ack.exists():
            ack.unlink()                        # 清残骸：不然"ack 在不在"永远为真
    except OSError as exc:
        return False, f"✗ 清不掉旧 ack {ack}：{exc}"

    if emit_fn is None:
        from capsplus import shot_capture as SC
        emit_fn = lambda: SC.emit(SC.build_connection(), SF.M_PIN, SF.PIN_SIG,   # noqa: E731
                                  SF.pin_signal_args(src, raw_path, width, height,
                                                     int(x), int(y), ack)[4])
    emit_fn()
    ok, waited = (wait_fn or (lambda p, ms: SF.wait_for_file(p, ms)))(str(ack), PIN_TIMEOUT_MS)
    if not ok:
        hint = SF.failure_report(str(ack), SF.parse_pin_log(shell_journal()))
        return False, SF.timeout_report(str(ack), waited, what="贴图",
                                        log_grep="pin") + (f"\n{hint}" if hint else "")
    info = parse_ack(ack.read_text(encoding="utf-8") if ack.exists() else "")
    return True, (f"✓ 已钉到 ({x}, {y})：{width}×{height}，屏幕上共 {info.get('count', '?')} 张"
                  f"（原图 {raw_path}，等了 {waited}ms）")


def clear(*, emit_fn=None, wait_fn=None, ack_path=None, timeout_ms: int = PIN_TIMEOUT_MS) -> tuple:
    """`Caps+Esc` 的活儿：清空屏幕上所有贴图。返回 `(ok, 说明)`。

    **没有贴图时也安全**（扩展侧只是清 0 张 + 写 ack 一行 `cleared=0`）——
    这条是硬口径：全局热键最怕"没东西时按一下就报错"，那会让用户不敢按。
    """
    ack = Path(ack_path) if ack_path else (raw_dir() / f"clear-{os.getpid()}.ack")
    ack.parent.mkdir(parents=True, exist_ok=True)
    try:
        if ack.exists():
            ack.unlink()
    except OSError as exc:
        return False, f"✗ 清不掉旧 ack {ack}：{exc}"

    if emit_fn is None:
        from capsplus import shot_capture as SC
        emit_fn = lambda: SC.emit(SC.build_connection(), SF.M_CLEAR, SF.CLEAR_SIG,   # noqa: E731
                                  SF.clear_signal_args(ack)[4])
    emit_fn()
    ok, waited = (wait_fn or (lambda p, ms: SF.wait_for_file(p, ms)))(str(ack), timeout_ms)
    if not ok:
        return False, SF.timeout_report(str(ack), waited, what="清空贴图", log_grep="pin")
    info = parse_ack(ack.read_text(encoding="utf-8") if ack.exists() else "")
    cleared = info.get("cleared", "?")
    return True, f"✓ 已清空贴图（清掉 {cleared} 张，屏幕上剩 {info.get('left', '?')} 张，等了 {waited}ms）"
