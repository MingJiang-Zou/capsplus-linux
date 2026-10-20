# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""截图链路里**碰 gi** 的那一半：发信号、跑主循环、等产物、报错话术。

信号方向（2026-10-19 实测，别再试别的形态）：外部 emit_signal 五个实参 → shell 里的扩展
`signal_subscribe` 订阅执行 → 结果落 journal、产物落文件。**没有回复通道**，所以这里不装监听器、
不 try 解析回复，只等产物。

两个 PyGI 上的坑（都是实测，不是猜的）：
  * 本机 PyGI 没有 `Gio.DBus.session`，必须 `Gio.bus_get_sync(Gio.BusType.SESSION, None)`
    （仓里 `tools/verify-raise-browser.py` 头注也记了这条）；
  * `emit_signal` **只是入队**：得把 GLib 主循环真跑起来才送得出去。`time.sleep()` 送不到、
    `MainContext.iteration(False)` 空转也送不到，只有 `GLib.MainLoop()` + `timeout_add` 能送到。
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

from capsplus import shot_frame as SF


def build_connection():
    """session 总线连接（本机必须走 bus_get_sync）。"""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _glib():
    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
    return GLib


def pump(ms: int = 150) -> None:
    """把主循环真跑 ms 毫秒 —— emit_signal 入队后不跑主循环就出不了本进程（实测见头注）。"""
    GLib = _glib()
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, lambda: (loop.quit(), False)[1])
    loop.run()


def emit(conn, member: str, signature: str, payload: tuple) -> None:
    """发一发信号，然后 pump 一次（不 pump = 没发）。"""
    GLib = _glib()
    conn.emit_signal(None, SF.BUS_PATH, SF.BUS_IFACE, member, GLib.Variant(signature, payload))
    pump()


