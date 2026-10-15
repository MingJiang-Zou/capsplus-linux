#!/usr/bin/env python3
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

import os
import socket
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from qbar.logic import (  # noqa: E402
    Entry, FileFinder, History, SearchEngine, browser_search_url, history_key,
    scan_desktop_files,
)

STATE_DIR = Path.home() / ".local/state/capsplus"
SOCK_PATH = STATE_DIR / "qbar.sock"
HISTORY_PATH = STATE_DIR / "qbar-history.json"
SEARCH_ROOTS = [Path.home() / "project", Path.home(), Path("/mnt/data"), Path("/mnt/back")]
DEBOUNCE_MS = 130
ENTRY_LIMIT = 40
PLACEHOLDER = "搜索应用 / 文件 / -> 命令 / = 算术 / 网址"

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


class QbarWindow(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_title("Qbar")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_default_size(680, 420)

        self.history = History(HISTORY_PATH)
        self.finder = FileFinder(SEARCH_ROOTS, max_entries=ENTRY_LIMIT, timeout_s=1.5)
        self.apps = scan_desktop_files(app_dirs())
        self.engine = SearchEngine(self.apps, self.history, self.finder)
        self.results: list[Entry] = []
        self._gen = 0
        self._debounce_id = 0

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
            self.view.append_column(Gtk.TreeViewColumn(title, renderer, text=idx))
        self.view.connect("row-activated", self._on_row_activated)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(340)
        scroll.add(self.view)
        box.pack_start(scroll, True, True, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)

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

    def _on_delete(self, *_args):
        self.hide()
        return True

    # ------------------------------------------------------------------
    # 打开
    # ------------------------------------------------------------------
    def _activate(self, entry: Entry):
        try:
            if entry.kind == "app":
                info = Gio.DesktopAppInfo.new_from_filename(entry.payload)
                if info is None:
                    raise RuntimeError("desktop 文件无法解析")
                info.launch(None, None)
            elif entry.kind in ("file", "dir"):
                Gio.AppInfo.launch_default_for_uri(Path(entry.payload).as_uri(), None)
            elif entry.kind in ("url", "hist"):
                if entry.kind == "hist" and entry.payload and not entry.payload.startswith("http"):
                    Gio.AppInfo.launch_default_for_uri(browser_search_url(entry.title), None)
                else:
                    Gio.AppInfo.launch_default_for_uri(entry.payload, None)
            elif entry.kind == "calc":
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(entry.payload, -1)
                self.entry.set_placeholder_text(f"已复制 {entry.payload}，关闭中…")
                GLib.timeout_add(700, self._hide_now)
                self.history.bump(history_key(entry.kind, entry.payload))
                return
            else:  # cmd / 其它
                Gio.AppInfo.launch_default_for_uri(browser_search_url(entry.title), None)
        except (GLib.Error, RuntimeError) as exc:
            print(f"打开失败 {entry.title}: {exc}", file=sys.stderr)
        self.history.bump(history_key(entry.kind, entry.payload or entry.title))
        self.hide()

    def _hide_now(self):
        self.hide()
        return False

    # ------------------------------------------------------------------
    def toggle(self):
        if self.get_visible():
            self.hide()
        else:
            self.entry.set_text("")
            self.entry.set_placeholder_text(PLACEHOLDER)
            self.show_all()
            self.present()
            self.entry.grab_focus()


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
    Gtk.main()
    SOCK_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
