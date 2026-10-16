#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""Qbar —— Capslock+ 的居中浮层启动器（GTK3）。

用法：capsplus-qbar            # 首次启动窗口；已在运行时切换显示/隐藏
      capsplus-qbar --hidden   # 起进程但不显示（常驻待唤）

设计要点：
  - 无边框、居中；Esc 关闭、↑/↓ 选择、Enter 打开、输入即搜
  - 单实例：Unix socket ~/.local/state/capsplus/qbar.sock，第二次启动只发一个字节切显示
  - 文件搜索走 worker 线程 + fdfind 超时（窗口永不卡住）
  - 打开动作只用 Gio.AppInfo / 剪贴板，不用 xdotool（Wayland 安全）
"""

from __future__ import annotations

import datetime
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from qbar.logic import (  # noqa: E402
    toggle_action, should_hide_on_focus_out,
    Entry, FileFinder, History, SearchEngine, browser_search_url,
    entry_history_payload, history_key,
    scan_desktop_files, settings_launch_command,
    window_width,
    raise_browser_signal, RAISE_SETTLE_US,
    RAISE_PROBE_MEMBER, RAISE_REPORT_MEMBER, parse_probe_report,
    ext_state_payload, should_settle, subscribe_probe_report,
)
from qbar.search_config import open_command  # noqa: E402

STATE_DIR = Path.home() / ".local/state/capsplus"
_BUS_CONN = None                      # 会话总线连接，懒取一次（见 _session_bus）
SOCK_PATH = STATE_DIR / "qbar.sock"
LOG_PATH = STATE_DIR / "qbar.log"
# CAPSPLUS_QBAR_DEBUG=1 时把每次按键/搜索条数也记进日志（排查"字没进去"用）
DEBUG = os.environ.get("CAPSPLUS_QBAR_DEBUG") == "1"


def log(msg: str) -> None:
    """轻量诊断日志（只留最近 200 行）。

    用途：排查"窗口开了但打不了字"这类看不见的问题 —— Wayland 下窗口拿到键盘焦点
    可能晚于 present()，光看进程活着没法判断。只记焦点/动作，不记用户敲的内容。
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        old = LOG_PATH.read_text(encoding="utf-8").splitlines() if LOG_PATH.exists() else []
        old.append(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}")
        LOG_PATH.write_text("\n".join(old[-200:]) + "\n", encoding="utf-8")
    except OSError:
        pass
HISTORY_PATH = STATE_DIR / "qbar-history.json"
SEARCH_ROOTS = [Path.home() / "project", Path.home(), Path("/mnt/data"), Path("/mnt/back")]
DEBOUNCE_MS = 130
ENTRY_LIMIT = 40
PLACEHOLDER = "搜网页（b / gh 关键词）/ 问 AI（qw 问题 = 复制+打开）/ 应用 / 文件 / -> 命令 / = 算术"

CSS = b"""
#qbarbox {
  background: rgba(28, 28, 32, 0.97);
  border: 1px solid rgba(255,255,255,0.18);
  border-radius: 10px;
}
#qbarsearch {
  background: transparent; color: #f2f2f2; border: none;
  border-bottom: 1px solid rgba(255,255,255,0.15);
  font-size: 16px; padding: 10px 12px;
}
"""


def app_dirs() -> list[Path]:
    dirs = [Path.home() / ".local/share/applications"]
    for part in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if part.strip():
            dirs.append(Path(part.strip()) / "applications")
    return dirs


def _session_bus():
    """会话总线连接（懒取一次）。PyGI 没有 GJS 那个 `Gio.DBus.session` 便捷对象。"""
    global _BUS_CONN
    if _BUS_CONN is None:
        _BUS_CONN = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return _BUS_CONN


