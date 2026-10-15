#!/usr/bin/env python3
"""capsplus-settings —— Capslock+ 的设置窗口（GTK3，中文，三页）。

    ① 快速链接   增/删/上下移动 + 四种类型 + 实时预览，写回 `qbar.toml`
    ② 通用       default_search / browser / hide_on_focus_out
    ③ 按键映射   当前生效表（层/键/动作/说明/来源），改动写覆盖层 `mappings.toml`，
                 「应用」走 render_with_overrides() + 现有 apply 流程（出错自动回滚）

分工（**这个文件只摆控件**）：校验、覆盖层增删、宏事件计数、预览拼装、TOML 写回、
apply 流程全在 `capsplus/settings_logic.py` 与 `qbar/search_config.py`，那两份不 import gi、有单测。

单实例：Unix socket `~/.local/state/capsplus/settings.sock`。第二次启动（含 Qbar 的
`-> settings`）只发一个字节，让已经开着的窗口回到前台 —— 开两个窗口各自覆盖对方的改动，
是最容易丢配置的那种事故。

入口只有三个：托盘「设置…」、Qbar `-> settings`、`~/.local/bin/capsplus-settings`。
刻意**没有** .desktop 应用菜单条目（2026-10-15 明江嫌应用里条目多，上一轮刚删掉一条）。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

REPO = Path(os.path.realpath(__file__)).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")   # 必须显式要：漏了就会默认加载 Gdk 4.0，
                                   # `from gi.repository import Gdk` 当场 ImportError ——
                                   # 而且**只有真跑入口才暴露**（测试进程自己先 require 过 3.0，
                                   # 235 条测试全绿照样打不开界面，2026-10-15 就是这么翻车的）
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from capsplus import mapping_overrides as MO  # noqa: E402
from capsplus import settings_logic as SL  # noqa: E402
from capsplus.render import binding_rows, render  # noqa: E402
from qbar import search_config as SC  # noqa: E402

STATE_DIR = Path.home() / ".local/state/capsplus"
SOCK_PATH = STATE_DIR / "settings.sock"
GREY = "#9aa0a6"
RED = "#e64545"
AMBER = "#c98a00"
BLUE = "#1a5fb4"
BLACK = "#000000"

# 第②页每行右侧的灰字解释（集中放这儿，别在摆控件的地方散落三份）
GENERAL_HELP = {
    "default_search": "不带前缀直接输入时，用哪个引擎搜（下拉只列 search 类型的链接）",
    "browser": "留空 = 系统默认浏览器（xdg-open）；填命令模板，{url} 会被换成真实地址",
    "hide_on_focus_out": "点别处就收起 Qbar（等价于再按一次 Caps+Q，下次按一下就能唤起）",
}

VERB = {"search": "浏览器搜", "url": "浏览器开", "app": "启动", "file": "打开"}


def markup(text: str, color: str = "") -> str:
    """灰/红/黄字都走 pango markup；转义交给 GLib（要显示的都是用户自己输入的文本）。"""
    escaped = GLib.markup_escape_text(text)
    return f'<span foreground="{color}">{escaped}</span>' if color else escaped




def _open_dropdown_windows() -> list:
    """当前**展开着**的下拉窗口（组合框 popup / 下拉菜单）。

    用它判断"屏幕上是否真有一个悬浮的下拉"，比猜编辑状态可靠 —— Wayland 下这个下拉是
    独立 surface，type hint 是 COMBO / DROPDOWN_MENU。
    （坑：`Gdk.Window.get_window_type()` 返回的是实现种类 TOPLEVEL/TEMP，不是提示种类，
    得用 `Gtk.Window.get_type_hint()`。）
    """
    kinds = (Gdk.WindowTypeHint.COMBO, Gdk.WindowTypeHint.DROPDOWN_MENU,
             Gdk.WindowTypeHint.POPUP_MENU)
    found = []
    for w in Gtk.Window.list_toplevels():
        try:
            if not (w.get_realized() and w.is_visible()):
                continue
            if w.get_type_hint() in kinds:      # 注意：用 type hint，不是 get_window_type()
                found.append(w)
        except (AttributeError, TypeError, RuntimeError):
            continue              # 正在销毁的 toplevel 会炸，跳过就算
    return found


def keep_dropdown_attached_to_scroll(view, scroll, renderer) -> None:
    """Wayland 下表格里的下拉（`CellRendererCombo`）是**独立 surface**：表格滚动时它不跟着走，
    就悬浮在原来的位置上（2026-10-15 明江实测反馈的 bug）。

    GTK3 在这个构建里既不暴露 `editing-started` 信号（实测 `GObject.signal_lookup` 查不到），
    也没有"popup 跟随父窗口"的机制，所以收口是：**一旦滚动就结束这个格子的编辑会话**
    （下拉随之销毁；格子和内容都还在，再点一次重新展开）。
    只在"确实有下拉展开"时才动手 —— 否则会把用户正在敲的普通文本格也一起打断。

    `view.dropdown_closes` 计数给单测断言用（证明"滚一下真收了"，不靠人眼）。
    """
    view.dropdown_closes = 0

    def _scrolled(_adj):
        if not _open_dropdown_windows():
            return                       # 没有悬浮下拉：别打扰正在输入的文本格
        try:
            renderer.stop_editing(True)
        except (AttributeError, TypeError):
            return
        view.dropdown_closes += 1

    for adj in (scroll.get_vadjustment(), scroll.get_hadjustment()):
        if adj is not None:
            adj.connect("value-changed", _scrolled)


# ===========================================================================
# 窗口
# ===========================================================================
class SettingsWindow(Gtk.Window):
    def __init__(self, qbar_path: Path | None = None, overrides_path: Path | None = None,
                 state_dir: Path | None = None):
        super().__init__(title="Capslock+ 设置")
        self.set_default_size(960, 680)

        self.qbar_path = Path(qbar_path) if qbar_path else SC.CONFIG_PATH
        self.overrides_path = Path(overrides_path) if overrides_path else MO.OVERRIDES_PATH
        self.state_dir = Path(state_dir) if state_dir else STATE_DIR
        self.backup_dir = self.state_dir / "config-backup"
        # keyd 的键名表；取不到 = None = 不校验键名（没装 keyd 的机器也要能开界面）
        self.known_keys = MO.load_known_keys()

        # 两份配置各存一份内存态；界面所有改动只动内存，点保存才写盘
        self.cfg = SC.load_config(self.qbar_path)
        self.ov = MO.load_overrides(self.overrides_path, self.known_keys)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.notebook = Gtk.Notebook()
        box.pack_start(self.notebook, True, True, 0)
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        self.statusbar = Gtk.Label(xalign=0)
        self.statusbar.set_line_wrap(True)
        self.statusbar.set_padding(6, 10)
        self.statusbar.set_selectable(True)        # 报错文本要能复制出来问我
        box.pack_start(self.statusbar, False, False, 0)
        self.add(box)

        self.links_page = LinksPage(self)
        self.general_page = GeneralPage(self)
        self.mapping_page = MappingPage(self)
        for title, page in (("快速链接", self.links_page), ("通用", self.general_page),
                            ("按键映射", self.mapping_page)):
            self.notebook.append_page(page, Gtk.Label(label=title))
        self.notebook.set_current_page(0)

        # 状态栏就绪之后才能报读取期发现的问题（放最开头会撞在未创建的控件上）
        if self.ov.error:
            self.status(f"覆盖层读不了，已按「没有覆盖层」处理：{self.ov.error}", RED)
        elif self.ov.errors:
            self.status("覆盖层里有几条被跳过（没生效）：\n  " + "\n  ".join(self.ov.errors), AMBER)
        elif self.ov.count:
            self.status(f"覆盖层现有 {self.ov.count} 条改动（{self.ov.path}）。", GREY)
        else:
            self.status(f"配置：{self.qbar_path} ／ 覆盖层：{self.overrides_path}", GREY)

        self.connect("delete-event", self._on_delete)

    # ------------------------------------------------------------------
    def status(self, text: str, color: str = GREY) -> None:
        """三页共用一条状态栏：红=拦住了、黄=提醒、灰=说明、蓝=写成了。"""
        self.statusbar.set_markup(markup(text or "", color))
        self.statusbar.set_tooltip_text(text or "")

    def focus_self(self) -> bool:
        self.deiconify()
        self.present()
        self.status("已经开着了 —— 这是同一个设置窗口。", GREY)
        return False

    def _on_delete(self, *_args):
        self.destroy()
        Gtk.main_quit()
        return True

    def refresh_all(self) -> None:
        """改完一处三页都跟着变（第②页的下拉候选就是第①页的链接表）。"""
        self.links_page.reload_from_memory()
        self.general_page.reload_from_memory()
        self.mapping_page.reload_from_memory()

    # ------------------------------------------------------------------
    # qbar.toml：第①②页共用这两个口子（别各写一份）
    # ------------------------------------------------------------------
    def reload_qbar_from_disk(self) -> None:
        self.cfg = SC.load_config(self.qbar_path)
        self.refresh_all()
        if self.cfg.error:
            self.status(f"读 {self.qbar_path}：{self.cfg.error}", AMBER)
        else:
            self.status(f"已从 {self.qbar_path} 重读（{len(self.cfg.links)} 条链接）。", GREY)

    def save_qbar(self) -> bool:
        """写回 qbar.toml。有任何一条链接不合法就**拒绝写**（宁可少改，不写坏配置）。"""
        links = self.links_page.memory_links()
        problems = []
        for index, item in enumerate(links, 1):
            for msg in SC.link_problems(item, ()):
                problems.append(f"第 {index} 条「{item.trigger or '?'}」{msg}")
        dups = SC.duplicate_triggers(links)
        for trigger in dups:
            problems.append(f"trigger 完全重复：{trigger}（长前缀优先会变成掷硬币，必须改）")
        if problems:
            self.status("没有写盘，先改这几处：\n  " + "\n  ".join(problems), RED)
            return False
        if not links:
            self.status("没有写盘：一条链接都没有。全删了 Qbar 会退回内置引擎，"
                        "但界面至少该留一条。", RED)
            return False
        default_search, browser, hide_on_focus_out = self.general_page.memory_values()
        try:
            saved = SL.snapshot_config(self.qbar_path, self.backup_dir)
            SC.save_config(links, default_search, browser, hide_on_focus_out, self.qbar_path)
        except OSError as exc:
            self.status(f"写不了 {self.qbar_path}：{exc}", RED)
            return False
        self.cfg.links = links
        self.cfg.default_search, self.cfg.browser = default_search, browser
        self.cfg.hide_on_focus_out = hide_on_focus_out
        hint = f"（原件存到 {saved.parent}）" if saved else ""
        self.status(f"已写入 {self.qbar_path}{hint}；Qbar 下次打开自动重读，不用重启进程。"
                    "注意：条目中间的注释会被模板注释顶掉，自己的说明请写在文件开头。", BLUE)
        self.general_page.reload_from_memory()
        return True

    # ------------------------------------------------------------------
    # mappings.toml 覆盖层（第③页）
    # ------------------------------------------------------------------
    def merged_data(self):
        return MO.merge_overrides(MO.Overrides(values=self.ov.values))


# ===========================================================================
# 第①页：快速链接
# ===========================================================================
class LinksPage(Gtk.Box):
    """表格每行 = trigger / 名称 / 类型 / 目标；带增删改、上下移动与实时预览。"""

    COLUMNS = ("trigger", "名称", "类型", "目标")

    def __init__(self, app: SettingsWindow):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.app = app
        self.set_border_width(10)

        # ListStore 列：trigger, name, type_id, type_label, target, problem
        self.store = Gtk.ListStore(str, str, str, str, str, str)
        self.type_model = Gtk.ListStore(str, str)
        for type_id in SC.LINK_TYPES:
            self.type_model.append([type_id, SC.LINK_TYPE_LABEL[type_id]])

        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_visible(True)
        # 第 6 列是"这行哪儿错了"，直接让 TreeView 拿它当 tooltip（GTK3 内置列）
        self.view.set_tooltip_column(5)
        self.view.get_selection().connect("changed", lambda *_a: self._update_preview())

        for index, title in enumerate(self.COLUMNS):
            if index == 2:                                   # 类型 = 下拉（四个固定值）
                renderer = Gtk.CellRendererCombo(model=self.type_model, text_column=1,
                                                 has_entry=False)
                dropdown_renderer = renderer
                renderer.connect("changed", self._on_type_changed)
                column = Gtk.TreeViewColumn(title, renderer, text=3)
            else:                                            # 其余三格可直接编辑
                renderer = Gtk.CellRendererText()
                renderer.set_property("editable", True)
                renderer.connect("edited", self._on_text_edited, index)
                column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_min_width(280 if index == 3 else 100)
            column.set_expand(index == 3)
            column.set_cell_data_func(renderer, self._paint_problem, index)
            self.view.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(300)
        scroll.add(self.view)
        self.pack_start(scroll, True, True, 0)
        keep_dropdown_attached_to_scroll(self.view, scroll, dropdown_renderer)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in (("新增", self.on_add), ("删除", self.on_delete),
                               ("上移", lambda _b: self.on_move(-1)),
                               ("下移", lambda _b: self.on_move(1)),
                               ("重读文件", lambda _b: self.app.reload_qbar_from_disk())):
            button = Gtk.Button(label=label)
            button.connect("clicked", handler)
            bar.pack_start(button, False, False, 0)
        bar.pack_start(Gtk.Box(), True, True, 0)
        save = Gtk.Button(label="保存快速链接")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", lambda _b: self.app.save_qbar())
        bar.pack_start(save, False, False, 0)
        self.pack_start(bar, False, False, 0)

        preview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preview.pack_start(Gtk.Label(label="示例词：", xalign=0), False, False, 0)
        self.sample = Gtk.Entry()
        self.sample.set_text("关键词")
        self.sample.set_width_chars(18)
        self.sample.connect("changed", lambda _e: self._update_preview())
        preview.pack_start(self.sample, False, False, 0)
        self.preview_label = Gtk.Label(xalign=0)
        self.preview_label.set_line_wrap(True)
        self.preview_label.set_selectable(True)
        preview.pack_start(self.preview_label, True, True, 0)
        self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        self.pack_start(preview, False, False, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_line_wrap(True)
        hint.set_markup(markup(
            "四种类型：search = 网站内搜索（目标里要有 {q} 或 {q_raw}）；"
            "url = 固定网址；app = 本地应用（.desktop 名或一条命令，命令里 {q} 当参数）；"
            "file = 文件或目录路径（~ 会展开，固定路径不支持 {q}）。", GREY))
        self.pack_start(hint, False, False, 0)

        self.reload_from_memory()

    # ------------------------------------------------------------------
    # 内存态 <-> 表格
    # ------------------------------------------------------------------
    def reload_from_memory(self) -> None:
        selected = self.selected_index()
        self.store.clear()
        for link in self.app.cfg.links:
            self.store.append([link.trigger, link.name, link.resolved_type,
                               link.type_label, link.url, ""])
        self._repaint_all()
        if selected is not None and selected < len(self.store):
            self.view.get_selection().select_path(Gtk.TreePath.new_from_indices([selected]))
        self._update_preview()

    def memory_links(self) -> list[SC.SearchLink]:
        return [SC.SearchLink(row[0], row[1], row[4], row[2]) for row in self.store]

    def selected_index(self) -> int | None:
        model, it = self.view.get_selection().get_selected()
        return model.get_path(it).get_indices()[0] if it is not None else None

    # ------------------------------------------------------------------
    # 编辑
    # ------------------------------------------------------------------
    def _on_text_edited(self, _renderer, path_string, new_text, column_index):
        it = self.store.get_iter(Gtk.TreePath.new_from_string(path_string))
        self.store[it][column_index] = new_text.strip()
        self._repaint_all()
        self._update_preview()

    def _on_type_changed(self, _renderer, path, combo_iter):
        it = self.store[path]
        type_id = self.type_model[combo_iter][0]
        it[2] = type_id
        it[3] = SC.LINK_TYPE_LABEL[type_id]
        self._repaint_all()
        self._update_preview()

    def _repaint_all(self) -> None:
        """每行现算一次校验结果 —— trigger 完全重复 = 红字拦下（硬错误）。"""
        links = self.memory_links()
        seen: list[str] = []
        for index, row in enumerate(self.store):
            self.store[index][5] = "；".join(SC.link_problems(links[index], tuple(seen)))
            seen.append(row[0].strip().lower())
        self.view.queue_draw()

    def _paint_problem(self, column, renderer, model, it, column_index):
        problem = model[it][5]
        renderer.set_property("foreground", RED if problem else BLACK)

    # ------------------------------------------------------------------
    # 增 / 删（带确认）/ 上下移动
    # ------------------------------------------------------------------
    def on_add(self, _button):
        it = self.store.append(["", "新链接", "url", SC.LINK_TYPE_LABEL["url"], "", ""])
        path = self.store.get_path(it)
        self.view.get_selection().select_path(path)
        self.view.scroll_to_cell(path, None, True, 0.5, 0.0)
        self.view.set_cursor(path, self.view.get_column(0), True)   # 光标直接落在 trigger 格
        self._repaint_all()
        self.app.status("填 trigger、名称和目标；类型点第三格换。", GREY)

    def on_delete(self, _button):
        index = self.selected_index()
        if index is None:
            self.app.status("先在表格里选中一行。", AMBER)
            return
        row = list(self.store[index])
        dialog = Gtk.MessageDialog(transient_for=self.app, modal=True,
                                   buttons=Gtk.ButtonsType.YES_NO,
                                   message_type=Gtk.MessageType.QUESTION,
                                   text=f"删除链接「{row[1]}」（trigger = {row[0] or '未填'}）？")
        dialog.format_secondary_text("只是从表里去掉；点「保存快速链接」才会真的写文件。")
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.YES:
            return
        self.store.remove(self.store.get_iter(Gtk.TreePath.new_from_indices([index])))
        self._repaint_all()
        self._update_preview()
        self.app.status("已删除（还没写盘，记得点「保存快速链接」）。", GREY)

    def on_move(self, delta: int) -> None:
        index = self.selected_index()
        if index is None:
            self.app.status("先选中一行再移动。", AMBER)
            return
        target = index + delta
        if target < 0 or target >= len(self.store):
            self.app.status("已经到顶 / 到底了。", GREY)
            return
        rows = [list(row) for row in self.store]
        rows[index], rows[target] = rows[target], rows[index]
        self.store.clear()
        for row in rows:
            self.store.append(row)
        self.view.get_selection().select_path(Gtk.TreePath.new_from_indices([target]))
        self._repaint_all()
        self._update_preview()
        self.app.status(f"顺序变了（第 {index + 1} 行 ↔ 第 {target + 1} 行）；"
                        f"配置文件里的先后顺序就是表格顺序，写回时原样保留。", GREY)

    # ------------------------------------------------------------------
    # 实时预览：输入 `b 关键词` → 最终 URL
    # ------------------------------------------------------------------
    def _update_preview(self) -> None:
        index = self.selected_index()
        if index is None:
            self.preview_label.set_markup(markup("选中一行就能预览。", GREY))
            return
        link = self.memory_links()[index]
        problem = self.store[index][5]
        if problem:
            self.preview_label.set_markup(markup(f"这条先改对：{problem}", RED))
            return
        text, result = SC.preview_pair(link, self.sample.get_text())
        self.preview_label.set_markup(
            f'输入 <b>{GLib.markup_escape_text(text)}</b> → {VERB[link.resolved_type]} '
            f'<tt>{GLib.markup_escape_text(result)}</tt>')


# ===========================================================================
# 第②页：通用
# ===========================================================================
class GeneralPage(Gtk.Box):
    def __init__(self, app: SettingsWindow):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app = app
        self.set_border_width(10)
        grid = Gtk.Grid(row_spacing=12, column_spacing=14)

        self.default_combo = Gtk.ComboBoxText()
        self.browser_entry = Gtk.Entry()
        self.browser_entry.set_width_chars(40)
        self.browser_entry.connect("changed", lambda *_a: self._mark_dirty())
        self.focus_switch = Gtk.Switch()
        self.focus_switch.set_halign(Gtk.Align.START)

        for row, (label, widget, key) in enumerate((
                ("默认搜索引擎", self.default_combo, "default_search"),
                ("浏览器命令", self.browser_entry, "browser"),
                ("失焦即隐藏", self.focus_switch, "hide_on_focus_out"))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
            help_label = Gtk.Label(xalign=0)
            help_label.set_line_wrap(True)
            help_label.set_markup(markup(GENERAL_HELP[key], GREY))
            grid.attach(help_label, 2, row, 1, 1)
        self.pack_start(grid, False, False, 0)

        self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_markup(markup(
            "这一页和「快速链接」写的是同一个文件（qbar.toml），"
            "随便哪一页点保存，都会把两页的改动一起写进去。", GREY))
        self.pack_start(note, False, False, 0)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        save = Gtk.Button(label="保存通用设置")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", lambda _b: self.app.save_qbar())
        bar.pack_start(save, False, False, 0)
        self.pack_start(bar, False, False, 0)
        self._search_triggers: list[str] = []
        self._loading = False
        self.reload_from_memory()

    def reload_from_memory(self) -> None:
        cfg = self.app.cfg
        self._loading = True              # 回填控件时别把"改了没保存"的提示触发出来
        self.default_combo.remove_all()
        search_links = [l for l in cfg.links if l.resolved_type == "search"]
        for link in search_links:
            self.default_combo.append_text(f"{link.trigger} — {link.name}")
        self._search_triggers = [l.trigger for l in search_links]
        if search_links:
            active = next((i for i, link in enumerate(search_links)
                           if link.trigger == cfg.default_search), 0)
            self.default_combo.set_active(active)
        self.browser_entry.set_text(cfg.browser)
        self.focus_switch.set_active(bool(cfg.hide_on_focus_out))
        self._loading = False

    def memory_values(self) -> tuple[str, str, bool]:
        """(default_search, browser, hide_on_focus_out) —— 界面值收成可写盘的三元组。"""
        index = self.default_combo.get_active()
        if 0 <= index < len(self._search_triggers):
            default_search = self._search_triggers[index]
        else:
            # 链接表里一条 search 都没有：别把不存在的 trigger 写进配置
            default_search = self.app.cfg.default_search
        return (default_search, self.browser_entry.get_text().strip(),
                self.focus_switch.get_active())

    def _mark_dirty(self) -> None:
        if getattr(self, "_loading", False):
            return                        # 程序自己在回填，不是用户改的
        if self.memory_values() != (self.app.cfg.default_search, self.app.cfg.browser,
                                    self.app.cfg.hide_on_focus_out):
            self.app.status("通用设置改了但还没写盘 —— 点「保存通用设置」。", AMBER)


# ===========================================================================
# 第③页：按键映射
# ===========================================================================
class MappingPage(Gtk.Box):
    """当前生效表（层/键/动作/说明/来源）；「动作」= 常用下拉 + 允许自由输入。"""

    COLUMNS = ("层", "键", "动作", "说明", "来源")

    def __init__(self, app: SettingsWindow):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.app = app
        self.set_border_width(10)

        # ListStore 列：section, key, action, note, source, problem
        self.store = Gtk.ListStore(str, str, str, str, str, str)
        self.action_model = Gtk.ListStore(str)
        for action in SL.COMMON_ACTIONS:
            self.action_model.append([action])

        self.view = Gtk.TreeView(model=self.store)
        self.view.set_tooltip_column(5)            # 出错原因挂在 tooltip 上
        for index, title in enumerate(self.COLUMNS):
            if index == 2:
                renderer = Gtk.CellRendererCombo(model=self.action_model, text_column=0,
                                                 has_entry=True)      # 下拉 + 自由输入
                dropdown_renderer = renderer
                renderer.set_property("editable", True)
                renderer.connect("edited", self._on_action_edited)
                renderer.connect("changed", self._on_action_edited)
                column = Gtk.TreeViewColumn(title, renderer, text=2)
            else:
                renderer = Gtk.CellRendererText()
                column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_min_width(200 if index in (2, 3) else 70)
            column.set_expand(index in (2, 3))
            column.set_cell_data_func(renderer, self._paint_row, index)
            self.view.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(330)
        scroll.add(self.view)
        self.pack_start(scroll, True, True, 0)
        keep_dropdown_attached_to_scroll(self.view, scroll, dropdown_renderer)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in (("恢复默认（删这条覆盖）", self.on_restore),
                               ("预览改动（只读）", self.on_preview),
                               ("只试渲染（不动系统）", lambda _b: self.on_apply(dry_run=True)),
                               ("重读覆盖层", lambda _b: self.reload_overrides())):
            button = Gtk.Button(label=label)
            button.connect("clicked", handler)
            bar.pack_start(button, False, False, 0)
        apply_button = Gtk.Button(label="应用（改 /etc/keyd 并 reload）")
        apply_button.get_style_context().add_class("suggested-action")
        apply_button.connect("clicked", lambda _b: self.on_apply())
        bar.pack_start(apply_button, False, False, 0)
        self.pack_start(bar, False, False, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_line_wrap(True)
        hint.set_markup(markup(
            "改完点「应用」才生效；出错自动回滚上一版（备份在 ~/.local/state/capsplus/"
            "keyd-conf-backup/）。改动只写进覆盖层 mappings.toml，没改的键跟着仓库默认走；"
            f"宏硬边界：单条 macro(...) 累计 ≤{SL.MACRO_EVENT_LIMIT} 个按键事件，超了整条静默不发。",
            GREY))
        self.pack_start(hint, False, False, 0)

        self.reload_from_memory()

    # ------------------------------------------------------------------
    def reload_overrides(self) -> None:
        self.app.ov = MO.load_overrides(self.app.overrides_path, self.app.known_keys)
        self.reload_from_memory()
        if self.app.ov.error:
            self.app.status(f"覆盖层读不了：{self.app.ov.error}", RED)
        elif self.app.ov.errors:
            self.app.status("覆盖层里有几条被跳过（没生效）：\n  "
                            + "\n  ".join(self.app.ov.errors), AMBER)
        else:
            self.app.status(f"已从 {self.app.overrides_path} 重读。", GREY)

    def reload_from_memory(self) -> None:
        selected = self.selected_pair()
        merged = self.app.merged_data()
        self.store.clear()
        for section in MO.ALLOWED_SECTIONS:
            for key, action, note, source in binding_rows(section, merged):
                self.store.append([section, key, action, note, source, ""])
        if selected:
            for index, row in enumerate(self.store):
                if (row[0], row[1]) == selected:
                    self.view.get_selection().select_path(Gtk.TreePath.new_from_indices([index]))
                    break
        self._count_status()

    def _count_status(self, text: str | None = None, color: str = GREY) -> None:
        overridden = sum(1 for row in self.store if row[4] == "覆盖")
        base = (f"当前生效 {len(self.store)} 条，其中 {overridden} 条来自覆盖层"
                f"（{self.app.overrides_path}）")
        self.app.status((text + "\n" + base) if text else base + "。", color)

    def selected_pair(self) -> tuple[str, str] | None:
        model, it = self.view.get_selection().get_selected()
        return (model[it][0], model[it][1]) if it is not None else None

    def _paint_row(self, column, renderer, model, it, column_index):
        problem, source = model[it][5], model[it][4]
        if problem:
            color = RED
        elif source == "覆盖":
            color = BLUE                       # 覆盖过的行标蓝 = "这条跟仓库默认不一样"
        elif column_index == 3:
            color = GREY                       # 说明列永远灰
        else:
            color = BLACK
        renderer.set_property("foreground", color)
        # 颜色会被选中态的白字盖掉，所以"覆盖"再加粗：选中那一行也认得出它是覆盖来的
        renderer.set_property("weight",
                              Pango.Weight.BOLD if source == "覆盖" and column_index in (2, 4)
                              else Pango.Weight.NORMAL)

    # ------------------------------------------------------------------
    def _on_action_edited(self, _renderer, path, new_text):
        row = self.store[path]
        section, key = row[0], row[1]
        problem = SL.action_problem(new_text, key, section, self.app.known_keys)
        if problem:
            # 非法就保持原值：表格里不许留坏数据，否则下一次渲染会把整层带崩
            self.app.status(f"[{section}] {key} 没改成「{new_text}」：{problem}", RED)
            return
        try:
            self.app.ov.values = SL.set_override(self.app.ov.values, section, key,
                                                 new_text, self.app.known_keys)
        except ValueError as exc:
            self.app.status(f"[{section}] {key} 没改：{exc}", RED)
            return
        if new_text.strip() == self._default_action(section, key):
            # 改回和默认一样 = 这条覆盖没意义，顺手删掉（覆盖层只存"真的改过的"）
            self.app.ov.values = SL.delete_override(self.app.ov.values, section, key)
        self.reload_from_memory()
        warning = SL.macro_warning(new_text)
        if warning:
            self._count_status(warning, AMBER)
        elif self._dirty():
            self._count_status(f"[{section}] {key} 改好了，还没写盘。"
                               f"点「应用」= 写覆盖层 + 装到 /etc/keyd + reload；"
                               f"点「恢复默认」= 把这条覆盖删掉。", BLUE)
        else:
            self._count_status(f"[{section}] {key} 已是这个值（覆盖层没变化）。", GREY)

    def _default_action(self, section: str, key: str) -> str:
        for row in binding_rows(section):          # 不传 merged = 仓库默认表
            if row[0] == key:
                return row[1]
        return ""

    def _dirty(self) -> bool:
        """内存里的覆盖层和磁盘上那份不一样吗？"""
        on_disk = MO.load_overrides(self.app.overrides_path, self.app.known_keys).values
        return on_disk != self.app.ov.values

    def on_restore(self, _button):
        pair = self.selected_pair()
        if pair is None:
            self.app.status("先选中要恢复默认的那一行。", AMBER)
            return
        section, key = pair
        if self.app.ov.get(section, key) is None:
            self._count_status(f"[{section}] {key} 本来就没有覆盖，已经是仓库默认。", GREY)
            return
        self.app.ov.values = SL.delete_override(self.app.ov.values, section, key)
        self.reload_from_memory()
        self._count_status(f"[{section}] {key} 已恢复默认。"
                           f"{'还没写盘，点「应用」把它从覆盖层里删掉。' if self._dirty() else ''}",
                           BLUE)

    def on_preview(self, _button):
        """仓库默认渲染 vs 默认+覆盖渲染差哪几条 —— 纯只读，不碰系统。"""
        after = render(self.app.merged_data())
        deltas = SL.changed_bindings(render(), after)
        if not deltas:
            self._count_status("覆盖层为空：渲染结果与仓库默认**逐条相同**。", GREY)
            return
        lines = "\n  ".join(deltas)
        hints = "\n".join(h for h in (SL.override_toml_hint(section)
                                       for section in MO.ALLOWED_SECTIONS
                                       if self.app.ov.values.get(section)) if h)
        self._count_status("应用之后会改成这几条：\n  " + lines
                           + (("\n" + hints) if hints else ""), BLUE)

    def on_apply(self, dry_run: bool = False) -> None:
        """写覆盖层 -> 走现有 apply 流程（备份 / reload / 抓 journal / 出错自动回滚）。"""
        if self._dirty():
            try:
                SL.save_overrides_with_snapshot(self.app.ov.values, self.app.overrides_path,
                                                self.app.backup_dir)
            except OSError as exc:
                self.app.status(f"写不了 {self.app.overrides_path}：{exc}", RED)
                return
        # 传 overrides_path：渲染用的必须就是上面刚写下去的那份（临时目录演练也要真）
        ok, text = SL.run_apply(dry_run=dry_run, overrides_path=self.app.overrides_path,
                                known_keys=self.app.known_keys)
        self._count_status(text, BLUE if ok else RED)
        if ok and not dry_run:
            self.reload_overrides()


# ===========================================================================
# 单实例：Unix socket（第二次启动只把已经开着的窗口拉到前台）
# ===========================================================================
def try_focus_existing() -> bool:
    if not SOCK_PATH.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(SOCK_PATH))
            sock.sendall(b"f")
        return True
    except OSError:
        SOCK_PATH.unlink(missing_ok=True)          # 陈旧 socket（上次没退干净）
        return False


def serve_socket(window: SettingsWindow) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
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
            GLib.idle_add(window.focus_self)

    threading.Thread(target=loop, daemon=True).start()


def _install_signal_handlers() -> None:
    """Ctrl+C / SIGTERM 走正常退出，好把 socket 文件删掉（否则留个陈旧 socket）。"""
    import signal

    def quit_(_sig, _frame):
        GLib.idle_add(Gtk.main_quit)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, quit_)


def _arg_value(argv: list[str], name: str) -> Path | None:
    """取 `--qbar /path/x` 这种参数的值（自检与单测靠它把配置指到临时目录）。"""
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return Path(argv[index + 1])
    return None


def main(argv: list[str]) -> int:
    if try_focus_existing() and "--selftest" not in argv:
        print("设置窗口已经开着了（已让它回到前台）")
        return 0
    window = SettingsWindow(qbar_path=_arg_value(argv, "--qbar"),
                            overrides_path=_arg_value(argv, "--overrides"),
                            state_dir=_arg_value(argv, "--state-dir"))
    window.connect("destroy", lambda _w: SOCK_PATH.unlink(missing_ok=True))
    window.show_all()
    _install_signal_handlers()
    if "--selftest" not in argv:
        serve_socket(window)
    else:                                       # 自检用：不起 socket，免得抢真窗口的入口
        GLib.timeout_add(1200, Gtk.main_quit)
    Gtk.main()
    # 退出路径不止 destroy（自检用 main_quit、超时被杀都到不了 destroy），
    # 这里补一次 unlink，否则下次启动只会 connect 上一个没人听的空 socket 文件。
    SOCK_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
