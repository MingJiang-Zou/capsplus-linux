#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""重新生成 assets/screenshots/*.png（README 顶部那三张图就是这么来的）。

为什么要在仓库里带这个脚本，而不是只放三张图：截图内容跟着界面走，改了界面就该能一条命令重拍；
同时把「拍之前先脱敏」这件事固化在代码里，不靠人记得。

三条硬规矩（2026-10-16 实测踩出来的，别再踩）：
  1. **绝不在真实 Wayland 桌面上拍** —— 会弹到用户屏幕上并抢键盘焦点。脚本先看 GDK 实际后端，
     是 Wayland 就拒跑，除非显式 `--allow-wayland`。正解是虚拟显示：
       xvfb-run -a --server-args="-screen 0 1024x768x24" \
         env -u WAYLAND_DISPLAY GDK_BACKEND=x11 python3 tools/make-screenshots.py
     （只 `-u WAYLAND_DISPLAY` 不够：libwayland 会退回连 `$XDG_RUNTIME_DIR/wayland-0`。）
  2. **路径不能把私人家目录画进像素** —— 状态栏会原样显示配置文件路径，所以一律用相对路径；
     表格里 `q` 那行动作含 `command(<bindir>/keyd-qbar-cmd)`，用 `CAPSPLUS_BINDIR=/opt/demo-bin`
     渲染成中性目录。PNG 里的字可不会被「grep 个人路径」的判据抓到。
  3. 界面数据来自 `assets/screenshots/demo-qbar.toml`（五条 example.com 假链接），
     **不要**拿真人那份 `~/.config/capsplus/qbar.toml` 拍。
"""
import argparse
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "assets" / "screenshots"
DEMO_QBAR = OUT_DIR / "demo-qbar.toml"
NEUTRAL_BINDIR = "/opt/demo-bin"
PAGES = ("links", "general", "keys")


def refuse_real_desktop(backend: str, allow_wayland: bool) -> str:
    """纯函数：后端是真实 Wayland 且没授权就给出拒跑理由（判据也测这个，不需要显示服务器）。"""
    if "Wayland" in backend and not allow_wayland:
        return (f"当前 GDK 后端是 {backend} —— 这是真实桌面，截图会把窗口弹到用户屏幕上。"
                "请用 xvfb-run + GDK_BACKEND=x11，或确实要拍真桌面时加 --allow-wayland。")
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重拍 README 用的三张界面截图")
    ap.add_argument("--allow-wayland", action="store_true", help="明确允许在真实 Wayland 桌面上拍（会弹窗）")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录（默认 assets/screenshots）")
    args = ap.parse_args(argv)

    os.environ.setdefault("CAPSPLUS_GUI", "1")          # 界面测试/起窗的显式开关
    os.environ.setdefault("CAPSPLUS_BINDIR", NEUTRAL_BINDIR)
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "settings"))
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk
    if not Gtk.init_check()[0]:
        print("连不上任何显示服务器：请用 xvfb-run 跑本脚本", file=sys.stderr)
        return 2
    display = Gdk.Display.get_default()
    reason = refuse_real_desktop(type(display).__name__, args.allow_wayland)
    if reason:
        print(reason, file=sys.stderr)
        return 3
    if not DEMO_QBAR.exists():
        print(f"缺少演示配置：{DEMO_QBAR}", file=sys.stderr)
        return 2

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capsplus-shot-") as td:
        state = pathlib.Path(td) / "state"
        state.mkdir()
        qbar = pathlib.Path(td) / "demo-qbar.toml"
        qbar.write_text(DEMO_QBAR.read_text(encoding="utf-8"), encoding="utf-8")
        overrides = state / "mappings.toml"
        os.chdir(td)                                     # 相对路径 → 状态栏里不会出现私人家目录
        import settings_app as SA
        win = SA.SettingsWindow(qbar_path=pathlib.Path("demo-qbar.toml"),
                                overrides_path=pathlib.Path("state", "mappings.toml"),
                                state_dir=pathlib.Path("state"))
        win.set_default_size(960, 680)
        win.show_all()

        def pump(seconds=0.4):
            end = __import__("time").monotonic() + seconds
            while __import__("time").monotonic() < end:
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                __import__("time").sleep(0.02)

        def find(widget, typ):
            if isinstance(widget, typ):
                return widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    hit = find(child, typ)
                    if hit is not None:
                        return hit
            return None

        pump(0.6)
        notebook = find(win, Gtk.Notebook)
        for index, name in enumerate(PAGES):
            notebook.set_current_page(index)
            pump(0.5)
            win.check_resize()
            pump(0.3)
            if name == "keys":                            # 选一行让编辑条有内容（README 要说明的就是它）
                page = win.mapping_page
                page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([6]))
                pump(0.3)
                page.sync_editor()
                pump(0.3)
            win.queue_draw()
            pump(0.4)
            alloc = win.get_allocation()
            import cairo
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, alloc.width, alloc.height)
            win.draw(cairo.Context(surface))
            surface.flush()
            data = surface.get_data()
            colors = len({bytes(data[k:k + 4]) for k in range(0, len(data), 4)})
            if colors < 50:
                print(f"✗ {name}.png 只有 {colors} 种颜色 = 空图（尺寸分配没跑完）", file=sys.stderr)
                return 4
            target = out / f"{name}.png"
            surface.write_to_png(str(target))
            print(f"  {target}  {target.stat().st_size} 字节  颜色 {colors}")
        win.destroy()
        pump(0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
