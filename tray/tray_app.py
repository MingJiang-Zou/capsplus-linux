#!/usr/bin/env python3
"""capsplus-tray —— GNOME(Wayland) 托盘：启停 keyd 映射、重载配置、唤起 Qbar。

用 GTK3 + AyatanaAppIndicator3（GNOME 侧靠 ubuntu-appindicators 扩展显示）。
所有外部命令都放线程里跑，主线程只用 GLib.idle_add 收结果 —— 托盘不许被命令卡住。
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(os.path.realpath(__file__)).parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

from capsplus import __version__, apply as cap_apply  # noqa: E402
from qbar.search_config import ensure_config_file, shell_quote  # noqa: E402

STATE_DIR = Path.home() / ".local/state/capsplus"
LOCK_PATH = STATE_DIR / "tray.lock"
ASSETS = REPO / "assets"
KEYD_CONF = cap_apply.KEYD_CONF
# keyd 以 root 运行，它要拉起 GUI 必须降权 + 带会话环境（明江已实测的那条包装串）
USER_ENV = ("runuser -u zmj -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 "
            "DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus")


def icon(name: str) -> str:
    for ext in (".svg", ".png"):
        path = ASSETS / (name + ext)
        if path.exists():
            return str(path)
    return "input-keyboard"


class Tray:
    def __init__(self):
        self.active = cap_apply.service_state() == "active"
        self.layer_note = ""

        self.indicator = AppIndicator.Indicator.new(
            "capsplus-tray", icon("capsplus-on" if self.active else "capsplus-off"),
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Capslock+ (keyd)")

        self.menu = Gtk.Menu()
        self.item_state = Gtk.MenuItem(label="读取中…")
        self.item_state.set_sensitive(False)
        self.menu.append(self.item_state)
        self.menu.append(Gtk.SeparatorMenuItem())

        self.entries = {}
        for label, handler in (
            ("打开 Qbar", self.on_qbar),
            ("编辑快速链接", self.on_edit_links),
            ("启用 / 暂停映射", self.on_toggle),
            ("重载配置", self.on_reload),
            ("打开映射文件", self.on_open_conf),
            ("查看最近日志", self.on_logs),
            ("关于", self.on_about),
            ("退出托盘", self.on_quit),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", handler)
            self.menu.append(item)
            self.entries[label] = item

        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.refresh_state()
        self.start_layer_listener()

    # ------------------------------------------------------------------
    # 后台执行 + 回主线程
    # ------------------------------------------------------------------
    def run_async(self, func, on_done=None):
        def worker():
            try:
                result = func()
            except Exception as exc:  # 托盘不许因为一条命令挂掉
                result = exc
            if on_done:
                GLib.idle_add(on_done, result)
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def shell(cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=30)

    def refresh_state(self):
        def worker():
            state = cap_apply.service_state()
            counts, conf, same = cap_apply.config_summary()
            return state, counts, self.layer_note

        def done(result):
            if isinstance(result, Exception):
                self.set_tooltip(f"读取状态失败: {result}")
                return False
            state, counts, layer = result
            self.active = state == "active"
            self.item_state.set_label("状态：映射已启用" if self.active else "状态：已暂停")
            self.entries["启用 / 暂停映射"].set_label("暂停映射" if self.active else "启用映射")
            self.indicator.set_icon_full(icon("capsplus-on" if self.active else "capsplus-off"),
                                         "capsplus")
            detail = "，".join(f"{s} {n} 条" for s, n in counts)
            self.set_tooltip(f"keyd: {state}" + (f"（{detail}）" if detail else "") +
                             (f"\n{layer}" if layer else ""))
            return False

        self.run_async(worker, done)

    def set_tooltip(self, text: str):
        self.indicator.set_title(text.replace("\n", " "))

    # ------------------------------------------------------------------
    # keyd listen：把 CAPS 层状态读出来（失败就静默降级）
    # ------------------------------------------------------------------
    def start_layer_listener(self):
        if not shutil.which("keyd.rvaiya"):
            return

        def worker():
            try:
                proc = subprocess.Popen(
                    ["sudo", "-n", "keyd.rvaiya", "listen"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError:
                return
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line or line.startswith("device added"):
                    continue
                GLib.idle_add(self.on_layer_line, line)

        threading.Thread(target=worker, daemon=True).start()

    def on_layer_line(self, line: str):
        # keyd listen 的行形如: "capl+alt: 1" / "capl: 0"
        name, _, value = line.partition(":")
        if "capl" not in name:
            return False
        self.layer_note = f"CAPS 层 {name.strip()}: {'激活' if value.strip() == '1' else '空闲'}"
        self.set_tooltip(self.layer_note)
        return False

    # ------------------------------------------------------------------
    # 菜单动作
    # ------------------------------------------------------------------
    def notify(self, text: str):
        """菜单首项临时当状态条用（GNOME 下 notify-send 可能没装）。"""
        self.item_state.set_label(text)

        def later():
            self.refresh_state()
            return False

        GLib.timeout_add_seconds(4, later)

    def on_toggle(self, _item):
        action = "stop" if self.active else "start"
        def worker():
            return self.shell(f"sudo -n systemctl {action} keyd")
        def done(result):
            if isinstance(result, Exception) or result.returncode != 0:
                self.notify(f"失败: {getattr(result, 'stderr', result)}".strip()[:80])
            else:
                self.notify("已暂停映射" if action == "stop" else "已启用映射")
            return False
        self.notify("执行中…")
        self.run_async(worker, done)

    def on_reload(self, _item):
        def worker():
            return cap_apply.apply()
        def done(result):
            if isinstance(result, Exception):
                self.notify(f"重载失败: {result}".replace("\n", " ")[:90])
            else:
                self.notify(f"重载成功（备份 {Path(result.backup).name if result.backup else '无'}）")
            return False
        self.notify("重载中…")
        self.run_async(worker, done)

    def on_qbar(self, _item):
        self.shell(f"{USER_ENV} capsplus-qbar")

    def on_edit_links(self, _item):
        """打开 Qbar 的快速链接配置；文件不在就先写一份默认的（带中文注释）。"""
        try:
            path = ensure_config_file()
        except OSError as exc:
            self.notify(f"写不了配置: {exc}"[:80])
            return
        self.notify("已打开快速链接配置")
        self.shell(f"xdg-open {shell_quote(str(path))}")

    def on_open_conf(self, _item):
        self.shell(f"xdg-open {KEYD_CONF}")

    def on_logs(self, _item):
        cmd = f"sudo -n journalctl -u keyd --no-pager -n 100; echo; echo '(按回车关闭)'; read"
        for term in ("ptyxis", "gnome-terminal", "kgx", "xterm"):
            if shutil.which(term):
                flag = "--" if term == "gnome-terminal" else "-e"
                self.shell(f'{term} {flag} bash -lc {shell_quote(cmd)}')
                return
        self.notify("没找到可用终端，请看 journalctl -u keyd")

    def on_about(self, _item):
        dialog = Gtk.AboutDialog()
        dialog.set_program_name("capsplus-tray")
        dialog.set_version(__version__)
        dialog.set_comments("Linux 版 Capslock+ 托盘：keyd 映射启停 / 重载 / Qbar 唤起")
        dialog.set_authors(["Echo（明江的专职代理）"])
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.show_all()

    def on_quit(self, _item):
        Gtk.main_quit()


def acquire_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main(_argv: list[str]) -> int:
    lock = acquire_lock()
    if lock is None:
        print("capsplus-tray 已在运行（tray.lock 被占用）")
        return 1
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