class QbarWindow(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_title("Qbar")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_position(Gtk.WindowPosition.CENTER)
        # 宽度按屏幕算，别写死：长历史 URL / 提示文字会把 TreeView 的自然宽度顶爆（2026-10-16 实测超屏）
        _widths = []
        _disp = Gdk.Display.get_default()
        if _disp is not None:
            for _i in range(_disp.get_n_monitors()):
                _widths.append(_disp.get_monitor(_i).get_geometry().width)
        self.set_default_size(window_width(_widths), 420)

        self.history = History(HISTORY_PATH)
        self.finder = FileFinder(SEARCH_ROOTS, max_entries=ENTRY_LIMIT, timeout_s=1.5)
        self.apps = scan_desktop_files(app_dirs())
        self.engine = SearchEngine(self.apps, self.history, self.finder)
        self.results: list[Entry] = []
        self._gen = 0
        self._debounce_id = 0
        self._ext_alive = None        # None=还没问过 / True=应答过 / False=确认不在
        self._ext_sub = 0             # 信号订阅 id（本机 GJS/PyGI 都没有 signal_remove，留着不碍事）
        GLib.idle_add(self._start_ext_watch)   # 起来先问一次 Probe，别等用户按下才摸黑
        # 窗口这一轮显示的时刻（monotonic 秒）：失焦防抖要靠它算"显示多久了"
        self._shown_at = 0.0

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_name("qbarbox")
        self.add(box)

        self.entry = Gtk.SearchEntry()
        self.entry.set_name("qbarsearch")
        self.entry.set_placeholder_text(PLACEHOLDER)
        self.entry.connect("changed", self._on_changed)
        self.entry.connect("key-press-event", self._on_key)
        box.pack_start(self.entry, False, False, 0)

        self.store = Gtk.ListStore(str, str, str)  # 类型 / 标题 / 载荷
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_visible(False)
        for title, idx in (("类型", 0), ("标题", 1)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("xpad", 10)
            # 尾部省略：没有这两行，一条长历史 URL 就能让整扇窗横向撑到屏外
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            renderer.set_property("width-chars", 6 if idx == 0 else 24)
            renderer.set_property("max-width-chars", 6 if idx == 0 else 80)
            col = Gtk.TreeViewColumn(title, renderer, text=idx)
            if idx == 1:
                col.set_expand(True)          # 标题列吃满剩余宽度，而不是反过来要求窗口变宽
                col.set_min_width(260)
            self.view.append_column(col)
        self.view.connect("row-activated", self._on_row_activated)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(340)
        scroll.add(self.view)
        box.pack_start(scroll, True, True, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)
        # Wayland 下窗口拿到键盘焦点常晚于 present()；只抓一次可能落在"窗口还没激活"的空档，
        # 于是窗口开着却打不进字。map / focus-in 都补抓一次，再延时兜一刀。
        self.connect("map-event", self._on_map)
        self.connect("focus-in-event", self._on_focus_in)
        self.connect("focus-out-event", self._on_focus_out)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._do_search()

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def _on_changed(self, _entry):
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(DEBOUNCE_MS, self._do_search)

    def _do_search(self):
        self._debounce_id = 0
        text = self.entry.get_text().strip()
        outcome = self.engine.search(text)
        self.results = outcome.entries
        self._render()
        self._gen += 1
        if outcome.needs_files and self.finder.available():
            threading.Thread(target=self._files_worker, args=(text, self._gen), daemon=True).start()
        return False

    def _files_worker(self, text: str, gen: int):
        files = self.finder.run(text)
        if files:
            GLib.idle_add(self._on_files, files, gen, text)

    def _on_files(self, files: list[Entry], gen: int, text: str):
        if gen != self._gen or text != self.entry.get_text().strip():
            return False  # 过期结果（用户又打字了）直接丢
        self.results = self.engine.merge_files(self.results, files, text)
        self._render()
        return False

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _render(self):
        self.store.clear()
        for entry in self.results[:ENTRY_LIMIT]:
            self.store.append([entry.label, entry.title, entry.payload])
        if self.results:
            self.view.get_selection().select_path(Gtk.TreePath.new_first())

    def _selected(self) -> Entry | None:
        model, it = self.view.get_selection().get_selected()
        if it is None:
            return None
        index = model.get_path(it).get_indices()[0]
        return self.results[index] if 0 <= index < len(self.results) else None

    # ------------------------------------------------------------------
    # 键盘
    # ------------------------------------------------------------------
    def _on_key(self, _widget, event):
        key = event.keyval
        if DEBUG:
            log(f"key {Gdk.keyval_name(key)} widget={type(_widget).__name__}")
        if key == Gdk.KEY_Escape:
            self.hide()
            return True
        if key in (Gdk.KEY_Down, Gdk.KEY_Up):
            self._move(1 if key == Gdk.KEY_Down else -1)
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            entry = self._selected()
            if entry:
                self._activate(entry)
            return True
        return False

    def _move(self, delta: int):
        model, it = self.view.get_selection().get_selected()
        current = model.get_path(it).get_indices()[0] if it is not None else 0
        target = max(0, min(len(self.results) - 1, current + delta))
        path = Gtk.TreePath.new_from_indices([target])
        self.view.get_selection().select_path(path)
        self.view.scroll_to_cell(path, None, False, 0.0, 0.0)

    def _on_row_activated(self, _view, path, _column):
        index = path.get_indices()[0]
        if 0 <= index < len(self.results):
            self._activate(self.results[index])

    def _on_map(self, *_args):
        log("map")
        GLib.timeout_add(120, self._refocus)
        return False

    def _on_focus_in(self, *_args):
        log("focus-in")
        self.entry.grab_focus()
        return False

    def _refocus(self):
        self.entry.grab_focus()
        return False

    def _on_focus_out(self, *_args):
        """失焦 → 隐藏（既定口径：跟再按一次 Caps+Q 等效）。

        `[general] hide_on_focus_out = false` 时保持老行为（不隐藏，
        下次 Caps+Q 走 toggle_action 的 raise 拉回前台）。
        刚显示后 300ms 内的失焦不算"用户点了别处"，见 logic.should_hide_on_focus_out。
        """
        flag = getattr(self.engine.config, "hide_on_focus_out", True)
        elapsed_ms = int((time.monotonic() - self._shown_at) * 1000) if self._shown_at else 10 ** 9
        # **别**把 self.is_active 当"还带焦点"传进去：GTK 是在默认类处理器里才清掉 active 标记的，
        # 我们连的回调跑在它前面 —— 于是 focus-out-event 触发的那一刻 is_active 仍然是 True，
        # 传进去等于"永远不隐藏"，而且这条分支原先不打日志，就表现为"点别处没反应"（2026-10-15 用户实测）。
        # 既然信号叫 focus-out，焦点就是要走了，focused 一律按 False 处理。
        log(f"focus-out visible={self.get_visible()} is_active={self.is_active()} "
            f"距显示={elapsed_ms}ms flag={flag}")
        if should_hide_on_focus_out(self.get_visible(), False, elapsed_ms, flag):
            self.hide()
            log(f"hide(focus-out) 显示后 {elapsed_ms}ms")
        return False

    def _on_delete(self, *_args):
        self.hide()
        return True

    # ------------------------------------------------------------------
    # 打开
    # ------------------------------------------------------------------
    def _launch_context(self):
        """**每次打开东西都要带上激活上下文**（AppLaunchContext + 当前事件时间戳）。

        原先四处全传 `None` —— 等于对合成器/浏览器说"这不是一次用户主动启动"，
        于是默认浏览器（本机是 Edge）**另起一个窗口**而不是回到现有窗口开标签页，
        之后 Qbar 打开的页面全都堆在那个新窗口里；只有焦点恰好在本机上时看起来才"正常"
        （2026-10-16 用户实测）。Wayland 下的正当姿势是带时间戳的 launch context。
        """
        context = Gdk.Display.get_default().get_app_launch_context()
        context.set_timestamp(Gtk.get_current_event_time())
        return context

    def _activate(self, entry: Entry):
        log(f"activate kind={entry.kind} title={entry.title!r}")
        try:
            if entry.kind == "app":
                info = Gio.DesktopAppInfo.new_from_filename(entry.payload)
                if info is None:
                    raise RuntimeError("desktop 文件无法解析")
                info.launch(None, self._launch_context())
            elif entry.kind in ("file", "dir"):
                Gio.AppInfo.launch_default_for_uri(Path(entry.payload).as_uri(), self._launch_context())
            elif entry.kind == "exec":
                # 快速链接 type="app" 但目标带了参数 / 不是 .desktop 名 → 当命令跑
                self._run_command(entry.payload)
            elif entry.kind == "cfg":
                self._open_file(entry.payload)
            elif entry.kind == "url":
                self._open_url(entry.payload)
            elif entry.kind == "ask":
                # 「复制 + 打开」两条动作，**顺序是刻意的**：先落剪贴板再叫浏览器 ——
                # 反过来做的话浏览器一起来就抢走了焦点/时序，用户回来粘贴时可能还是旧内容。
                # 剪贴板放的是 `entry.clip` = 用户输入的**问题原文**（不是 URL、不是编码串）；
                # 打开的是 `entry.payload` = 目标网址。走 `_open_url` 而不是自己 launch，
                # 因为激活上下文（`_launch_context()`）在那一头，别在这儿绕过它。
                if entry.clip:
                    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(entry.clip, -1)
                    self.entry.set_placeholder_text("问题已复制，粘贴（Ctrl+V）后回车")
                else:
                    # 光敲了 trigger 没问题可给：别动剪贴板（把人家原来的内容洗掉更糟）
                    self.entry.set_placeholder_text("没问题可复制：先敲问题再回车")
                self._open_url(entry.payload)
            elif entry.kind == "hist":
                if entry.payload and not entry.payload.startswith("http"):
                    self._open_url(browser_search_url(entry.title, self.engine.config))
                else:
                    self._open_url(entry.payload)
            elif entry.kind == "calc":
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(entry.payload, -1)
                self.entry.set_placeholder_text(f"已复制 {entry.payload}，关闭中…")
                GLib.timeout_add(700, self._hide_now)
                self.history.bump(history_key(entry.kind, entry.payload))
                return
            else:  # cmd / 其它
                if not self._run_builtin(entry.payload):
                    self._open_url(browser_search_url(entry.title, self.engine.config))
        except (GLib.Error, RuntimeError) as exc:
            print(f"打开失败 {entry.title}: {exc}", file=sys.stderr)
        # `entry_history_payload`：ask 型要把「网址 + 问题原文」一起写进历史键，
        # 否则下次点这条只打得开网页、复制不出那个问题（其余类型行为不变）。
        self.history.bump(history_key(entry.kind, entry_history_payload(entry)))
        self.hide()

    def _run_builtin(self, payload: str) -> bool:
        """`-> date` / `-> time` / `-> settings` 这类能自己处理的命令：处理了返回 True。

        返回 False 的（`calc`/`q` 之类的补全提示）由调用方按老路子当搜索词开浏览器。
        """
        word = payload.strip()
        if word == "settings":
            cmd = settings_launch_command()
            if not cmd:
                self.entry.set_placeholder_text("没找到 capsplus-settings，先跑一次 ./install.sh")
                return True
            # 设置窗口是**另一个进程**：这里只负责把它拉起来，Qbar 自己收起
            threading.Thread(target=lambda: subprocess.run(cmd, capture_output=True),
                             daemon=True).start()
            GLib.timeout_add(150, self._hide_now)
            return True
        if word not in ("date", "time"):
            return False
        text = datetime.datetime.now().strftime("%Y-%m-%d" if word == "date" else "%H:%M:%S")
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.entry.set_placeholder_text(f"已复制 {text}，关闭中…")
        GLib.timeout_add(700, self._hide_now)
        return True

    def _run_command(self, command: str):
        """跑一条 shell 命令（快速链接 app 类的兜底路径）。放线程里，窗口不卡。"""
        if not command.strip():
            return
        threading.Thread(
            target=lambda: subprocess.run(["bash", "-lc", command], capture_output=True),
            daemon=True).start()


    def _open_file(self, path: str):
        """用系统默认程序打开一个本地文件（Qbar 的配置就靠它）。不存在就报错不崩。"""
        target = Path(path)
        if not target.exists():
            self.entry.set_placeholder_text(f"文件不存在：{target}")
            return
        Gio.AppInfo.launch_default_for_uri(target.as_uri(), self._launch_context())

    def _ext_state_path(self):
        """扩展应答的实测状态文件（给 `capsplus status` 与人看，别只写在进程记忆里）。"""
        return Path.home() / ".local/state/capsplus/raise-browser.json"

    def _write_ext_state(self, alive, info):
        try:
            p = self._ext_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = ext_state_payload(alive, info, time.time())
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(p)
        except OSError as exc:
            log(f"扩展状态写不了：{exc}")

    def _on_probe_report(self, _conn, _sender, _path, _iface, _member, params):
        """扩展回 ProbeReport ⇒ 它在跑。顺带把窗口数记一行，方便事后对账。"""
        text = params.unpack()[0] if params is not None else ""
        info = parse_probe_report(text)
        first = self._ext_alive is not True
        self._ext_alive = True
        if info.get("windows") is not None:
            log(f"扩展应答：windows={info['windows']} on-current={info['on_current']} "
                f"default={info['default']}")
        self._write_ext_state(True, info)
        if first:
            log("raise-browser 扩展在跑（打开网址前会先激活浏览器窗口）")

    def _start_ext_watch(self):
        """订阅扩展应答 + 问一次 Probe。装不上不影响别的（Qbar 正事是打开网址）。"""
        try:
            bus = _session_bus()
            self._ext_sub = subscribe_probe_report(bus, self._on_probe_report,
                                                   Gio.DBusSignalFlags.NONE)
            raise_browser_signal(bus, RAISE_PROBE_MEMBER)
            self._write_ext_state(None, {})
            log("已向扩展问 Probe（没应答就是扩展没加载：capsplus status 能看状态）")
        except Exception as exc:
            log(f"扩展应答订阅失败（不影响打开）：{type(exc).__name__} {exc}")

    def _raise_browser_window(self):
        """发一发 D-Bus 信号，请 GNOME 扩展把浏览器现有窗口激活（失败绝不拦下打开动作）。

        不激活的后果是用户实测到的：焦点不在浏览器时 Edge **另起一扇窗**，之后 Qbar 打开的
        页面全堆在那扇窗里。扩展那边只在**当前工作区**有浏览器窗口时才动手（不硬拽工作区）。

        为什么走**信号**而不是"导出对象 + 方法调用"：本机 GJS 实测
        `export_object_path` / `return_value` 这些便捷函数已被摘除，`register_object` 只剩
        原始 C 绑定（vtable 是 C 结构，JS 造不出来），而 `emit_signal` / `signal_subscribe`
        可用且跨实现跑通过（Python-GI 发、GJS 收）。信号没有回复通道 ⇒ 这里不假设成功，
        只等一帧（80ms）再打开；扩展没装时这一步没有任何副作用。
        """
        bus = None
        try:
            bus = _session_bus()
            raise_browser_signal(bus)
            raise_browser_signal(bus, RAISE_PROBE_MEMBER)   # 顺便问一下活着没/几扇窗
        except Exception as exc:                      # 连不上会话总线这类极端情况
            log(f"raise-browser 跳过：{type(exc).__name__} {exc}")
            self._ext_alive = False
            self._write_ext_state(False, {})
            return
        # 已知扩展不在就别白等；未知（第一次）与确认在，都等这一帧。
        if should_settle(self._ext_alive):
            GLib.usleep(RAISE_SETTLE_US)

    def _open_url(self, url: str):
        """开网址：配了 `general.browser` 就用那条命令，否则系统默认浏览器（xdg-open）。"""
        if not url:
            return
        if getattr(self.engine.config, "raise_browser", True):
            self._raise_browser_window()
        cmd = open_command(self.engine.config.browser, url)
        if cmd.startswith("xdg-open "):
            Gio.AppInfo.launch_default_for_uri(url, self._launch_context())
            return
        threading.Thread(
            target=lambda: subprocess.run(["bash", "-lc", cmd], capture_output=True),
            daemon=True).start()

    def _hide_now(self):
        self.hide()
        return False

    # ------------------------------------------------------------------
    def toggle(self):
        # is_active() 是**方法**不是属性：漏括号时它是个 bound method，永远为真 ——
        # 于是"显示但没焦点→raise"这一支从来没走过（2026-10-15 同一个坑的两处）
        action = toggle_action(self.get_visible(), self.is_active())
        if action == "hide":
            self.hide()
            log("hide")
        elif action == "raise":
            # 只是被别的窗口盖住了：拉回前台并重新抓焦点，别装死
            self.present()
            self.entry.grab_focus()
            log("raise")
        else:
            # 每次打开都重读 qbar.toml：改完快速链接不用重启 Qbar
            self.engine.reload_config()
            self.entry.set_text("")
            self.entry.set_placeholder_text(PLACEHOLDER)
            self.show_all()
            self.present()
            self.entry.grab_focus()
            self._shown_at = time.monotonic()    # 失焦防抖的时间基准
            log("show(toggle)")
            self._do_search()      # 文本本来就空时 "changed" 不触发，这里主动刷新一次


# ---------------------------------------------------------------------------
# 单实例：Unix socket
# ---------------------------------------------------------------------------
def try_toggle_existing() -> bool:
    """已有实例则发一个字节让它切显示，返回 True。"""
    if not SOCK_PATH.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(SOCK_PATH))
            sock.sendall(b"t")
        return True
    except OSError:
        SOCK_PATH.unlink(missing_ok=True)  # 陈旧 socket
        return False


def serve_socket(window: QbarWindow):
    if SOCK_PATH.exists():
        SOCK_PATH.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCK_PATH))
    server.listen(4)
    server.settimeout(0.5)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.recv(16)
                except OSError:
                    pass
            GLib.idle_add(window.toggle)

    threading.Thread(target=loop, daemon=True).start()


def _install_signal_handlers():
    """Ctrl+C / SIGTERM 也走正常退出路径，好把 socket 文件删掉（否则留个陈旧 socket）。"""
    import signal

    def quit_(_sig, _frame):
        GLib.idle_add(Gtk.main_quit)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, quit_)


def main(argv: list[str]) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _install_signal_handlers()
    if try_toggle_existing():
        return 0
    window = QbarWindow()
    serve_socket(window)
    window.show_all()
    if "--hidden" in argv:
        window.hide()
    else:
        # 冷启动（Caps+Q 首次唤起走的就是这条路）必须自己抢焦点：
        # 只 show_all() 的话窗口没有键盘焦点，用户敲的字会落到别的窗口里。
        window.present()
        window.entry.grab_focus()
        window._shown_at = time.monotonic()
        log("show(cold start)")
    Gtk.main()
    SOCK_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
