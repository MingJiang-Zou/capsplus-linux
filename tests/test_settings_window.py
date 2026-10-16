# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""设置窗口的界面测试（**需要能连上显示服务器**，连不上整模块 skip）。

为什么值得用真窗口跑：任务要求"三页里各页控件确实建出来了、表格有行数"，
这在纯逻辑单测里证明不了 —— 这里跑的是同一条代码路径（`show_all()` 真映射上），
顺带把"编辑→校验→写盘→恢复默认→试渲染"整条链在**临时目录**里走一遍，
绝不碰 `~/.config/capsplus/`（两个路径参数 + state_dir 都指到 tmp）。

2026-10-15 方案 A（表格只读 + 编辑条/表单条）之后，这个文件负责的是**行为级**判据：
  ① 两页表格所有列的 renderer `get_property("editable")` 必须全 False；
  ② 单击第 N 行 → 编辑条显示的键名 == 第 N 行的键名（截图那个 bug 的直接判据）；
  ③ 双击行 → 焦点控件是编辑条的输入框，不是表格里的格子；
  ④ 表格滚动之后编辑条一个字都不变（编辑控件不在滚动区域里，物理上不可能脱开）。
源码级那一半（禁用 `CellRendererCombo` / `editable=True`）在 `tests/test_table_readonly.py`。
"""

import ast
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "settings"))   # settings_app 是按目录路径导入的（与 bin/ 同样写法）

try:                                            # 没有 PyGObject / 没有显示服务器就整模块跳过
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
except Exception as exc:                        # noqa: BLE001
    gi = None
    GI_ERROR = str(exc)
else:
    GI_ERROR = ""

os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")

from capsplus import mapping_overrides as MO    # noqa: E402
from capsplus.render import binding_rows        # noqa: E402
from qbar.search_config import (                # noqa: E402
    LINK_TYPES, SearchLink, load_config, render_config_toml,
)

SAMPLE = """# capsplus Qbar 快速链接配置
# 我自己加的说明，写回之后必须还在

[general]
default_search = "b"
browser = ""
hide_on_focus_out = true

[[link]]
trigger = "b"
name = "必应"
type = "search"
url = "https://www.bing.com/search?q={q}"

[[link]]
trigger = "ghp"
name = "GitHub 我的仓库"
type = "url"
url = "https://github.com/pulls"

[[link]]
trigger = "no"
name = "笔记目录"
type = "file"
url = "~/notes"
"""


def imported_modules(path: Path) -> set[str]:
    """一个模块里所有 import 语句的模块名（含函数里的局部 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            for alias in node.names:
                names.add(f"{node.module or ''}.{alias.name}")
    return names


def display_works() -> bool:
    if gi is None:
        return False
    ok, _err = Gtk.init_check()
    return bool(ok)