def journal_since() -> str:
    """给排查话术用的时间戳（`date -Is` 那种形态，journalctl 直接吃）。"""
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def journal_lines() -> str:
    """抓一眼 shell 那侧的痕迹；读不到就返回空串（**排查失败绝不让主流程崩**）。"""
    import subprocess
    try:
        proc = subprocess.run(SF.journal_command(journal_since()), capture_output=True,
                              text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def capture(x: int, y: int, w: int, h: int, out_path, *, timeout_ms: int = SF.CAPTURE_TIMEOUT_MS,
            emit_fn=None, wait_fn=None, verify_fn=None) -> tuple:
    """发 `CaptureArea` + 等产物。返回 `(ok, 一句话说明)`。

    三个注入点（`emit_fn` / `wait_fn` / `verify_fn`）就是为判据留的：单测里 stub 掉 D-Bus 与时钟，
    **没有真屏、没有扩展也能跑**，反例具型也能真的红。
    """
    out = Path(out_path).expanduser()
    # **转成绝对路径再交给 shell 扩展**（2026-10-20 实测踩到）：扩展是在 **gnome-shell 的进程里**
    # 写文件的，相对路径按**它自己的 cwd**（本机是 `~`）解析 —— 于是
    # `--capture 0 0 1400 900 --out A.png` 把图写到 `~/A.png`，而本进程还在原目录等 `A.png`，
    # 3 秒后报"没等到产物"：**明明抓成功了却说失败**（当时用绝对路径绕过，根因在这里）。
    # 只用 `abspath` 不用 `resolve()`：不追软链，免得把调用方给的路径改写掉。
    out = Path(os.path.abspath(str(out)))
    if w <= 0 or h <= 0:
        return False, f"✗ 选区不合法：{w}×{h}（宽高必须为正）"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"✗ 建不了目录 {out.parent}：{exc}"
    # 上一次的残骸先清掉：不然"文件在不在"这个判据永远为真，会拿旧图当新图。
    for stale in (out, Path(str(out) + ".part")):
        try:
            if stale.exists():
                stale.unlink()
        except OSError as exc:
            return False, f"✗ 清不掉旧产物 {stale}：{exc}"

    if emit_fn is None:
        emit_fn = lambda: emit(build_connection(), SF.M_CAPTURE, SF.CAPTURE_SIG,   # noqa: E731
                               (int(x), int(y), int(w), int(h), str(out)))
    emit_fn()
    ok, waited = (wait_fn or (lambda p, ms: SF.wait_for_file(p, ms)))(str(out), timeout_ms)
    if not ok:
        hint = SF.failure_report(str(out), SF.parse_shot_log(journal_lines()))
        return False, SF.timeout_report(str(out), waited) + (f"\n{hint}" if hint else "")
    check = (verify_fn or SF.is_complete_png)(str(out))
    if not check[0]:
        return False, f"✗ 产物不完整：{check[1]}"
    return True, f"✓ {out}（{check[1]}，等了 {waited}ms）"


def command_clear_pins(args, deps=None) -> int:
    """`Caps+Esc` 的活儿：清空所有贴图。

    为什么要它（2026-10-19 明江「贴出来的图 删都删不掉」）：贴图是 shell 的图层 actor，
    只有 shell 能销毁；而"点某张图上的 ×""右键菜单""Esc（拿到焦点后）"都在那张图上 ——
    万一哪条不好使，得有**不看光标在哪**的全局兜底。这条走的就是已有的 D-Bus 信号通道。
    """
    deps = deps or {}
    clear_fn = deps.get("clear")
    if clear_fn is None:
        from capsplus import shot_pin
        clear_fn = shot_pin.clear
    ok, msg = clear_fn()
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def command_region(args, deps=None) -> int:
    """先冻结帧 → 选区（冻结图当背景）→ **从冻结图裁剪** → 标注窗。

    `deps` 是给判据用的注入点（真跑时全走默认实现）：
      `monitors` 显示器几何、`capture` 抓帧、`select` 选区、`freeze` 整段冻结、`annotate` 标注、`pin` 钉屏。

    顺序不能改（2026-10-19 P0）：**抓帧必须在遮罩出现之前**。旧版反着来（先开遮罩、确认后再抓），
    于是遮罩只能靠"透明"露出桌面，Wayland 下直接全黑。
    """
    deps = deps or {}
    select = deps.get("select")
    capture_fn = deps.get("capture") or (lambda x, y, w, h, out: capture(
        x, y, w, h, out, timeout_ms=args.timeout_ms))
    annotate_fn = deps.get("annotate") or _default_annotate
    providers = deps.get("providers")
    if providers is None and not deps.get("no_text"):
        from capsplus import shot_text
        providers = shot_text.TextProviders()
    pin_fn = deps.get("pin")
    if pin_fn is None and not deps.get("no_pin"):
        from capsplus import shot_pin
        pin_fn = shot_pin.pin

    from capsplus import shot_frames as SFR
    monitors = deps.get("monitors")
    if monitors is None:
        monitors, _display = _monitor_geometry(deps)
    freeze_fn = deps.get("freeze") or (lambda mons: SFR.freeze(mons, capture_fn))
    try:
        return _region_flow(args, deps, monitors, freeze_fn, select, annotate_fn, providers, pin_fn,
                            SFR)
    finally:
        # **四条路都清**（成功 / 失败 / 异常 / 用户取消）：冻结帧在 tmpfs 上，不清就是吃内存的
        # 泄漏（lead 实测 41 个文件 108MB）。留最近几个给排障，见 `cleanup_runtime` 的注释。
        if not deps.get("keep_frames"):
            SFR.cleanup_runtime(keep=deps.get("keep") or SFR.KEEP_FRAMES)


def _region_flow(args, deps, monitors, freeze_fn, select, annotate_fn, providers, pin_fn, SFR) -> int:
    """`command_region` 的主体（外面那层负责 finally 清冻结帧）。"""
    frames, union, fmsg = freeze_fn(monitors)
    if not frames:
        print(f"✗ 先冻结再选这条链没走通：{fmsg}", file=sys.stderr)
        print("  没有冻结帧就不开遮罩 —— 那样只能靠透明露桌面，Wayland 下就是全黑盲选",
              file=sys.stderr)
        return 1
    print(fmsg)

    if select is None:
        def select(_frames, _mons):
            from capsplus import shot_overlay
            return shot_overlay.select_region(_frames, monitors=_mons)
    # 注入点一律 `select(frames, monitors)`：**冻结帧是选区的必需品**（没它就没背景），
    # 所以签名里必须有它 —— 判据注入的桩也照这个签名写。
    # 返回值：`(rect, failure)`；`failure` 是遮罩**自检没过**时的那句可操作的话
    # （2026-10-19：窗口尺寸没对上时宁可说"这次没成"，也不拿一张全黑的去截）。
    outcome = select(frames, monitors)
    rect, failure = outcome if isinstance(outcome, tuple) else (outcome, None)
    if failure:
        print(failure, file=sys.stderr)
        return 1
    if not rect:
        print("已取消（没抓图）")
        return 0

    out = Path(args.out) if args.out else SF.unique_path(SF.save_dir(), SF.shot_filename())
    try:
        surface = SFR.crop_from_frames(rect, frames)
    except ValueError as exc:
        print(f"✗ 裁剪失败：{exc}", file=sys.stderr)
        return 1
    from capsplus.shot_annotate import png_bytes
    Path(out).write_bytes(png_bytes(surface))
    print(f"✓ {out}（{Path(out).stat().st_size} 字节，{rect[2]}×{rect[3]}，从冻结图裁剪）")
    if args.no_annotate:
        return 0
    if args.no_annotate:
        return 0

    def on_pin(surface):
        """标注窗上点"钉到屏幕上"：先把渲染结果落成 PNG，再交给扩展钉（逻辑坐标居中偏上）。"""
        if pin_fn is None:
            return False, "✗ 没有钉屏实现（这条链路没接上）"
        from capsplus.shot_annotate import png_bytes
        tmp = Path(str(out)).with_name(Path(out).stem + ".pinned.png")
        tmp.write_bytes(png_bytes(surface))
        return pin_fn(tmp)

    result, saved, message = annotate_fn(str(out), on_pin, providers)
    if message:
        print(message)
    if saved:
        print(f"✓ 标注版已存 {saved}")
    return 0 if result in ("copy", "save", "pin", "close", None) else 1


def command_annotate(args, deps=None) -> int:
    """对一张已有的图开标注窗（第 3 步的独立入口，方便明江单独验这件事）。"""
    deps = deps or {}
    annotate_fn = deps.get("annotate") or _default_annotate
    pin_fn = deps.get("pin")
    if pin_fn is None and not deps.get("no_pin"):
        from capsplus import shot_pin
        pin_fn = shot_pin.pin

    def on_pin(surface):
        if pin_fn is None:
            return False, "✗ 没有钉屏实现（这条链路没接上）"
        from capsplus.shot_annotate import png_bytes
        tmp = Path(args.annotate).with_name(Path(args.annotate).stem + ".pinned.png")
        tmp.write_bytes(png_bytes(surface))
        return pin_fn(tmp)

    if not Path(args.annotate).exists():
        print(f"✗ 找不到 {args.annotate}", file=sys.stderr)
        return 2
    result, saved, message = annotate_fn(args.annotate, on_pin)
    if message:
        print(message)
    if saved:
        print(f"✓ 标注版已存 {saved}")
    return 0


def command_pin(args, deps=None) -> int:
    """贴图：把一张（或刚标注完的）图钉到屏幕上。"""
    deps = deps or {}
    pin_fn = deps.get("pin")
    if pin_fn is None:
        from capsplus import shot_pin
        pin_fn = shot_pin.pin
    x, y = (args.at or (None, None))
    ok, msg = pin_fn(args.pin, x, y)
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def _monitor_geometry(deps=None):
    """显示器几何（全局逻辑坐标）。判据注入假的；真跑走 GDK（与抓帧坐标同一套）。"""
    deps = deps or {}
    try:
        from capsplus import shot_overlay
        return shot_overlay.monitor_rects()
    except Exception:
        return [], None


def _default_select(frames, monitors):
    """真跑：起选区遮罩（冻结帧当背景），返回 `(矩形 | None, 失败说明 | None)`。"""
    from capsplus import shot_overlay
    return shot_overlay.select_region(frames, monitors=monitors)


def _default_annotate(png_path, on_pin, providers=None):
    from capsplus import shot_annotate
    return shot_annotate.annotate(png_path, on_pin=on_pin, providers=providers)


def parse_args(argv):
    """`--capture X Y W H --out FILE`。第二、三步会往这里加 `--region` / `--pin`。"""
    p = argparse.ArgumentParser(prog="capsplus-shot", add_help=True,
                               description="替代 Snipaste 的截图（抓帧走 GNOME 扩展，不闪）")
    p.add_argument("--capture", nargs=4, type=int, metavar=("X", "Y", "W", "H"),
                   help="抓一块矩形（逻辑坐标）")
    p.add_argument("--region", action="store_true",
                   help="拖一个选区（遮罩不闪）→ 抓帧 → 开标注窗")
    p.add_argument("--annotate", metavar="PNG", help="对一张已有的图开标注窗")
    p.add_argument("--pin", metavar="PNG", help="把一张图钉在屏幕上（真置顶）")
    p.add_argument("--clear-pins", action="store_true",
                   help="清空屏幕上所有贴图（`Caps+Esc` 全局兜底走这条；没有贴图时也安全）")
    p.add_argument("--clean", action="store_true",
                   help="清掉冻结帧（tmpfs 上的中间产物；默认留最近 2 个给排障）")
    p.add_argument("--keep", type=int, default=None, help="配 --clean：留几个（默认 2）")
    p.add_argument("--runtime-usage", action="store_true", help="看冻结帧目录现在多大、几个文件")
    p.add_argument("--ocr-status", action="store_true", help="看 OCR 两条后端现在什么状态")
    p.add_argument("--install-local-ocr", action="store_true",
                   help="一条命令装本地 OCR（下载 ~136MB、占盘 ~392MB、模型随包带；走镜像约 40 秒）")
    p.add_argument("--translate-status", action="store_true",
                   help="看翻译两条后端现在什么状态（本机模型起没起、云端有没有凭据）")
    p.add_argument("--at", nargs=2, type=int, metavar=("X", "Y"),
                   help="钉屏位置（默认主屏中间偏上）；逻辑坐标")
    p.add_argument("--no-annotate", action="store_true", help="--region 抓完不开标注窗")
    p.add_argument("--out", help="产物落点（默认给一个带时间戳的文件）")
    p.add_argument("--timeout-ms", type=int, default=SF.CAPTURE_TIMEOUT_MS,
                   help=f"等产物的超时（默认 {SF.CAPTURE_TIMEOUT_MS}ms）")
    p.add_argument("--journal", action="store_true",
                   help="打印 shell 那侧的抓帧痕迹（成功与失败都会留一行）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)

    if args.journal:
        for rec in SF.parse_shot_log(journal_lines()):
            print(rec.get("raw", ""))
        return 0

    if args.runtime_usage:
        from capsplus import shot_frames as SFR
        usage = SFR.runtime_usage()
        mb = usage["bytes"] / 1048576.0
        print(f"冻结帧目录：{SFR.runtime_dir()}\n  文件 {usage['files']} 个、占用 {mb:.1f} MB")
        return 0
    if args.clean:
        from capsplus import shot_frames as SFR
        keep = 2 if args.keep is None else max(0, args.keep)
        result = SFR.cleanup_runtime(keep=keep)
        freed = result["freed"] / 1048576.0
        print(f"✓ 清掉 {result['removed']} 个冻结帧、释放 {freed:.1f} MB，留下 {result['left']} 个")
        return 0
    if args.ocr_status:
        from capsplus import shot_ocr
        print(shot_ocr.describe())
        return 0
    if args.install_local_ocr:
        from capsplus import shot_ocr
        ok, msg = shot_ocr.install_local()
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if args.translate_status:
        from capsplus import shot_translate
        print(shot_translate.describe())
        return 0
    if args.clear_pins:
        return command_clear_pins(args)
    if args.region:
        return command_region(args)
    if args.annotate:
        return command_annotate(args)
    if args.pin:
        return command_pin(args)

    if not args.capture:
        print("用法：capsplus-shot --region | --capture X Y W H [--out FILE] | "
              "--annotate PNG | --pin PNG [--at X Y] | --clear-pins（--help 看全部）",
              file=sys.stderr)
        return 2

    x, y, w, h = args.capture
    out = Path(args.out) if args.out else SF.unique_path(SF.save_dir(), SF.shot_filename())
    ok, msg = capture(x, y, w, h, out, timeout_ms=args.timeout_ms)
    stream = sys.stdout if ok else sys.stderr
    print(msg, file=stream)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())