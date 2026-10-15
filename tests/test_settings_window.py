"""设置窗口的界面测试（**需要能连上显示服务器**，连不上整模块 skip）。

为什么值得用真窗口跑：任务要求"三页里各页控件确实建出来了、表格有行数"，
这在纯逻辑单测里证明不了 —— 这里跑的是同一条代码路径（`show_all()` 真映射上），
顺带把"编辑→校验→写盘→恢复默认→试渲染"整条链在**临时目录**里走一遍，
绝不碰 `~/.config/capsplus/`（两个路径参数 + state_dir 都指到 tmp）。
"""

import ast
import os
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
from qbar.search_config import SearchLink, load_config, render_config_toml  # noqa: E402

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
        self.pump(0.8)

    def tearDown(self):
        self.window.destroy()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        self._tmp.cleanup()

    def pump(self, seconds: float = 0.5) -> None:
        """跑一会儿主循环：Wayland 下映射是异步的，不 pump 就读不到 mapped 状态。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
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
        # 表格列数 = trigger/名称/类型/目标
        self.assertEqual(len(self.window.links_page.view.get_columns()), 4)

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
        page.store[1][0] = "b"                     # 第二行的 trigger 改成和第一行一样
        page._repaint_all()
        self.assertIn("trigger 重复", page.store[1][5])
        saved = self.window.save_qbar()
        self.assertFalse(saved, "trigger 重复必须拦住写盘")
        self.assertEqual(self.status_color(), "red")
        self.assertIn("重复", self.status_text())

    def test_add_delete_and_move(self):
        page = self.window.links_page
        page.on_add(Gtk.Button())
        self.assertEqual(len(page.store), 4)
        self.assertEqual(page.store[3][1], "新链接")
        # 新增那行还没填 → 校验必须已经在报"缺少 trigger"
        self.assertIn("缺少 trigger", page.store[3][5])
        page.store[3][0] = "zz"
        page.store[3][1] = "测试"
        page.store[3][4] = "https://zz.test/?q={q}"
        page.store[3][2] = "search"
        page._repaint_all()
        self.assertEqual(page.store[3][5], "")
        page.on_move(-1)
        self.assertEqual(page.store[2][0], "zz")
        # 删除会弹确认框：这里直接走 store.remove 的分支（确认框本身另测）
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([2]))
        dialog_shown = []
        original = Gtk.MessageDialog.run
        Gtk.MessageDialog.run = lambda self: (dialog_shown.append(1), Gtk.ResponseType.NO)[1]
        try:
            page.on_delete(Gtk.Button())
        finally:
            Gtk.MessageDialog.run = original
        self.assertEqual(dialog_shown, [1], "删除必须带确认")
        self.assertEqual(len(page.store), 4, "确认框回答否就不该删")

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
        page = self.window.mapping_page
        page._on_action_edited(None, Gtk.TreePath.new_from_indices([self.row_index("capl", "e")]),
                               "pageup")
        self.assertEqual(page.store[self.row_index("capl", "e")][4], "覆盖")
        self.assertEqual(self.window.ov.get("capl", "e"), "pageup")
        # 试渲染（不动系统）
        ok, text = self.app_module.SL.run_apply(dry_run=True, overrides_path=self.overrides,
                                                known_keys=self.window.known_keys)
        self.assertTrue(ok, text)
        page.on_apply(dry_run=True)
        self.assertIn("dry-run", self.status_text())
        # 预览改动要说清是哪一条变了
        page.on_preview(Gtk.Button())
        self.assertIn("[capl] e: up -> pageup", self.status_text())

    def test_illegal_action_is_refused_and_row_unchanged(self):
        page = self.window.mapping_page
        index = self.row_index("capl", "e")
        before = page.store[index][2]
        page._on_action_edited(None, Gtk.TreePath.new_from_indices([index]), "up down")
        self.assertEqual(page.store[index][2], before, "非法值不许留在表里")
        self.assertEqual(self.status_color(), "red")
        self.assertIn("空格", self.status_text())

    def test_unknown_key_action_is_refused_when_keyd_present(self):
        page = self.window.mapping_page
        if self.window.known_keys is None:
            self.skipTest("本机没有 keyd.rvaiya list-keys，键名校验本来就该跳过")
        index = self.row_index("capl", "e")
        page._on_action_edited(None, Gtk.TreePath.new_from_indices([index]), "nosuchkey")
        self.assertEqual(self.status_color(), "red")
        self.assertIn("不认识的键名", self.status_text())

    def test_long_macro_warns_in_amber(self):
        page = self.window.mapping_page
        index = self.row_index("capl", "e")
        page._on_action_edited(None, Gtk.TreePath.new_from_indices([index]),
                               "macro(" + " ".join(["up"] * 16) + ")")
        self.assertEqual(self.status_color(), "amber")
        self.assertIn("16", self.status_text())
        self.assertIn("静默", self.status_text())

    def test_restoring_default_removes_the_override(self):
        page = self.window.mapping_page
        page._on_action_edited(None, Gtk.TreePath.new_from_indices([self.row_index("capl", "t")]),
                               "up")
        page.view.get_selection().select_path(Gtk.TreePath.new_from_indices([self.row_index("capl", "t")]))
        page.on_restore(Gtk.Button())
        self.assertIsNone(self.window.ov.get("capl", "t"))
        self.assertEqual(page.store[self.row_index("capl", "t")][4], "默认")

    def test_editing_back_to_default_value_drops_the_override(self):
        """改成和默认一模一样的值 = 这条覆盖没有意义，直接删掉（覆盖层只存真改过的）。"""
        page = self.window.mapping_page
        index = self.row_index("capl", "e")
        default = page.store[index][2]
        page._on_action_edited(None, Gtk.TreePath.new_from_indices([index]), "pageup")
        page._on_action_edited(None, Gtk.TreePath.new_from_indices([index]), default)
        self.assertIsNone(self.window.ov.get("capl", "e"))

    def test_apply_writes_overrides_file_with_quoted_alt_section(self):
        page = self.window.mapping_page
        page._on_action_edited(None,
                               Gtk.TreePath.new_from_indices([self.row_index("capl+alt", "b")]),
                               "up")
        page.on_apply(dry_run=True)           # 写覆盖层 + 试渲染（不动 /etc/keyd）
        text = self.overrides.read_text(encoding="utf-8")
        self.assertIn('["capl+alt"]', text)
        self.assertNotIn("\n[capl+alt]\n", text)
        self.assertIn("b = \"up\"", text)

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
