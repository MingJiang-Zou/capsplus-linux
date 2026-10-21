#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""capsplus-settings —— CapsPlus 管理器（GTK3，中文，五页）。

    ⓪ 状态       **本工具的总入口**：keyd / 扩展 / 语音 / 截图 / 用户侧栈一页看全，
                 每条红项配一个按钮（同步扩展、应用键位、重启托盘、一键修复）。
                 那份状态**不是这里另算的**：直接调用 `capsplus.cli.collect_status()`，
                 与命令行 `capsplus status` 是同一份真相（2026-10-20 统一入口时定的）。
    ① 快速链接   只读表格 + 表格下方的**表单条**：增/删/上下移动 + 五种类型 + 实时预览，
                 写回 `qbar.toml`
    ② 通用       default_search / browser / hide_on_focus_out
    ③ 按键映射   只读生效表（层/键/动作/说明/来源）+ 表格下方的**编辑条**，
                 改动写覆盖层 `mappings.toml`，「应用」走 render_with_overrides() + 现有
                 apply 流程（出错自动回滚）
    ④ 服务       翻译 / OCR / 语音识别 各自"用哪台机器上的哪个服务"（2026-10-21 加）。
                 数据来自 `capsplus/services.py`，**这里不另算一份**；凭据只显示
                 "已配置（来自 X）"，**界面上任何地方都不出现明文 key**。

**表格一律只读**（2026-10-15 治本方案 A，用户实测截图定案）：Wayland/GTK3 下 TreeView 的
cell editor 是独立 surface、**不跟随表格滚动** —— 表格滚了、编辑框的像素坐标不动，于是编辑框
视觉上落到别的行，用户以为自己改的是 `t`，实际写进了 `k`。这是"静默改错键"，上一轮"滚动即收
悬浮下拉"只补了下拉那一半，输入框本身照样脱开。所以把编辑控件从滚动的表格里**搬出去**：
表格只显示，编辑走表格下方固定的编辑条/表单条，这个 bug 类从根上没有立足点
（源码级判据见 `tests/test_table_readonly.py`：出现 `CellRendererCombo` 或
`set_property("editable", True)` 直接判红）。

分工（**这个文件只摆控件**）：校验、覆盖层增删、宏事件计数、"第 N 行是谁"、预览拼装、
TOML 写回、apply 流程全在 `capsplus/settings_logic.py` 与 `qbar/search_config.py`，
那两份不 import gi、有单测。

单实例：Unix socket `~/.local/state/capsplus/settings.sock`。第二次启动（含 Qbar 的
`-> settings`）只发一个字节，让已经开着的窗口回到前台 —— 开两个窗口各自覆盖对方的改动，
是最容易丢配置的那种事故。

入口四个，都通向**这一个窗口**：托盘「设置…」、Qbar `-> settings`、命令行 `capsplus settings`、
应用菜单里的「CapsPlus」。2026-10-20 之前的规则是"刻意不装菜单条目"（应用里条目越多越难找），
现在的规则换成"**应用里有且只有一条，而且它就是总入口**" —— 理由见 README「安装」一节。
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
# 这里**不用** Gdk：编辑控件离开表格之后，"有没有悬浮下拉"这件事已经没人关心了，
# 上一轮为探测下拉而引入的 Gdk 已随那段死代码一起删掉。
# （留个提醒给以后：真要 `from gi.repository import Gdk`，必须同时加
#  `gi.require_version("Gdk", "3.0")` —— 漏了会默认加载 4.0，`import Gdk` 当场 ImportError，
#  而且**只有真跑入口才暴露**（2026-10-15 就是这么翻车的，判据在
#  `tests/test_settings_window.py::test_real_entry_points_import_cleanly`）。）
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from capsplus import mapping_overrides as MO  # noqa: E402
from capsplus import services as SV  # noqa: E402
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
    "raise_browser": "打开网址前先把浏览器现有窗口激活（需要装仓内 GNOME 扩展 raise-browser；"
                     "没装就自动跳过，不影响打开）。不激活时浏览器会另起一扇新窗口。",
}

VERB = {"search": "浏览器搜", "url": "浏览器开", "app": "启动", "file": "打开",
        "ask": "复制问题并浏览器开"}


def markup(text: str, color: str = "") -> str:
    """灰/红/黄字都走 pango markup；转义交给 GLib（要显示的都是用户自己输入的文本）。"""
    escaped = GLib.markup_escape_text(text)
    return f'<span foreground="{color}">{escaped}</span>' if color else escaped


def bold(text: str) -> str:
    """编辑条里「这一格是谁」用加粗写出来 —— 防写错键的那道保险。"""
    return f"<b>{GLib.markup_escape_text(text)}</b>"


def store_rows(store) -> list[tuple]:
    """Gtk.TreeStore -> 纯元组序列，交给不 import gi 的判据函数（`SL.editor_binding` 等）。"""
    return [tuple(row) for row in store]