@unittest.skipUnless(display_works(), f"没有可用的显示服务器（{GI_ERROR or 'Gtk.init_check 失败'}）")
class TestSettingsWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = __import__("settings_app", globals(), locals(), ["SettingsWindow"], 0)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.qbar = self.dir / "qbar.toml"
        self.qbar.write_text(SAMPLE, encoding="utf-8")
        self.overrides = self.dir / "mappings.toml"
        self.state = self.dir / "state"
        self.window = self.app_module.SettingsWindow(qbar_path=self.qbar,
                                                     overrides_path=self.overrides,
                                                     state_dir=self.state)
        self.window.show_all()
        self.pump(0.8, until=self.window.get_mapped)

    def tearDown(self):
        self.window.destroy()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        self._tmp.cleanup()

    def pump(self, seconds: float = 0.5, until=None) -> None:
        """跑一会儿主循环：Wayland 下映射是异步的，不 pump 就读不到 mapped 状态。

        给了 `until` 就一满足就返回（原来固定等满 0.8s，27 个用例 = 21 秒白等）。
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            if until is not None and until():
                break
            time.sleep(0.02)

    def status_text(self) -> str:
        return self.window.statusbar.get_text()

    def status_color(self) -> str:
        return "red" if '#e64545' in (self.window.statusbar.get_label() or "") else \
               ("amber" if '#c98a00' in (self.window.statusbar.get_label() or "") else
                ("blue" if '#1a5fb4' in (self.window.statusbar.get_label() or "") else "grey"))

    # ------------------------------------------------------------------
    # 真起窗
    # ------------------------------------------------------------------
    def test_real_entry_points_import_cleanly(self):
        """**子进程**跑真入口 `bin/capsplus-*`，防"单测全绿但界面打不开"。

        2026-10-15 的翻车实录：给 settings_app 加 `from gi.repository import Gdk` 时漏了
        `gi.require_version("Gdk","3.0")`，入口一跑就 ImportError，用户点图标没反应；
        而 237 条单测全绿 —— 因为测试进程早就 require 过 Gdk 3.0，永远不重演入口那条路径。
        判据：能撑到超时（= 真起了窗口）或干净退出（单实例已在跑）都算过；
        stderr 里出现 Traceback 就是红。
        """
        env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/1000", WAYLAND_DISPLAY="wayland-0")
        for entry in ("capsplus-settings", "capsplus-qbar"):
            path = REPO / "bin" / entry
            self.assertTrue(path.exists(), f"入口不存在：{path}")
            proc = subprocess.Popen([sys.executable, str(path)], cwd=str(REPO), env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:          # 活着到超时 = 起窗成功
                proc.kill(); proc.wait(); continue
            err = (proc.stderr.read() or "")
            self.assertNotIn("Traceback", err, f"{entry} 启动即崩：\n{err[-600:]}")
            self.assertEqual(proc.returncode, 0,
                             f"{entry} 退出码 {proc.returncode}：\n{err[-600:]}")

    # ------------------------------------------------------------------
    # 只读表格 + 编辑条：2026-10-15 治本方案 A 的**行为级**判据
    # ------------------------------------------------------------------
    def all_renderers(self, view) -> list:
        return [cell for column in view.get_columns() for cell in column.get_cells()]

    def test_no_renderer_in_either_table_is_editable(self):
        """行为级①：两页表格**所有列**的 renderer `get_property("editable")` 必须全是 False。
        只要有一个 True，Wayland 下它就是一块不跟滚动走的悬浮编辑框（截图那个 bug 的根）。"""
        for name, view in (("快速链接", self.window.links_page.view),
                           ("按键映射", self.window.mapping_page.view)):
            cells = self.all_renderers(view)
            self.assertTrue(cells, f"{name} 的表格一列都没有")
            bad = [f"{type(c).__name__}@{i}" for i, c in enumerate(cells)
                   if c.get_property("editable")]
            self.assertEqual(bad, [], f"{name} 表格里还有可编辑 renderer：{bad}")

    def test_clicking_row_N_fills_editor_with_row_N(self):
        """行为级② —— **截图那个 bug 的直接判据**：单击第 N 行，编辑条显示的键名必须
        等于第 N 行的键名、框里的动作必须等于第 N 行的动作。逐行都试，错一行就红。"""
        page = self.window.mapping_page
        store = page.store
        for index in (0, 1, len(store) // 2, self.row_index("capl", "t"), len(store) - 1):
            row = store[index]
            page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([index]))
            label = page.editor_target.get_text()
            self.assertEqual(label, f"{row[0]} / {row[1]}",
                             f"单击第 {index + 1} 行（{row[0]}/{row[1]}），"
                             f"编辑条却写着「{label}」")
            self.assertEqual(page.action_entry.get_text(), row[2],
                             f"第 {index + 1} 行动作是「{row[2]}」，编辑条里却是"
                             f"「{page.action_entry.get_text()}」")
            self.assertEqual(page.selected_pair(), (row[0], row[1]))
            self.assertEqual(page.editor_pair, (row[0], row[1]))

    def test_double_click_focuses_the_action_entry(self):
        """双击行 → 焦点直接进动作输入框（不用再去摸表格里的格子）。"""
        page = self.window.mapping_page
        index = self.row_index("capl", "t")
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([index]))
        page.view.emit("row-activated", Gtk.TreePath.new_from_indices([index]),
                       page.view.get_column(2))
        self.pump(0.3, until=lambda: self.window.get_focus() is page.action_entry)
        self.assertIs(self.window.get_focus(), page.action_entry,
                      "双击之后焦点不在编辑条的输入框上")

    def test_enter_in_entry_is_the_same_as_the_button(self):
        """输入框里按回车 == 点「改这一格」（立刻写覆盖层，不碰系统）。"""
        page = self.window.mapping_page
        index = self.row_index("capl", "e")
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([index]))
        page.action_entry.set_text("pageup")
        page.action_entry.emit("activate")
        self.assertEqual(self.window.ov.get("capl", "e"), "pageup")
        self.assertEqual(page.store[self.row_index("capl", "e")][4], "覆盖")
        self.assertIn('e = "pageup"', self.overrides.read_text(encoding="utf-8"))
        # 提交后编辑条跟着刷成新值（不许停在旧值上，那样下一次提交就会写错值）
        self.assertEqual(page.action_entry.get_text(), "pageup")
        self.assertEqual(page.editor_pair, ("capl", "e"))
        # 编辑条在滚动区**外面**：表格滚到底，它仍然整块待在视口下方
        # （断言"y 一动不动"是错的 —— 滚到底会出现横向滚动条，整块布局合法地缩小 23px；
        #  真正不能接受的是"编辑条跑到某一行上面去"，所以断言结构与相对位置。）
        scroll = page.view.get_parent()
        self.assertFalse(page.action_combo.is_ancestor(scroll),
                         "编辑条的 combo 在 ScrolledWindow 里面 = 老 bug 的结构")
        adjustment = scroll.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        self.pump(0.2)
        area = scroll.get_allocation()
        combo_a = page.action_combo.get_allocation()
        self.assertGreaterEqual(combo_a.y, area.y + area.height - 1,
                                f"滚动后编辑条(y={combo_a.y})不再位于视口下方"
                                f"(视口底 {area.y + area.height}) = 可能压到别的行")
        self.assertEqual(page.editor_target.get_text(), "capl / e", "滚动后编辑条还认这一格")

    def test_scrolling_the_table_cannot_move_the_editor(self):
        """截图那一步的等价复现：选中 `capl / t` → 把表格滚到底再滚回来 ——
        编辑条**一个字都不该变**，因为表格里根本没有会脱开的编辑框。
        （比上一轮"滚动即收下拉"强：那条只覆盖下拉，输入框照样脱开。）"""
        page = self.window.mapping_page
        index = self.row_index("capl", "t")
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([index]))
        before = (page.editor_target.get_text(), page.action_entry.get_text())
        self.assertEqual(before[0], "capl / t", "用例前提：先选中 capl/t")
        adjustment = page.view.get_parent().get_vadjustment()
        adjustment.set_value(adjustment.get_upper())        # 真滚到底
        self.pump(0.2, until=lambda: adjustment.get_value() >= adjustment.get_upper() - 1)
        adjustment.set_value(adjustment.get_lower())        # 再滚回顶
        self.pump(0.2)
        self.assertEqual((page.editor_target.get_text(), page.action_entry.get_text()), before,
                         "滚动之后编辑条换了行 —— 这正是被治本方案 A 消灭的那类 bug")
        self.assertEqual(page.selected_pair(), ("capl", "t"))
        self.assertEqual(page.editor_pair, ("capl", "t"))

    def test_editor_and_table_disagreement_is_refused(self):
        """防写错键的保险丝：编辑条认的格子 != 表格选中的格子 → 红字拦住，一格都不写。"""
        page = self.window.mapping_page
        page.view.get_selection().select_path(
            Gtk.TreePath.new_from_indices([self.row_index("capl", "t")]))
        page.editor_pair = ("capl", "k")        # 人为制造"脱开"（老 bug 的形状）
        page.action_entry.set_text("pageup")
        page.on_commit(None)
        self.assertEqual(self.status_color(), "red")
        self.assertIn("capl / t", self.status_text())
        self.assertIn("capl / k", self.status_text())
        self.assertIsNone(self.window.ov.get("capl", "t"), "拦住了还写了 t = 判据失败")
        self.assertIsNone(self.window.ov.get("capl", "k"), "拦住了还写了 k = 静默改错键")
        self.assertFalse(self.overrides.exists(), "拦住之后不该有覆盖层文件")

    def edit_action(self, section: str, key: str, action: str):
        """新交互的等价写法：单击那一行 → 编辑条里打字 → 点「改这一格」。
        （旧写法 `page._on_action_edited(renderer, path, text)` 是 in-cell 编辑，已废除。）"""
        page = self.window.mapping_page
        page.view.get_selection().select_path(
            Gtk.TreePath.new_from_indices([self.row_index(section, key)]))
        page.action_entry.set_text(action)
        page.on_commit(page.commit_button)
        return page

    # ------------------------------------------------------------------
    # 真起窗
    # ------------------------------------------------------------------
    def test_window_is_really_mapped(self):
        display = Gtk.Window.get_display(self.window) if hasattr(Gtk.Window, "get_display") else None
        from gi.repository import Gdk
        name = Gdk.Display.get_default().get_name()
        self.assertTrue(self.window.get_realized(), "窗口没 realize = 根本没连上显示服务器")
        self.assertTrue(self.window.get_mapped(), "show_all() 之后没 mapped = 没真起窗")
        self.assertTrue(self.window.get_visible())
        self.assertIn("wayland", name.lower())
        self.assertTrue(display is None or True)

    def test_three_pages_exist_with_the_right_titles(self):
        labels = [self.window.notebook.get_tab_label(self.window.notebook.get_nth_page(i))
                  .get_text() for i in range(self.window.notebook.get_n_pages())]
        self.assertEqual(labels, ["快速链接", "通用", "按键映射"])
        self.assertIsInstance(self.window.links_page, self.app_module.LinksPage)
        self.assertIsInstance(self.window.general_page, self.app_module.GeneralPage)
        self.assertIsInstance(self.window.mapping_page, self.app_module.MappingPage)

    # ------------------------------------------------------------------
    # 第①页
    # ------------------------------------------------------------------
    def test_links_table_rows_match_the_config(self):
        store = self.window.links_page.store
        self.assertEqual(len(store), 3)
        self.assertEqual([row[0] for row in store], ["b", "ghp", "no"])
        self.assertEqual([row[3] for row in store], ["网站内搜索", "固定网址", "文件或目录"])
        # 列：前缀 / 名称 / 类型 / 目标 / 备注（表格只读，备注列显示现算的校验结果）
        titles = [c.get_title() for c in self.window.links_page.view.get_columns()]
        self.assertEqual(titles, ["前缀", "名称", "类型", "目标", "备注"])

    def test_selecting_row_fills_the_form_with_that_row(self):
        """行为级（链接页版）：选中第 N 行 → 表单四个框就是第 N 行的四个值，错一行就红。"""
        page = self.window.links_page
        for index in range(len(page.store)):
            row = page.store[index]
            page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([index]))
            self.assertEqual(page.form_values(), (row[0], row[1], row[2], row[4]),
                             f"选中第 {index + 1} 行，表单填的却是 {page.form_values()}")
            self.assertEqual(page.form_index, index)
        page.view.get_selection().unselect_all()
        self.assertEqual(page.form_values(), ("", "", "", ""), "没选中就该清空，别留残影")
        self.assertIsNone(page.form_index)

    def test_double_click_focuses_the_clicked_field(self):
        """双击不同列 → 焦点落到对应的表单框（并且**一定不在表格里面**）。"""
        page = self.window.links_page
        columns = page.view.get_columns()
        for position, expect in ((0, page.f_trigger), (1, page.f_name),
                                 (3, page.f_target), (4, page.f_target)):
            page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([1]))
            page.view.emit("row-activated", Gtk.TreePath.new_from_indices([1]), columns[position])
            self.pump(0.3, until=lambda w=expect: self.window.get_focus() is w)
            self.assertIs(self.window.get_focus(), expect,
                          f"双击第 {position + 1} 列（{columns[position].get_title()}）"
                          f"之后焦点不在预期输入框")

    def test_form_typing_updates_preview_live(self):
        """实时预览跟着**表单条**走：改目标/换类型立刻重算，不用先保存。"""
        page = self.window.links_page
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([0]))
        page.sample.set_text("keyd")
        self.assertIn("bing.com/search?q=keyd", page.preview_label.get_text())
        page.f_target.set_text("https://example.com/s?x={q}")
        self.assertIn("example.com/s?x=keyd", page.preview_label.get_text())
        page.f_type.set_active(LINK_TYPES.index("url"))     # url 不许带 {q} → 预览当场说哪儿错
        self.assertIn("固定网址不许带", page.preview_label.get_text())

    def test_duplicate_prefix_in_form_blocks_save(self):
        """前缀和别的行撞了 → 红字拦下，**不许写盘**（文件一个字节都不该变）。"""
        page = self.window.links_page
        before = self.qbar.read_text(encoding="utf-8")
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([0]))
        page.f_trigger.set_text("ghp")                       # 和第 2 行撞
        page.on_save_row(Gtk.Button())
        self.assertEqual(self.status_color(), "red")
        self.assertIn("trigger 重复", self.status_text())
        self.assertEqual(self.qbar.read_text(encoding="utf-8"), before, "拦下了还写盘 = 判据失败")

    def test_save_this_row_writes_the_file_and_keeps_comments(self):
        page = self.window.links_page
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([1]))
        page.f_name.set_text("我的仓库")
        page.on_save_row(Gtk.Button())
        self.assertEqual(self.status_color(), "blue")
        back = load_config(self.qbar, create=False)
        self.assertEqual([l.name for l in back.links], ["必应", "我的仓库", "笔记目录"])
        text = self.qbar.read_text(encoding="utf-8")
        self.assertIn("我自己加的说明，写回之后必须还在", text)   # 开头注释仍原样带回去

    def test_preview_shows_input_and_final_url(self):
        page = self.window.links_page
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([0]))
        page.sample.set_text("keyd 配置")
        self.pump(0.1)
        text = page.preview_label.get_text()
        self.assertIn("输入 b keyd 配置", text)
        self.assertIn("https://www.bing.com/search?q=keyd+%E9%85%8D%E7%BD%AE", text)

    def test_preview_for_file_row_shows_expanded_path(self):
        page = self.window.links_page
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([2]))
        self.pump(0.1)
        text = page.preview_label.get_text()
        self.assertIn(str(Path.home() / "notes"), text)

    def test_duplicate_trigger_is_marked_red_and_blocks_saving(self):
        page = self.window.links_page
        page.store[1][0] = "b"                     # 第二行的前缀改成和第一行一样
        page._repaint_all()
        self.assertIn("trigger 重复", page.store[1][5])
        self.assertIn("trigger 重复", page.store[1][6])   # 备注列 = 表格里看得见的那一列
        saved = self.window.save_qbar()
        self.assertFalse(saved, "trigger 重复必须拦住写盘")
        self.assertEqual(self.status_color(), "red")
        self.assertIn("重复", self.status_text())

    def test_add_then_fill_form_and_save_writes_the_new_link(self):
        """「新增」→ 焦点在前缀框 → 表单填完 → 「保存这一条」写文件（顺序仍按表格）。"""
        page = self.window.links_page
        page.on_add(Gtk.Button())
        self.assertEqual(len(page.store), 4)
        self.assertEqual(page.store[3][1], "新链接")
        # 新增那行还没填 → 校验必须已经在报"缺少 trigger"
        self.assertIn("缺少 trigger", page.store[3][5])
        self.assertEqual(page.form_values(), ("", "新链接", "url", ""))
        self.pump(0.3, until=lambda: self.window.get_focus() is page.f_trigger)
        self.assertIs(self.window.get_focus(), page.f_trigger, "新增之后焦点要在前缀框")
        page.f_trigger.set_text("zz")
        page.f_target.set_text("https://zz.test/?q={q}")
        page.f_type.set_active(LINK_TYPES.index("search"))
        page.on_save_row(Gtk.Button())
        self.assertEqual(self.status_color(), "blue")
        back = load_config(self.qbar, create=False)
        self.assertEqual([l.trigger for l in back.links], ["b", "ghp", "no", "zz"])
        self.assertEqual(back.links[3].resolved_type, "search")

    def test_move_and_delete_with_confirmation(self):
        page = self.window.links_page
        page.store[2][0] = "zz"
        page._repaint_all()
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([2]))
        page.on_move(-1)
        self.assertEqual(page.store[1][0], "zz")
        self.assertEqual(page.store[2][0], "ghp")
        page.on_move(-1)                                  # 到顶之后不许越界
        self.assertEqual(page.store[0][0], "zz")
        page.on_move(-1)
        self.assertEqual(page.store[0][0], "zz")
        self.assertIn("到顶", self.status_text())
        answers = iter([Gtk.ResponseType.NO, Gtk.ResponseType.YES])
        original = Gtk.MessageDialog.run
        Gtk.MessageDialog.run = lambda self: next(answers)   # 删除必须带确认
        try:
            page.on_delete(Gtk.Button())
            self.assertEqual(len(page.store), 3, "确认框回答「否」就不该删")
            page.on_delete(Gtk.Button())
            self.assertEqual([row[0] for row in page.store], ["b", "ghp"])
        finally:
            Gtk.MessageDialog.run = original
        self.assertEqual(self.status_color(), "grey")
        self.assertIn("已删除", self.status_text())

    def test_saving_writes_toml_and_keeps_leading_comments(self):
        page = self.window.links_page
        page.store[0][1] = "必应搜索"
        page._repaint_all()
        self.assertTrue(self.window.save_qbar())
        back = load_config(self.qbar, create=False)
        self.assertEqual(back.links[0].name, "必应搜索")
        self.assertEqual(back.links[2].resolved_type, "file")
        text = self.qbar.read_text(encoding="utf-8")
        self.assertIn("我自己加的说明，写回之后必须还在", text)
        self.assertEqual(self.status_color(), "blue")
        self.assertIn("已写入", self.status_text())
        # 写盘前原件进了 state 目录（不是 .bak，不留就地副本）
        snapshots = sorted((self.state / "config-backup").iterdir())
        self.assertTrue(snapshots and (snapshots[-1] / "qbar.toml").exists())

    # ------------------------------------------------------------------
    # 第②页
    # ------------------------------------------------------------------
    def test_general_page_lists_only_search_links(self):
        page = self.window.general_page
        self.assertEqual(page._search_triggers, ["b"])
        self.assertTrue(page.focus_switch.get_active())
        self.assertEqual(page.memory_values(), ("b", "", True))

    def test_general_switch_and_entry_write_through(self):
        page = self.window.general_page
        page.focus_switch.set_active(False)
        page.browser_entry.set_text("firefox --new-tab {url}")
        self.assertTrue(self.window.save_qbar())
        back = load_config(self.qbar, create=False)
        self.assertIs(back.hide_on_focus_out, False)
        self.assertEqual(back.browser, "firefox --new-tab {url}")

    # ------------------------------------------------------------------
    # 第③页
    # ------------------------------------------------------------------
    def test_mapping_table_shows_both_layers(self):
        store = self.window.mapping_page.store
        expected = len(binding_rows("capl")) + len(binding_rows("capl+alt"))
        self.assertEqual(len(store), expected)
        self.assertEqual({row[0] for row in store}, {"capl", "capl+alt"})
        self.assertEqual({row[4] for row in store}, {"默认"}, "没改过就该全是默认")

    def row_index(self, section: str, key: str) -> int:
        for index, row in enumerate(self.window.mapping_page.store):
            if (row[0], row[1]) == (section, key):
                return index
        raise AssertionError(f"表格里找不到 [{section}] {key}")

    def test_edit_action_marks_override_and_writes_quoted_toml(self):
        page = self.edit_action("capl", "e", "pageup")
        self.assertEqual(page.store[self.row_index("capl", "e")][4], "覆盖")
        self.assertEqual(self.window.ov.get("capl", "e"), "pageup")
        # 「改这一格」立刻写覆盖层文件，但不碰系统
        self.assertIn('e = "pageup"', self.overrides.read_text(encoding="utf-8"))
        # 试渲染（不动系统）
        ok, text = self.app_module.SL.run_apply(dry_run=True, overrides_path=self.overrides,
                                                known_keys=self.window.known_keys)
        self.assertTrue(ok, text)
        page.on_apply(dry_run=True)
        self.assertIn("dry-run", self.status_text())
        # 预览改动要说清是哪一条变了（keyd 段名写法）
        page.on_preview(Gtk.Button())
        self.assertIn("[capl] e: up -> pageup", self.status_text())

    def test_status_names_the_exact_cell_being_changed(self):
        """「这一格要改的是 `capl / t`」必须**编辑条和状态栏两边都写出来**（防写错键的保险）。"""
        page = self.edit_action("capl", "t", "pageup")
        self.assertEqual(page.editor_target.get_text(), "capl / t")
        text = self.status_text()
        self.assertIn("已把 capl / t 改成 pageup", text)
        self.assertIn("还没生效", text)
        self.assertIn("应用", text)
        self.assertEqual(self.status_color(), "blue")
        # 编辑条旁边还带着这一格的说明（截图里那行"大段上/下滚…"）
        self.assertIn("大段上/下滚", page.editor_note.get_text())

    def test_illegal_action_is_refused_and_row_unchanged(self):
        page = self.window.mapping_page
        index = self.row_index("capl", "e")
        before = page.store[index][2]
        self.edit_action("capl", "e", "up down")
        self.assertEqual(page.store[self.row_index("capl", "e")][2], before, "非法值不许留在表里")
        self.assertEqual(self.status_color(), "red")
        self.assertIn("空格", self.status_text())
        self.assertIsNone(self.window.ov.get("capl", "e"))
        self.assertFalse(self.overrides.exists(), "非法值不许写盘")
        self.assertEqual(index, self.row_index("capl", "e"))

    def test_unknown_key_action_is_refused_when_keyd_present(self):
        page = self.window.mapping_page
        if self.window.known_keys is None:
            self.skipTest("本机没有 keyd.rvaiya list-keys，键名校验本来就该跳过")
        index = self.row_index("capl", "e")
        self.edit_action("capl", "e", "nosuchkey")
        self.assertEqual(self.status_color(), "red")
        self.assertIn("不认识的键名", self.status_text())
        self.assertEqual(page.store[index][2], "up")

    def test_long_macro_warns_in_amber(self):
        self.edit_action("capl", "e", "macro(" + " ".join(["up"] * 16) + ")")
        self.assertEqual(self.status_color(), "amber")
        self.assertIn("16", self.status_text())
        self.assertIn("静默", self.status_text())

    def test_restoring_default_removes_the_override(self):
        page = self.edit_action("capl", "t", "up")
        self.assertEqual(page.store[self.row_index("capl", "t")][4], "覆盖")
        page.on_restore(Gtk.Button())
        self.assertIsNone(self.window.ov.get("capl", "t"))
        self.assertEqual(page.store[self.row_index("capl", "t")][4], "默认")
        # 文件里也真删掉了（不是只改内存）
        disk = MO.load_overrides(self.overrides, self.window.known_keys)
        self.assertIsNone(disk.get("capl", "t"), disk.errors)
        self.assertIn("capl / t", self.status_text())

    def test_editing_back_to_default_value_drops_the_override(self):
        """改成和默认一模一样的值 = 这条覆盖没有意义，直接删掉（覆盖层只存真改过的）。"""
        page = self.window.mapping_page
        index = self.row_index("capl", "e")
        default = page.store[index][2]
        self.edit_action("capl", "e", "pageup")
        self.edit_action("capl", "e", default)
        self.assertIsNone(self.window.ov.get("capl", "e"))
        self.assertEqual(page.store[self.row_index("capl", "e")][4], "默认")

    def test_editing_the_same_value_writes_nothing_new(self):
        """值没变 = 不重写文件（覆盖层只存"真的改过的"，也不该刷一堆快照）。"""
        page = self.edit_action("capl", "e", "pageup")
        before = self.overrides.read_text(encoding="utf-8")
        self.edit_action("capl", "e", "pageup")
        self.assertEqual(self.status_color(), "grey")
        self.assertIn("已经是", self.status_text())
        self.assertEqual(self.overrides.read_text(encoding="utf-8"), before)

    def test_apply_writes_overrides_file_with_quoted_alt_section(self):
        page = self.edit_action("capl+alt", "b", "up")
        # 界面显示用 keyd 写法（capl+alt / b），同时把"文件里为什么带引号"说清楚
        self.assertIn("capl+alt / b", page.editor_target.get_text())
        self.assertIn('["capl+alt"]', self.status_text())
        page.on_apply(dry_run=True)           # 覆盖层已写 + 试渲染（不动 /etc/keyd）
        text = self.overrides.read_text(encoding="utf-8")
        self.assertIn('["capl+alt"]', text)             # 写盘走 toml_table()：+ 不是合法裸键
        self.assertNotIn("\n[capl+alt]\n", text)
        self.assertIn('b = "up"', text)

    def test_main_section_cannot_be_reached_from_the_table(self):
        """表格里出现的段只有 ALLOWED_SECTIONS —— [main] 的 capslock 不许出现在界面上。"""
        sections = {row[0] for row in self.window.mapping_page.store}
        self.assertEqual(sections, set(MO.ALLOWED_SECTIONS))
        self.assertNotIn("main", sections)

    def test_broken_override_file_on_disk_is_reported_not_fatal(self):
        self.overrides.write_text('[capl]\ne = "up down"\n', encoding="utf-8")
        page = self.window.mapping_page
        page.reload_overrides()
        self.assertIn("覆盖层里有几条被跳过", self.status_text())
        self.assertEqual(len(page.store), len(binding_rows("capl")) + len(binding_rows("capl+alt")))


class TestNoDisplayHelpers(unittest.TestCase):
    """没有显示服务器也能跑的部分：设置窗口用的纯函数不许依赖 gi。"""

    def test_the_two_pure_layers_really_do_not_import_gi(self):
        """docstring 里就写着"刻意不 import gi"，所以只能按 AST 的 import 语句判断。"""
        for rel in ("capsplus/settings_logic.py", "qbar/search_config.py", "qbar/logic.py"):
            names = imported_modules(REPO / rel)
            self.assertNotIn("gi", names, f"{rel} 里出现了 import gi")
            self.assertFalse(any(n.startswith("gi.repository") for n in names), f"{rel}: {names}")

    def test_render_config_toml_output_reparses(self):
        links = [SearchLink("b", "必应", "https://b.test/?q={q}", "search"),
                 SearchLink("co", "代码", "code --diff {q}", "app")]
        self.assertEqual(load_config_write_back(render_config_toml(links, "b", "", True)),
                         [(l.trigger, l.resolved_type) for l in links])


def load_config_write_back(text: str):
    """把 TOML 文本落到临时文件再读回来，返回 (trigger, 类型) 列表。"""
    import tempfile as tf
    with tf.TemporaryDirectory() as tmp:
        path = Path(tmp) / "qbar.toml"
        path.write_text(text, encoding="utf-8")
        cfg = load_config(path, create=False)
        assert cfg.error == "", cfg.error
        return [(l.trigger, l.resolved_type) for l in cfg.links]


if __name__ == "__main__":
    unittest.main()