# ===========================================================================
# ⓪ 状态页 —— 总入口
# ===========================================================================
class StatusPage(Gtk.Box):
    """一页看全 + 就地修。判断全部来自 `capsplus.cli.collect_status()`（不在这里另算一份）。

    两条刻意的设计（2026-10-20）：
      · **探测放到线程里**：一次体检要问 systemctl、读扩展状态、探识别服务，串起来 1~2 秒。
        直接在 GTK 主线程里做，窗口会假死，用户以为程序卡住。
      · **按钮只调 `capsplus` 子命令**，不在界面里重写一遍装机/apply 的逻辑 ——
        否则窗口和命令行又会变成两份实现、两套行为。
    """

    BUTTONS = (
        ("重新检查", ["status"], False),
        ("一键修复", ["doctor", "--fix"], False),
        ("同步扩展", ["ext", "sync"], False),
        ("应用键位", ["apply"], False),
        ("重启托盘", ["restart"], False),
    )

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.app = app
        self.set_border_width(10)

        hint = Gtk.Label(xalign=0)
        hint.set_line_wrap(True)
        hint.set_markup(markup("这是 CapsPlus 的总入口。红项下面都有对应的按钮；"
                               "命令行等价物是 `capsplus status` / `capsplus doctor --fix`。", GREY))
        self.pack_start(hint, False, False, 0)

        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_left_margin(8)
        self.view.set_top_margin(6)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.view)
        self.pack_start(scroll, True, True, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, argv, _ in self.BUTTONS:
            button = Gtk.Button(label=label)
            button.connect("clicked", self._on_action, argv)
            row.pack_start(button, False, False, 0)
        open_dir = Gtk.Button(label="打开日志目录")
        open_dir.connect("clicked", self._on_open_logs)
        row.pack_end(open_dir, False, False, 0)
        self.pack_start(row, False, False, 0)

        self.reload()

    # ── 探测（线程里做，回主线程画）────────────────────────────────────
    def reload(self) -> None:
        self._set_text("检查中……（systemctl / 扩展 / 语音 / 截图，约 1~2 秒）")
        threading.Thread(target=self._probe, daemon=True).start()

    def _probe(self) -> None:
        import io

        from capsplus import cli

        buf = io.StringIO()
        try:
            cli.render_groups(cli.collect_status(), buf)
            text = buf.getvalue()
        except Exception as exc:  # noqa: BLE001 —— 体检自己挂了也要把原因显示出来
            text = f"体检失败：{type(exc).__name__}: {exc}\n命令行排查：capsplus status"
        GLib.idle_add(self._set_text, text)

    def _set_text(self, text: str) -> bool:
        self.view.get_buffer().set_text(text)
        return False        # 只跑一次（GLib.idle_add 的约定）

    # ── 按钮 ───────────────────────────────────────────────────────────
    def _on_action(self, _button, argv: list[str]) -> None:
        self._set_text(f"正在执行：capsplus {' '.join(argv)} ……")
        threading.Thread(target=self._run_cli, args=(argv,), daemon=True).start()

    def _run_cli(self, argv: list[str]) -> None:
        import subprocess

        cmd = [sys.executable, str(REPO / "bin" / "capsplus"), *argv]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            text = (proc.stdout + proc.stderr).strip() or f"（{argv[0]} 没有输出）"
            text += f"\n\n[退出码 {proc.returncode}]"
        except Exception as exc:  # noqa: BLE001
            text = f"执行失败：{type(exc).__name__}: {exc}"
        GLib.idle_add(self._set_text, text)
        # 动作做完再体检一次：用户要看到的是"现在怎么样"，不是动作的回显
        GLib.timeout_add(600, lambda: (self.reload(), False)[1])

    def _on_open_logs(self, _button) -> None:
        import subprocess

        try:
            subprocess.Popen(["xdg-open", str(STATE_DIR)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def reload_from_memory(self) -> None:
        """设置窗统一刷新时会调它（状态页没有内存态，重探一次就够）。"""
        self.reload()


# ===========================================================================
# 窗口
# ===========================================================================
class SettingsWindow(Gtk.Window):
    def __init__(self, qbar_path: Path | None = None, overrides_path: Path | None = None,
                 state_dir: Path | None = None, services_path: Path | None = None):
        super().__init__(title="CapsPlus 管理器")
        self.set_default_size(960, 680)

        self.qbar_path = Path(qbar_path) if qbar_path else SC.CONFIG_PATH
        self.overrides_path = Path(overrides_path) if overrides_path else MO.OVERRIDES_PATH
        self.state_dir = Path(state_dir) if state_dir else STATE_DIR
        # 服务配置（翻译 / OCR / 识别各连哪台机器）—— 2026-10-21 加的第五页
        self.services_path = Path(services_path) if services_path else SV.SERVICES_PATH
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

        self.status_page = StatusPage(self)
        self.links_page = LinksPage(self)
        self.general_page = GeneralPage(self)
        self.mapping_page = MappingPage(self)
        self.services_page = ServicesPage(self)
        for title, page in (("状态", self.status_page), ("快速链接", self.links_page),
                            ("通用", self.general_page), ("按键映射", self.mapping_page),
                            ("服务", self.services_page)):
            self.notebook.append_page(page, Gtk.Label(label=title))
        # 打开就落在「状态」：它是总入口 —— 谁点开这个窗口，十有八九是某个键没反应。
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
        if self.services_page.services.error:
            self.status(f"服务配置读不了，已按内置默认处理：{self.services_page.services.error}"
                        "（下一步：改完点「保存服务配置」会写出一份好的；原件会先备份到 "
                        f"{self.backup_dir}）", RED)

        self.connect("delete-event", self._on_delete)

    # ------------------------------------------------------------------
    def status(self, text: str, color: str = GREY) -> None:
        """五页共用一条状态栏：红=拦住了、黄=提醒、灰=说明、蓝=写成了。"""
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
        """改完一处三个配置页都跟着变（第②页的下拉候选就是第①页的链接表）。

        状态页不在这里刷：它每次重探都要问 systemctl 与识别服务，
        跟着每一次按键变化重跑会把窗口拖慢（它有自己的「重新检查」）。
        """
        self.links_page.reload_from_memory()
        self.general_page.reload_from_memory()
        self.mapping_page.reload_from_memory()
        self.services_page.reload_from_memory()

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
        default_search, browser, hide_on_focus_out, raise_browser = \
            self.general_page.memory_values()
        try:
            saved = SL.snapshot_config(self.qbar_path, self.backup_dir)
            SC.save_config(links, default_search, browser, hide_on_focus_out, self.qbar_path,
                           raise_browser=raise_browser)
        except OSError as exc:
            self.status(f"写不了 {self.qbar_path}：{exc}", RED)
            return False
        self.cfg.links = links
        self.cfg.default_search, self.cfg.browser = default_search, browser
        self.cfg.hide_on_focus_out = hide_on_focus_out
        self.cfg.raise_browser = raise_browser
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
    """上方**只读**表格（前缀/名称/类型/目标/备注）+ 表格下方的**表单条**。

    表格为什么必须只读：见文件头 —— Wayland 下 cell editor 不跟表格滚动，会静默改错行。
    表单条不参与滚动，"第几行"这件事由 `SC.form_at(表格行, 选中行号)` 现算，
    而且点「保存这一条」时还会再核一遍（表单认的行号 == 表格选中的行号），不一致就拦下。
    """

    COLUMNS = ("前缀", "名称", "类型", "目标", "备注")
    # 列号 -> ListStore 列号（类型显示第 3 列的中文标签，备注显示第 6 列的现算文本）
    TEXT_COL = (0, 1, 3, 4, 6)

    def __init__(self, app: SettingsWindow):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.app = app
        self.set_border_width(10)

        # ListStore 列：trigger, name, type_id, type_label, target, problem, 备注显示
        self.store = Gtk.ListStore(str, str, str, str, str, str, str)

        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_visible(True)
        self.view.set_tooltip_column(5)         # 第 5 列是"这行哪儿错了"，也挂 tooltip
        self.view.get_selection().connect("changed", self._on_selection_changed)
        self.view.connect("row-activated", self._on_row_activated)

        for index, title in enumerate(self.COLUMNS):
            renderer = Gtk.CellRendererText()
            renderer.set_property("editable", False)    # 只读；判据见 tests/test_table_readonly.py
            column = Gtk.TreeViewColumn(title, renderer, text=self.TEXT_COL[index])
            column.set_resizable(True)
            column.set_min_width(240 if index == 3 else 90)
            column.set_expand(index == 3)
            column.set_cell_data_func(renderer, self._paint_cell, index)
            self.view.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(280)
        scroll.add(self.view)
        self.pack_start(scroll, True, True, 0)

        # ------------------------------------------------------------------
        # 表单条：字段 + 「保存这一条」
        # ------------------------------------------------------------------
        form = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.f_trigger = Gtk.Entry()
        self.f_trigger.set_width_chars(10)
        self.f_name = Gtk.Entry()
        self.f_name.set_width_chars(18)
        self.f_type = Gtk.ComboBoxText()
        for type_id in SC.LINK_TYPES:
            self.f_type.append_text(f"{SC.LINK_TYPE_LABEL[type_id]}（{type_id}）")
        self.f_target = Gtk.Entry()
        self.f_target.set_width_chars(34)
        self.f_target.set_hexpand(True)
        for label, widget in (("前缀", self.f_trigger), ("名称", self.f_name),
                              ("类型", self.f_type), ("目标", self.f_target)):
            form.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
            form.pack_start(widget, False, False, 0)
        self.save_row_button = Gtk.Button(label="保存这一条")
        self.save_row_button.get_style_context().add_class("suggested-action")
        self.save_row_button.connect("clicked", self.on_save_row)
        form.pack_start(self.save_row_button, False, False, 0)
        self.pack_start(form, False, False, 0)

        for widget in (self.f_trigger, self.f_name, self.f_target):
            widget.connect("changed", lambda *_a: self._on_form_edited())
            widget.connect("activate", lambda *_a: self.on_save_row(None))
        self.f_type.connect("changed", lambda *_a: self._on_form_edited())

        # ------------------------------------------------------------------
        # 工具条：增删移 + 文件级动作（这些改的是整张表，写盘靠「保存快速链接」）
        # ------------------------------------------------------------------
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in (("新增", self.on_add),
                               ("删除（带确认）", self.on_delete),
                               ("上移", lambda _b: self.on_move(-1)),
                               ("下移", lambda _b: self.on_move(1)),
                               ("重读文件", lambda _b: self.app.reload_qbar_from_disk())):
            button = Gtk.Button(label=label)
            button.connect("clicked", handler)
            tools.pack_start(button, False, False, 0)
        tools.pack_start(Gtk.Box(), True, True, 0)
        save = Gtk.Button(label="保存快速链接")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", lambda _b: self.app.save_qbar())
        tools.pack_start(save, False, False, 0)
        self.pack_start(tools, False, False, 0)

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
            "表格只读：选中一行 → 下面表单条填的是**那一行**，双击行直接把焦点送进对应输入框。"
            "五种类型：search = 网站内搜索（目标里要有 {q} 或 {q_raw}）；"
            "url = 固定网址；app = 本地应用（.desktop 名或一条命令，命令里 {q} 当参数）；"
            "file = 文件或目录路径（~ 会展开，固定路径不支持 {q}）；"
            "ask = 问 AI（复制+打开）：url 必须是 http(s) 网址，回车把问题原文拷进剪贴板、"
            "同时打开那个网页，粘贴后回车。", GREY))
        self.pack_start(hint, False, False, 0)

        self.form_index: int | None = None
        self._loading = False
        self.reload_from_memory()

    # ------------------------------------------------------------------
    # 内存态 <-> 表格
    # ------------------------------------------------------------------
    def reload_from_memory(self) -> None:
        selected = self.selected_index()
        self.store.clear()
        for link in self.app.cfg.links:
            self.store.append([link.trigger, link.name, link.resolved_type,
                               link.type_label, link.url, "", "—"])
        self._repaint_all()
        if selected is not None and selected < len(self.store):
            self.view.get_selection().select_path(Gtk.TreePath.new_from_indices([selected]))
        else:
            self.sync_form_to_row()
        self._update_preview()

    def memory_links(self) -> list[SC.SearchLink]:
        return [SC.SearchLink(row[0], row[1], row[4], row[2]) for row in self.store]

    def selected_index(self) -> int | None:
        model, it = self.view.get_selection().get_selected()
        return model.get_path(it).get_indices()[0] if it is not None else None

    # ------------------------------------------------------------------
    # 表单条 <-> 选中的那一行
    # ------------------------------------------------------------------
    def form_values(self) -> tuple[str, str, str, str]:
        """表单四个框的当前值 `(trigger, name, type_id, target)`（type 存 id，不存中文）。"""
        position = self.f_type.get_active()
        type_id = SC.LINK_TYPES[position] if 0 <= position < len(SC.LINK_TYPES) else ""
        return (self.f_trigger.get_text().strip(), self.f_name.get_text().strip(),
                type_id, self.f_target.get_text().strip())

    def sync_form_to_row(self) -> None:
        """把表单条填成**表格选中的那一行**（行号现算，不猜、不从别的来源取）。"""
        index = self.selected_index()
        fields = SC.form_at(store_rows(self.store), index)
        self._loading = True
        self.form_index = index if fields is not None else None
        if fields is None:                        # 没选中/空表：清空，别留上一行的残影
            self.f_trigger.set_text("")
            self.f_name.set_text("")
            self.f_target.set_text("")
            self.f_type.set_active(-1)
        else:
            trigger, name, type_id, target = fields
            self.f_trigger.set_text(trigger)
            self.f_name.set_text(name)
            self.f_target.set_text(target)
            self.f_type.set_active(SC.LINK_TYPES.index(type_id) if type_id in SC.LINK_TYPES
                                   else len(SC.LINK_TYPES) - 1)
        self._loading = False
        self._update_preview()

    def _on_selection_changed(self, *_args) -> None:
        self.sync_form_to_row()

    def _on_form_edited(self) -> None:
        """表单里打字 = 只刷新预览（**不写表格、不写盘**），改成什么由「保存这一条」决定。"""
        if not self._loading:
            self._update_preview()

    def _on_row_activated(self, _view, _path, column, _data=None) -> None:
        """双击（或键盘回车）某一行：先同步表单，再把焦点落到那一列对应的输入框。"""
        self.sync_form_to_row()
        columns = self.view.get_columns()
        try:
            position = columns.index(column) if column is not None else -1
        except ValueError:
            position = -1
        widget = {0: self.f_trigger, 1: self.f_name, 2: self.f_type,
                  3: self.f_target}.get(position, self.f_target)
        widget.grab_focus()

    def _commit_index(self) -> int | None:
        """要写回第几行 —— 表单认的行号必须 == 表格当前选中的行号，否则拦住。"""
        selected = self.selected_index()
        if self.form_index is None:
            self.app.status("先在表格里选中一行，或点「新增」。", AMBER)
            return None
        if self.form_index != selected:
            chosen = f"第 {selected + 1} 行" if selected is not None else "没选中任何行"
            self.app.status(f"没写盘：表单里显示的是第 {self.form_index + 1} 行，"
                            f"表格里选中的却是{chosen}。"
                            f"先点一下真正要改的那一行 —— 认错行比改错值严重得多。", RED)
            return None
        return selected

    def on_save_row(self, _button) -> None:
        """校验表单 -> 写进表格那一行 -> 走 `save_qbar()` 写文件（保留开头注释与顺序）。"""
        index = self._commit_index()
        if index is None:
            return
        fields = self.form_values()
        problems = SC.form_problems(fields, index, self.memory_links())
        if problems:
            self.app.status("没有写盘，先改这几处：\n  " + "\n  ".join(problems), RED)
            return
        trigger, name, type_id, target = fields
        for column, value in enumerate((trigger, name, type_id,
                                        SC.LINK_TYPE_LABEL[type_id], target, "")):
            self.store[index][column] = value
        self._repaint_all()
        self.app.save_qbar()

    # ------------------------------------------------------------------
    # 校验结果画进表格（备注列）
    # ------------------------------------------------------------------
    def _paint_cell(self, column, renderer, model, it, column_index):
        """有毛病的行**整行标红**（备注列没毛病时显示 — ，所以它是灰字）。"""
        problem = model[it][5]
        if problem:
            renderer.set_property("foreground", RED)
        else:
            renderer.set_property("foreground", GREY if column_index == 4 else BLACK)

    def _repaint_all(self) -> None:
        """每行现算一次校验结果 —— trigger 完全重复 = 红字拦下（硬错误）。"""
        links = self.memory_links()
        seen: list[str] = []
        for index, row in enumerate(self.store):
            problem = "；".join(SC.link_problems(links[index], tuple(seen)))
            self.store[index][5] = problem
            self.store[index][6] = problem or "—"
            seen.append(row[0].strip().lower())
        self.view.queue_draw()

    # ------------------------------------------------------------------
    # 增 / 删（带确认）/ 上下移动
    # ------------------------------------------------------------------
    def on_add(self, _button):
        it = self.store.append(["", "新链接", "url", SC.LINK_TYPE_LABEL["url"], "", "", "—"])
        path = self.store.get_path(it)
        index = path.get_indices()[0]
        self._repaint_all()
        self.view.get_selection().select_path(path)      # 触发 sync_form_to_row
        self.view.scroll_to_cell(path, None, True, 0.5, 0.0)
        self.f_trigger.grab_focus()
        self.app.status(f"新增：第 {index + 1} 行。填前缀、名称和目标，类型在「类型」下拉里换；"
                        f"点「保存这一条」写进 {self.app.qbar_path}。", GREY)

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
        self.form_index = None
        self._repaint_all()
        if len(self.store):
            self.view.get_selection().select_path(
                Gtk.TreePath.new_from_indices([min(index, len(self.store) - 1)]))
        else:
            self.sync_form_to_row()
        self.app.status("已删除（还没写盘，记得点「保存快速链接」）。", GREY)

    def on_move(self, delta: int) -> None:
        index = self.selected_index()
        if index is None:
            self.app.status("先选中一行再移动。", AMBER)
            return
        rows = SC.swapped(store_rows(self.store), index, index + delta)
        if rows is None:
            self.app.status("已经到顶 / 到底了。", GREY)
            return
        target = index + delta
        self.store.clear()
        self.form_index = None
        for row in rows:
            self.store.append(list(row))
        self.view.get_selection().select_path(Gtk.TreePath.new_from_indices([target]))
        self._repaint_all()
        self.app.status(f"顺序变了（第 {index + 1} 行 ↔ 第 {target + 1} 行）；"
                        f"配置文件里的先后顺序就是表格顺序，写回时原样保留。"
                        f"还没写盘，点「保存快速链接」。", GREY)

    # ------------------------------------------------------------------
    # 实时预览：跟着**表单条**走 —— 输入 `b 关键词` → 最终 URL
    # ------------------------------------------------------------------
    def _update_preview(self) -> None:
        if self._loading:
            return
        fields = self.form_values()
        if not any(fields):
            self.preview_label.set_markup(markup("选中一行（或把表单填完）就能预览。", GREY))
            return
        link = SC.link_from_form(*fields)
        problems = SC.link_problems(link, ())
        if problems:
            self.preview_label.set_markup(markup("这条先改对：" + "；".join(problems), RED))
            return
        text, result = SC.preview_pair(link, self.sample.get_text())
        # ask 的第二项是「网址 \n 两步动作」：拆成两行画，网址走等宽、说明走灰字，
        # 免得那句人话被塞进 <tt> 里变成一条又长又等宽的串（换行在 pango 里不会被 wrap 掉）
        head, sep, tail = result.partition("\n")
        line = (f'输入 <b>{GLib.markup_escape_text(text)}</b> → {VERB[link.resolved_type]} '
                f'<tt>{GLib.markup_escape_text(head)}</tt>')
        if sep:
            line += "\n" + markup(tail, GREY)
        self.preview_label.set_markup(line)


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
        self.raise_switch = Gtk.Switch()
        self.raise_switch.set_halign(Gtk.Align.START)

        for row, (label, widget, key) in enumerate((
                ("默认搜索引擎", self.default_combo, "default_search"),
                ("浏览器命令", self.browser_entry, "browser"),
                ("失焦即隐藏", self.focus_switch, "hide_on_focus_out"),
                ("先激活浏览器", self.raise_switch, "raise_browser"))):
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
        self.raise_switch.set_active(bool(getattr(cfg, "raise_browser", True)))
        self._loading = False

    def memory_values(self) -> tuple[str, str, bool, bool]:
        """(default_search, browser, hide_on_focus_out, raise_browser) —— 界面值收成可写盘的四元组。"""
        index = self.default_combo.get_active()
        if 0 <= index < len(self._search_triggers):
            default_search = self._search_triggers[index]
        else:
            # 链接表里一条 search 都没有：别把不存在的 trigger 写进配置
            default_search = self.app.cfg.default_search
        return (default_search, self.browser_entry.get_text().strip(),
                self.focus_switch.get_active(), self.raise_switch.get_active())

    def _mark_dirty(self) -> None:
        if getattr(self, "_loading", False):
            return                        # 程序自己在回填，不是用户改的
        if self.memory_values() != (self.app.cfg.default_search, self.app.cfg.browser,
                                    self.app.cfg.hide_on_focus_out,
                                    bool(getattr(self.app.cfg, "raise_browser", True))):
            self.app.status("通用设置改了但还没写盘 —— 点「保存通用设置」。", AMBER)


# ===========================================================================
# 第③页：按键映射
# ===========================================================================
class MappingPage(Gtk.Box):
    """上方**只读**生效表（层/键/动作/说明/来源）+ 表格下方的**编辑条**。

    编辑条永远显示"这一格是谁 + 它当前的动作"，并且「这一格要改的是 `capl / t`」在编辑条
    和状态栏**两边都写出来**（防写错键的保险，2026-10-15 定案要加）。改完立刻写覆盖层文件、
    立刻刷表（来源变「覆盖」），但**不碰系统** —— 生效必须点「应用」。
    """

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
        self.view.get_selection().connect("changed", self._on_selection_changed)
        self.view.connect("row-activated", self._on_row_activated)
        for index, title in enumerate(self.COLUMNS):
            renderer = Gtk.CellRendererText()
            renderer.set_property("editable", False)    # 只读；判据见 tests/test_table_readonly.py
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_min_width(200 if index in (2, 3) else 70)
            column.set_expand(index in (2, 3))
            column.set_cell_data_func(renderer, self._paint_row, index)
            self.view.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(300)
        scroll.add(self.view)
        self.pack_start(scroll, True, True, 0)

        # ------------------------------------------------------------------
        # 编辑条（不参与滚动，所以不可能"落到别的行"）
        # ------------------------------------------------------------------
        editor = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        editor.pack_start(Gtk.Label(label="目标：", xalign=0), False, False, 0)
        self.editor_target = Gtk.Label(xalign=0)
        self.editor_target.set_selectable(True)
        self.editor_target.set_markup(markup("（先在表格里点一行）", GREY))
        editor.pack_start(self.editor_target, False, False, 0)
        self.editor_note = Gtk.Label(xalign=0)
        self.editor_note.set_ellipsize(Pango.EllipsizeMode.END)
        self.editor_note.set_markup(markup("", GREY))
        editor.pack_start(self.editor_note, True, True, 0)
        editor.pack_start(Gtk.Label(label="动作：", xalign=0), False, False, 0)
        self.action_combo = Gtk.ComboBox.new_with_entry()     # 下拉列常用动作 + 可自由输入
        self.action_combo.set_model(self.action_model)
        self.action_combo.set_entry_text_column(0)
        self.action_entry = self.action_combo.get_child()     # ← 双击行要把焦点给它
        self.action_entry.set_width_chars(28)
        self.action_entry.connect("activate", lambda *_a: self.on_commit(None))
        editor.pack_start(self.action_combo, False, False, 0)
        self.commit_button = Gtk.Button(label="改这一格")
        self.commit_button.get_style_context().add_class("suggested-action")
        self.commit_button.connect("clicked", self.on_commit)
        editor.pack_start(self.commit_button, False, False, 0)
        restore = Gtk.Button(label="恢复默认（删这条覆盖）")
        restore.connect("clicked", self.on_restore)
        editor.pack_start(restore, False, False, 0)
        self.pack_start(editor, False, False, 0)

        # ------------------------------------------------------------------
        # 工具条（这些不编辑某一只格子，是对整层/整个系统的动作）
        # ------------------------------------------------------------------
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in (("预览改动（只读）", self.on_preview),
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
            "表格只读：点一行 → 下面编辑条填的就是**那一行**（双击行焦点直接进输入框，"
            "框里按回车 = 点「改这一格」）。「改这一格 / 恢复默认」立刻写覆盖层 "
            f"{self.app.overrides_path} 并刷表，但不碰系统；点「应用」才装进 /etc/keyd 并 reload，"
            "出错自动回滚上一版（备份在 ~/.local/state/capsplus/keyd-conf-backup/）。"
            f"宏硬边界：单条 macro(...) 累计 ≤{SL.MACRO_EVENT_LIMIT} 个按键事件，超了整条静默不发。",
            GREY))
        self.pack_start(hint, False, False, 0)

        self.editor_pair: tuple[str, str] | None = None
        self._loading = False
        self.reload_from_memory()

    # ------------------------------------------------------------------
    # 编辑条 <-> 选中的那一行
    # ------------------------------------------------------------------
    def _on_selection_changed(self, *_args) -> None:
        self.sync_editor()

    def _on_row_activated(self, *_args) -> None:
        """双击行（或键盘回车）：同步编辑条，然后焦点直接进动作输入框。"""
        self.sync_editor()
        self.action_entry.grab_focus()

    def sync_editor(self) -> None:
        """编辑条 = 表格选中那一行。行内容一律由 `SL.editor_binding(表格行, 选中行号)` 现算。"""
        row = SL.editor_binding(store_rows(self.store), self.selected_index())
        self._loading = True
        if row is None:
            self.editor_pair = None
            self.editor_target.set_markup(markup("（先在表格里点一行）", GREY))
            self.editor_note.set_markup(markup("", GREY))
            self.action_entry.set_text("")
        else:
            self.editor_pair = (row["section"], row["key"])
            self.editor_target.set_markup(bold(SL.editor_target(row["section"], row["key"])))
            self.editor_note.set_markup(markup(row["note"], GREY))
            self.action_entry.set_text(row["action"])
        self._loading = False
        self._refresh_target_tooltip()

    def _refresh_target_tooltip(self) -> None:
        target = SL.editor_target(*self.editor_pair) if self.editor_pair else ""
        text = (f"这一格要改的是 {target}：改完立刻写覆盖层（{self.app.overrides_path}），"
                f"但还不生效 —— 点「应用」才写进 /etc/keyd。" if target else
                "先在表格里点一行。")
        self.editor_target.set_tooltip_text(text)

    def checked_pair(self) -> tuple[str, str] | None:
        """要改的是哪一格：编辑条认的 == 表格当前选中的，否则拦住（宁可一格都不改）。"""
        selected = self.selected_pair()
        if self.editor_pair is None:
            self._count_status("先在表格里点一行 —— 得先知道要改的是哪一格。", AMBER)
            return None
        if selected is None:
            self._count_status(SL.desync_problem(SL.editor_target(*self.editor_pair),
                                                 "（没选中任何行）"), RED)
            return None
        if self.editor_pair != selected:
            self._count_status(SL.desync_problem(SL.editor_target(*self.editor_pair),
                                                 SL.editor_target(*selected)), RED)
            return None
        return selected

    def current_action(self, section: str, key: str) -> str:
        for row in self.store:
            if (row[0], row[1]) == (section, key):
                return row[2]
        return ""

    # ------------------------------------------------------------------
    def reload_overrides(self) -> None:
        self.app.ov = MO.load_overrides(self.app.overrides_path, self.app.known_keys)
        self.reload_from_memory()
        if self.app.ov.error:
            self.app.status(f"覆盖层读不了：{self.app.overrides_path} —— {self.app.ov.error}", RED)
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
        else:
            self.sync_editor()          # 选不回来（表空了/那行没了）也要让编辑条跟着清空
        self._count_status()

    def _count_status(self, text: str | None = None, color: str = GREY) -> None:
        overridden = sum(1 for row in self.store if row[4] == "覆盖")
        base = (f"当前生效 {len(self.store)} 条，其中 {overridden} 条来自覆盖层"
                f"（{self.app.overrides_path}）")
        self.app.status((text + "\n" + base) if text else base + "。", color)

    def selected_index(self) -> int | None:
        """表格当前选中的**行号**（编辑条靠它现算"这一格是谁"）。"""
        model, it = self.view.get_selection().get_selected()
        return model.get_path(it).get_indices()[0] if it is not None else None

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
    # 改这一格 / 恢复默认（都**立刻写覆盖层文件**，都不碰系统）
    # ------------------------------------------------------------------
    def on_commit(self, _button):
        pair = self.checked_pair()
        if pair is None:
            return
        section, key = pair
        target = SL.editor_target(section, key)
        action = self.action_entry.get_text().strip()
        problem = SL.action_problem(action, key, section, self.app.known_keys)
        if problem:
            # 非法就保持原值：表格里不许留坏数据，否则下一次渲染会把整层带崩
            self._count_status(f"{target} 没改成「{action}」：{problem}", RED)
            return
        if action == self.current_action(section, key):
            self._count_status(f"{target} 已经是「{action}」（覆盖层没变化）。", GREY)
            return
        try:
            values = SL.set_override(self.app.ov.values, section, key, action,
                                     self.app.known_keys)
        except ValueError as exc:
            self._count_status(f"{target} 没改：{exc}", RED)
            return
        back_to_default = action == self._default_action(section, key)
        if back_to_default:
            # 改回和默认一样 = 这条覆盖没意义，顺手删掉（覆盖层只存"真的改过的"）
            values = SL.delete_override(values, section, key)
        if not self.write_overrides(values):
            return
        self.app.ov.values = values
        self.reload_from_memory()
        hint = SL.override_toml_hint(section)
        warning = SL.macro_warning(action)
        if warning:
            self._count_status(warning, AMBER)
            return
        message = (SL.restore_status(section, key) if back_to_default
                   else SL.commit_status(section, key, action))
        self._count_status(f"{message}\n{hint}" if hint else message, BLUE)

    def _default_action(self, section: str, key: str) -> str:
        for row in binding_rows(section):          # 不传 merged = 仓库默认表
            if row[0] == key:
                return row[1]
        return ""

    def _dirty(self) -> bool:
        """内存里的覆盖层和磁盘上那份不一样吗？"""
        on_disk = MO.load_overrides(self.app.overrides_path, self.app.known_keys).values
        return on_disk != self.app.ov.values

    def write_overrides(self, values: dict[str, dict[str, str]]) -> bool:
        """把覆盖层写到文件（写之前快照原件）。失败 = 红字 + 返回 False，内存态不动。"""
        try:
            SL.save_overrides_with_snapshot(values, self.app.overrides_path,
                                            self.app.backup_dir)
        except OSError as exc:
            self.app.status(f"写不了 {self.app.overrides_path}：{exc}", RED)
            return False
        return True

    def on_restore(self, _button):
        pair = self.checked_pair()
        if pair is None:
            return
        section, key = pair
        target = SL.editor_target(section, key)
        if self.app.ov.get(section, key) is None:
            self._count_status(f"{target} 本来就没有覆盖，已经是仓库默认。", GREY)
            return
        values = SL.delete_override(self.app.ov.values, section, key)
        if not self.write_overrides(values):
            return
        self.app.ov.values = values
        self.reload_from_memory()
        self._count_status(SL.restore_status(section, key), BLUE)

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
        """覆盖层已经在「改这一格」时写进文件了；这里只走现有 apply 流程
        （备份 / reload / 抓 journal / 出错自动回滚），**真机生效只有这个按钮**。"""
        if self._dirty():                     # 别的路径改过内存（例如手改文件后重读失败）
            if not self.write_overrides(self.app.ov.values):
                return
        # 传 overrides_path：渲染用的必须就是上面刚写下去的那份（临时目录演练也要真）
        ok, text = SL.run_apply(dry_run=dry_run, overrides_path=self.app.overrides_path,
                                known_keys=self.app.known_keys)
        self._count_status(text, BLUE if ok else RED)
        if ok and not dry_run:
            self.reload_overrides()


# ===========================================================================
# 第④页：服务（翻译 / OCR / 语音识别 的"服务在哪"可配置）
#
#   数据**不是这里另算的**：配置读写走 `capsplus/services.py`，
#   校验与表单往返走 `capsplus/settings_logic.py`（不 import gi，有单测），
#   这一节只负责摆控件 —— 与其它三页同一条规矩。
#
#   **凭据纪律**（这一页最容易出事的地方）：
#     · 表格里那列写的是「已配置（来自 环境变量 XXX）」，**永远不是 key 的值**；
#     · 明文 key 的输入框默认**不显示**（`visibility=False`），而且**永远是空的**
#       —— 留空 = 保持原样，填了 = 换掉，勾「清掉明文 key」= 真的删掉；
#     · 端点改名时旧端点的明文 key 必须**跟着搬过去**（按位置找旧端点，
#       不按名字 —— 按名字就找不到它，key 被悄悄丢掉 = 静默删凭据）。
# ===========================================================================
class ServiceBlock(Gtk.Box):
    """一栏能力（翻译 / OCR 识别 / 语音识别）：用法下拉 + 语言输入 + **只读**端点表。

    表格只读这条老规矩照样适用（见文件头）：编辑走页面下方那条**共用**的编辑条，
    所以三个能力共用一套编辑控件，也就不会出现"三份表单各写错一处"。
    """

    COLUMNS = ("名字", "本机/远程", "类型", "地址", "模型", "凭据来源", "超时")

    def __init__(self, page: "ServicesPage", capability: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.page = page
        self.capability = capability
        self._loading = False
        self._modes: list[str] = []

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top.pack_start(Gtk.Label(label="用法：", xalign=0), False, False, 0)
        self.mode_combo = Gtk.ComboBoxText()
        # ComboBoxText 没有 set_width_chars（那是 Entry 的）—— 用尺寸请求代替
        self.mode_combo.set_size_request(200, -1)
        self.mode_combo.connect("changed", lambda *_a: self._on_mode_changed())
        top.pack_start(self.mode_combo, False, False, 0)
        self.lang_label = Gtk.Label(xalign=0)
        top.pack_start(self.lang_label, False, False, 0)
        self.lang_entry = Gtk.Entry()
        self.lang_entry.set_width_chars(10)
        top.pack_start(self.lang_entry, False, False, 0)
        top.pack_end(Gtk.Box(), True, True, 0)
        self.pack_start(top, False, False, 0)

        # 端点表：只读（判据见 tests/test_table_readonly.py）
        self.store = Gtk.ListStore(str, str, str, str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_visible(True)
        # 每列的最小宽度：别把「凭据来源」那一列挤没了 ——
        # 那一列写的是「已配置（来自 凭据文件 … 的 KEY）」，被截断就等于没显示。
        widths = (90, 90, 100, 220, 130, 260, 70)
        for index, title in enumerate(self.COLUMNS):
            renderer = Gtk.CellRendererText()
            renderer.set_property("editable", False)    # 只读
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_min_width(widths[index])
            column.set_expand(index in (3, 5))      # 地址与凭据来源各占一份富余
            self.view.append_column(column)
        self.view.get_selection().connect("changed", self._on_selection_changed)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(140)
        scroll.add(self.view)
        self.pack_start(scroll, True, True, 0)

    # ------------------------------------------------------------------
    def reload_from_memory(self) -> None:
        """按内存里的配置重画这一栏（表格 + 用法下拉 + 语言框）。"""
        cap = self.page.capability(self.capability)
        self._loading = True
        self.store.clear()
        if cap is None:
            self.mode_combo.remove_all()
            self._modes = []
            self.lang_label.set_text("")
            self.lang_entry.set_text("")
            self._loading = False
            return
        for ep in cap.ordered_endpoints():
            self.store.append([
                ep.name,
                SL.KIND_LABELS.get(ep.kind, ep.kind),
                SL.STYLE_LABELS.get(ep.style, ep.style),
                ep.base_url or "（没填地址）",
                ep.model or "—",
                SL.credential_source_short(ep),    # ← 只有"来自哪"，没有值（短版，列才放得下）
                f"{ep.timeout_s:g} 秒" if ep.timeout_s else "—",
            ])
        self._rebuild_mode(cap)
        if self.capability == "translate":
            self.lang_label.set_text("目标语言：")
            self.lang_entry.set_text(cap.target)
        elif self.capability == "asr":
            self.lang_label.set_text("识别语言：")
            self.lang_entry.set_text(cap.lang)
        else:
            self.lang_label.set_text("")
            self.lang_entry.set_text("")
        self._loading = False

    def _rebuild_mode(self, cap) -> None:
        """用法下拉 = auto + 每个端点（**顺序 = auto 的优先级**）。"""
        self.mode_combo.remove_all()
        self._modes = []
        self.mode_combo.append_text("auto（按顺序挨个试）")
        self._modes.append(SV.MODE_AUTO)
        for ep in cap.ordered_endpoints():
            self.mode_combo.append_text(ep.name)
            self._modes.append(ep.name)
        active = self._modes.index(cap.mode) if cap.mode in self._modes else 0
        self.mode_combo.set_active(active)

    def mode_value(self) -> str:
        index = self.mode_combo.get_active()
        return self._modes[index] if 0 <= index < len(self._modes) else SV.MODE_AUTO

    def lang_value(self) -> str:
        return self.lang_entry.get_text().strip()

    def _on_mode_changed(self) -> None:
        """换用法只改内存态（不写盘）：写盘只有一个入口 ——「保存服务配置」。"""
        if self._loading:
            return
        self.page.set_mode(self.capability, self.mode_value())

    def _on_selection_changed(self, *_args) -> None:
        model, it = self.view.get_selection().get_selected()
        if it is None:
            return
        index = model.get_path(it).get_indices()[0]
        self.page.select_endpoint(self.capability, index)

    def select_row(self, index: int) -> None:
        if 0 <= index < len(self.store):
            self.view.get_selection().select_path(Gtk.TreePath.new_from_indices([index]))


class ServicesPage(Gtk.Box):
    """第五页：翻译 / OCR / 语音识别 各自用哪台机器上的哪个服务。

    结构（从上到下）：
      · 三个子标签（翻译 / OCR 识别 / 语音识别），每个一栏：用法下拉 + 语言 + 只读端点表；
      · **一条共用的端点编辑条**（表格只读的老规矩：编辑控件不许进表格）；
      · `保存服务配置` `重读文件` `测试连接` + 一个结果框（耗时 + 一行摘要）。
    """

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.app = app
        self.set_border_width(10)
        self.services = SV.load_services(app.services_path)
        self.selected: tuple[str, int] | None = None      # (能力名, 端点下标)
        self._loading = False

        hint = Gtk.Label(xalign=0)
        hint.set_line_wrap(True)
        # ⚠ `markup()` 只转义 + 上色，**不会**把 `**…**` 变粗（pango 不认 markdown）——
        # 要加粗就用 `bold()`，别在 markup 里写星号（页面上会露出星号）。
        hint.set_markup(markup(
            "这一页配的是「翻译 / OCR / 语音识别 各自连哪台机器上的哪个服务」。"
            f"配置文件：{app.services_path}（改完不用重启，各链路每次调用都会重读）。", GREY)
            + bold("凭据只写来源不写值")
            + markup("：环境变量名 / 凭据文件+键 / env 文件+键；"
                     "实在要写明文就用编辑条里那个输入框 —— 它默认不显示，"
                     "而且界面上任何地方都不会把已保存的 key 显示出来。", GREY))
        self.pack_start(hint, False, False, 0)

        self.sub = Gtk.Notebook()
        self.blocks: dict[str, ServiceBlock] = {}
        for name in SV.CAPABILITIES:
            block = ServiceBlock(self, name)
            self.blocks[name] = block
            self.sub.append_page(block, Gtk.Label(label=SV.CAPABILITY_LABELS.get(name, name)))
        self.sub.connect("switch-page", self._on_switch_page)
        self.pack_start(self.sub, True, True, 0)

        # ── 端点编辑条（三个能力共用一条）────────────────────────────────
        editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.editor_target = Gtk.Label(xalign=0)
        self.editor_target.set_selectable(True)
        self.editor_target.set_markup(markup("（先在上面的表里点一个端点，或点「新增端点」）", GREY))
        editor.pack_start(self.editor_target, False, False, 0)

        grid = Gtk.Grid(row_spacing=6, column_spacing=10)
        self.e_name = Gtk.Entry()
        self.e_name.set_width_chars(14)
        self.e_kind = Gtk.ComboBoxText()
        for kind in SL.SERVICE_KINDS:
            self.e_kind.append_text(f"{SL.KIND_LABELS[kind]}（{kind}）")
        self.e_style = Gtk.ComboBoxText()
        for style in SL.SERVICE_STYLES:
            self.e_style.append_text(f"{SL.STYLE_LABELS[style]}（{style}）")
        self.e_url = Gtk.Entry()
        self.e_url.set_width_chars(40)
        self.e_model = Gtk.Entry()
        self.e_model.set_width_chars(18)
        self.e_timeout = Gtk.Entry()
        self.e_timeout.set_width_chars(7)
        self.e_key_env = Gtk.Entry()
        self.e_key_env.set_width_chars(20)
        self.e_cred_file = Gtk.Entry()
        self.e_cred_file.set_width_chars(24)
        self.e_cred_key = Gtk.Entry()
        self.e_cred_key.set_width_chars(22)
        self.e_env_file = Gtk.Entry()
        self.e_env_file.set_width_chars(24)
        self.e_env_key = Gtk.Entry()
        self.e_env_key.set_width_chars(18)
        self.e_plain = Gtk.Entry()
        self.e_plain.set_width_chars(20)
        # 明文 key 的输入框默认**不显示**：它是"只写不读"的，肩并肩看着更安全
        self.e_plain.set_visibility(False)
        self.e_clear = Gtk.CheckButton(label="清掉明文 key")
        self.key_state = Gtk.Label(xalign=0)
        self.key_state.set_markup(markup("凭据：—", GREY))

        for row, fields in enumerate((
                (("名字", self.e_name), ("本机/远程", self.e_kind), ("类型", self.e_style),
                 ("地址", self.e_url)),
                (("模型", self.e_model), ("超时(秒)", self.e_timeout),
                 ("环境变量名", self.e_key_env)),
                (("凭据文件", self.e_cred_file), ("文件里的键", self.e_cred_key),
                 ("env 文件", self.e_env_file), ("env 里的键", self.e_env_key)),
                (("明文 key", self.e_plain), (None, self.e_clear)))):
            column = 0
            for label, widget in fields:
                if label is not None:
                    grid.attach(Gtk.Label(label=label, xalign=0), column, row, 1, 1)
                    column += 1
                grid.attach(widget, column, row, 1, 1)
                column += 1
        # 凭据状态**单独占一行**（放同一行会跟「清掉明文 key」那个复选框重叠 —— 实测截图里撞过）
        self.key_state.set_line_wrap(True)
        self.key_state.set_max_width_chars(110)
        grid.attach(self.key_state, 0, 4, 6, 1)
        editor.pack_start(grid, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.save_endpoint_button = Gtk.Button(label="保存这一条")
        self.save_endpoint_button.get_style_context().add_class("suggested-action")
        self.save_endpoint_button.connect("clicked", self.on_save_endpoint)
        row.pack_start(self.save_endpoint_button, False, False, 0)
        for label, handler in (("新增端点", self.on_add_endpoint),
                               ("删除端点（带确认）", self.on_delete_endpoint)):
            button = Gtk.Button(label=label)
            button.connect("clicked", handler)
            row.pack_start(button, False, False, 0)
        row.pack_end(Gtk.Box(), True, True, 0)
        editor.pack_start(row, False, False, 0)
        self.pack_start(editor, False, False, 0)

        # ── 文件级动作 + 结果框 ───────────────────────────────────────────
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        save = Gtk.Button(label="保存服务配置")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self.on_save)
        bar.pack_start(save, False, False, 0)
        for label, handler in (("重读文件", self.on_reload),
                               ("测试连接", self.on_test)):
            button = Gtk.Button(label=label)
            button.connect("clicked", handler)
            bar.pack_start(button, False, False, 0)
        bar.pack_end(Gtk.Box(), True, True, 0)
        self.pack_start(bar, False, False, 0)

        self.result_view = Gtk.TextView()
        self.result_view.set_editable(False)
        self.result_view.set_cursor_visible(False)
        self.result_view.set_monospace(True)
        self.result_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.result_view.set_left_margin(8)
        self.result_view.set_top_margin(6)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(120)
        scroll.add(self.result_view)
        self.pack_start(scroll, False, False, 0)

        self.reload_from_memory()
        if self.services.error:
            self.append_result(f"! 服务配置读不了，下面显示的是内置默认：\n  {self.services.error}")

    # ------------------------------------------------------------------
    # 内存态
    # ------------------------------------------------------------------
    def capability(self, name: str):
        return self.services.get(name)

    def current_capability(self) -> str:
        """当前子标签对应哪个能力（给「测试连接」用）。"""
        index = self.sub.get_current_page()
        if 0 <= index < len(SV.CAPABILITIES):
            return SV.CAPABILITIES[index]
        return SV.CAPABILITIES[0]

    def reload_from_memory(self) -> None:
        """按内存态重画三个表 + 编辑条（不动文件）。"""
        for block in self.blocks.values():
            block.reload_from_memory()
        if self.selected:
            capability, index = self.selected
            cap = self.capability(capability)
            if cap is not None and 0 <= index < len(cap.ordered_endpoints()):
                self.select_endpoint(capability, index)
                return
        self.clear_editor()

    def clear_editor(self) -> None:
        self.selected = None
        self._loading = True
        for widget in (self.e_name, self.e_url, self.e_model, self.e_timeout,
                       self.e_key_env, self.e_cred_file, self.e_cred_key,
                       self.e_env_file, self.e_env_key, self.e_plain):
            widget.set_text("")
        self.e_kind.set_active(0)
        self.e_style.set_active(0)
        self.e_clear.set_active(False)
        self.key_state.set_markup(markup("凭据：—", GREY))
        self.editor_target.set_markup(markup("（先在上面的表里点一个端点，或点「新增端点」）", GREY))
        self._loading = False

    # ------------------------------------------------------------------
    # 编辑条 ↔ 选中的端点
    # ------------------------------------------------------------------
    def select_endpoint(self, capability: str, index: int) -> None:
        cap = self.capability(capability)
        if cap is None:
            return
        ordered = cap.ordered_endpoints()
        if not (0 <= index < len(ordered)):
            return
        ep = ordered[index]
        self.selected = (capability, index)
        self._loading = True
        self.e_name.set_text(ep.name)
        self.e_kind.set_active(SL.SERVICE_KINDS.index(ep.kind)
                               if ep.kind in SL.SERVICE_KINDS else 0)
        self.e_style.set_active(SL.SERVICE_STYLES.index(ep.style)
                                if ep.style in SL.SERVICE_STYLES else 0)
        self.e_url.set_text(ep.base_url)
        self.e_model.set_text(ep.model)
        self.e_timeout.set_text(f"{ep.timeout_s:g}" if ep.timeout_s else "")
        self.e_key_env.set_text(ep.api_key_env)
        self.e_cred_file.set_text(ep.credentials_file)
        self.e_cred_key.set_text(ep.credentials_key)
        self.e_env_file.set_text(ep.env_file)
        self.e_env_key.set_text(ep.env_key)
        self.e_plain.set_text("")            # ← 明文 key 永远不进编辑条
        self.e_clear.set_active(False)
        self.key_state.set_markup(markup(f"凭据：{SL.credential_state(ep)}", GREY))
        label = SV.CAPABILITY_LABELS.get(capability, capability)
        self.editor_target.set_markup(
            bold(f"{label} / {ep.name}") + markup("　（改的是这一条；点「保存这一条」才进内存）", GREY))
        self._loading = False

    def current_form(self) -> dict:
        """编辑条当前的表单值（明文 key 只从输入框取，**不来自已保存的配置**）。"""
        kind_index = self.e_kind.get_active()
        style_index = self.e_style.get_active()
        return {
            "name": self.e_name.get_text().strip(),
            "kind": SL.SERVICE_KINDS[kind_index] if 0 <= kind_index < len(SL.SERVICE_KINDS)
                    else SL.SERVICE_KINDS[0],
            "style": SL.SERVICE_STYLES[style_index] if 0 <= style_index < len(SL.SERVICE_STYLES)
                     else SL.SERVICE_STYLES[0],
            "base_url": self.e_url.get_text().strip(),
            "model": self.e_model.get_text().strip(),
            "api_key_env": self.e_key_env.get_text().strip(),
            "credentials_file": self.e_cred_file.get_text().strip(),
            "credentials_key": self.e_cred_key.get_text().strip(),
            "env_file": self.e_env_file.get_text().strip(),
            "env_key": self.e_env_key.get_text().strip(),
            "timeout_s": self.e_timeout.get_text().strip(),
            "api_key": self.e_plain.get_text(),      # 只写不读：填了才换
            "clear_key": self.e_clear.get_active(),
        }

    # ------------------------------------------------------------------
    # 增 / 改 / 删
    # ------------------------------------------------------------------
    def on_save_endpoint(self, _button) -> None:
        """把编辑条写进内存态（**不写盘** —— 写盘只有一个入口「保存服务配置」）。"""
        if self.selected is None:
            self.app.status("先在上面的表里点一个端点，或点「新增端点」。", AMBER)
            return
        capability, index = self.selected
        new_services, problem = SL.set_endpoint(self.services, capability, index,
                                                self.current_form())
        if new_services is None:
            self.app.status(f"没改：{problem}", RED)
            return
        self.services = new_services
        self.e_plain.set_text("")            # 存过的明文 key 不留在输入框里
        self.reload_from_memory()
        self.app.status(f"这一条已改好（还在内存里，点「保存服务配置」才写进 "
                        f"{self.app.services_path}）。", BLUE)

    def on_add_endpoint(self, _button) -> None:
        capability = self.current_capability()
        new_services, problem = SL.add_endpoint(self.services, capability)
        if new_services is None:
            self.app.status(f"没新增：{problem}", RED)
            return
        self.services = new_services
        self.reload_from_memory()
        block = self.blocks[capability]
        block.select_row(len(block.store) - 1)
        self.app.status(f"新增了一个端点（还没写盘）：填好名字、地址、凭据来源，"
                        "再点「保存这一条」，最后点「保存服务配置」。", GREY)

    def on_delete_endpoint(self, _button) -> None:
        if self.selected is None:
            self.app.status("先在上面的表里点一个端点。", AMBER)
            return
        capability, index = self.selected
        cap = self.capability(capability)
        if cap is None:
            return
        ordered = cap.ordered_endpoints()
        if not (0 <= index < len(ordered)):
            return
        name = ordered[index].name
        dialog = Gtk.MessageDialog(transient_for=self.app, modal=True,
                                   buttons=Gtk.ButtonsType.YES_NO,
                                   message_type=Gtk.MessageType.QUESTION,
                                   text=f"删除端点「{name}」？")
        dialog.format_secondary_text("只是从这一栏里去掉；点「保存服务配置」才会真的写文件。")
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.YES:
            return
        new_services, problem = SL.delete_endpoint(self.services, capability, name)
        if new_services is None:
            self.app.status(f"没删除：{problem}", RED)
            return
        self.services = new_services
        self.clear_editor()
        self.reload_from_memory()
        self.app.status(f"已删除端点「{name}」（还没写盘，点「保存服务配置」）。", GREY)

    def set_mode(self, capability: str, mode: str) -> None:
        """改这一栏的用法（auto / 直接指定某个端点）。只改内存态。"""
        cap = self.capability(capability)
        if cap is None:
            return
        new_services, problem = SL.set_capability_mode(self.services, capability, mode)
        if new_services is None:
            self.app.status(f"用法没改成「{mode}」：{problem}", RED)
            return
        self.services = new_services

    # ------------------------------------------------------------------
    # 文件级动作
    # ------------------------------------------------------------------
    def on_save(self, _button) -> None:
        """把三个栏的用法与语言收进内存态 → 校验 → 写盘（原子写 + 0600 + 回读自证）。"""
        for name, block in self.blocks.items():
            cap = self.capability(name)
            if cap is None:
                continue
            target = block.lang_value() if name == "translate" else cap.target
            lang = block.lang_value() if name == "asr" else cap.lang
            new_services, problem = SL.set_capability_mode(self.services, name,
                                                           block.mode_value(), target, lang)
            if new_services is None:
                self.app.status(f"没写盘：{SV.CAPABILITY_LABELS.get(name, name)} —— {problem}", RED)
                return
            self.services = new_services
        # 写盘前把原件快照到 state 目录（用户手写的服务配置不许被静默覆盖）
        saved = SL.snapshot_config(self.app.services_path, self.app.backup_dir)
        ok, text = SL.save_services_form(self.services, self.app.services_path)
        if not ok:
            self.app.status(text, RED)
            return
        self.services = SV.load_services(self.app.services_path)
        self.reload_from_memory()
        hint = f"（原件存到 {saved.parent}）" if saved else ""
        self.app.status(f"{text}{hint}；不用重启，各链路每次调用都会重读这个文件。", BLUE)

    def on_reload(self, _button) -> None:
        """从磁盘重读（丢掉没保存的改动 —— 所以要说清楚）。"""
        self.services = SV.load_services(self.app.services_path)
        self.clear_editor()
        self.reload_from_memory()
        if self.services.error:
            self.app.status(f"服务配置读不了，已按内置默认处理：{self.services.error}"
                            "（下一步：改完点「保存服务配置」会写出一份好的）", RED)
        else:
            self.app.status(f"已从 {self.app.services_path} 重读。", GREY)

    # ------------------------------------------------------------------
    # 测试连接（真跑一次往返；**报耗时与一行摘要，不报凭据**）
    # ------------------------------------------------------------------
    def on_test(self, _button) -> None:
        capability = self.current_capability()
        cap = self.capability(capability)
        endpoints = list(cap.ordered_endpoints()) if cap is not None else []
        if not endpoints:
            self.app.status(f"「{SV.CAPABILITY_LABELS.get(capability, capability)}」这一栏"
                            "一个端点都没有 —— 先点「新增端点」。", AMBER)
            return
        self.append_result(f"正在测「{SV.CAPABILITY_LABELS.get(capability, capability)}」"
                           f"这一栏的 {len(endpoints)} 个端点……")
        threading.Thread(target=self._probe, args=(endpoints,), daemon=True).start()

    def _probe(self, endpoints) -> None:
        """探测放线程里：一次往返可能好几秒，堵住主线程窗口就假死了。"""
        lines = []
        for ep in endpoints:
            result = SL.probe_endpoint(ep)
            mark = "✓" if result["ok"] else "✗"
            lines.append(f"{mark} {ep.name} — {result['ms']} ms — {result['summary']}")
            if result["hint"]:
                lines.append(f"    下一步：{result['hint']}")
        GLib.idle_add(self.append_result, "\n".join(lines))

    def append_result(self, text: str) -> bool:
        buffer = self.result_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), (text or "") + "\n")
        return False        # 只跑一次（GLib.idle_add 的约定）

    def _on_switch_page(self, *_args) -> None:
        self.app.status(f"现在看的是「{SV.CAPABILITY_LABELS.get(self.current_capability(), '')}」"
                        "这一栏 —— 点「测试连接」就测这一栏的端点。", GREY)

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
                            state_dir=_arg_value(argv, "--state-dir"),
                            services_path=_arg_value(argv, "--services"))
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
