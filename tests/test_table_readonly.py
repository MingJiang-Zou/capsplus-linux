# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""源码级护栏：**两页表格必须永远只读**（2026-10-15 治本方案 A 的永久锁）。

按 现象 / 原因 / 建议 写在这儿，免得下一个人再把编辑控件塞回表格：

  现象（明江实测截图定案）：GTK3/Wayland 下 TreeView 的 cell editor **不跟随滚动** ——
        表格滚了、编辑框的像素坐标不动，于是编辑框视觉上落到别的行：高亮的是 `t` 行、
        编辑框落在 `k` 行、框里还是 `t` 的值。用户以为改的是 `t`，**写进去的是 `k`**。
  原因：cell editor（尤其 `CellRendererCombo` 的下拉）是独立 surface，父窗口滚动/移动时
        GTK3 不跟随；这个构建里既不暴露 `editing-started`，也没有 popup 跟随机制。
        上一轮"滚动即收悬浮下拉"只补了下拉那一半，文本输入框本身照样脱开 —— 是治标。
  建议：编辑控件搬到表格**外面**（编辑条/表单条），表格只读。本文件从源码层面锁死这个决定：
        出现 `CellRendererCombo`、或把 cell renderer 的 `editable` 设成真值 → 直接判红。
        它不需要显示服务器，所以 headless / CI 也拦得住（界面行为级判据在
        `tests/test_settings_window.py`：表格 renderer 的 `get_property("editable")` 全 False、
        单击第 N 行编辑条显示的就是第 N 行）。

刻意走 **AST**（代码节点）而不是全文搜索：本文件和 `settings_app.py` 的 docstring
里都要写这些词来解释为什么禁用，纯文本搜索会把自己绊倒。
"""

import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

GUI_FILE = "settings/settings_app.py"
FORBIDDEN_FUNCTIONS = ("keep_dropdown_attached_to_scroll", "_open_dropdown_windows")
BAD_SNIPPET = '''
def keep_dropdown_attached_to_scroll(view, scroll, renderer):
    return None
renderer = Gtk.CellRendererCombo(model=m, text_column=0, has_entry=True)
other = Gtk.CellRendererText(editable=True)
other.set_property("editable", True)
'''


def _falsy(node) -> bool:
    """只有明确写成假值（False/0/""/None）才算"设成不可编辑"。
    动态表达式（变量、函数调用）一律按可疑处理：不许靠"运行时大概是 False"蒙过去。"""
    return isinstance(node, ast.Constant) and not node.value


def offenders(source: str, label: str = "<snippet>") -> list[str]:
    """返回源码里所有"把编辑控件放回表格"的写法（空 = 干净）。"""
    tree = ast.parse(source)
    bad: list[str] = []
    for node in ast.walk(tree):
        # ① 代码里出现 CellRendererCombo 这个名字（属性访问或裸名）
        name = (node.attr if isinstance(node, ast.Attribute)
                else node.id if isinstance(node, ast.Name) else None)
        if name == "CellRendererCombo":
            bad.append(f"{label}:{node.lineno} 用了 CellRendererCombo —— "
                       f"表格里的下拉是不跟滚动走的独立 surface")
        # ② set_property("editable", <真值>)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_property" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "editable" and not _falsy(node.args[1])):
            bad.append(f"{label}:{node.lineno} 把 renderer 设成可编辑（set_property）")
        # ③ 构造关键字 editable=<真值>（Gtk.CellRendererText(editable=True) 同样致命）
        if isinstance(node, ast.Call):
            for keyword in node.keywords or ():
                if keyword.arg == "editable" and not _falsy(keyword.value):
                    bad.append(f"{label}:{node.lineno} 构造时把 editable 设成真值")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in FORBIDDEN_FUNCTIONS:
            bad.append(f"{label}:{node.lineno} 又写回了 {node.name}()"
                       f"（治标护栏，方案 A 之后是死代码）")
    return bad


def _dotted(node) -> str:
    """`Gtk.ComboBox.new_with_entry(...)` 这种链式属性取全名（两层不够，得走到底）。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class TestTablesAreReadOnly(unittest.TestCase):
    def test_settings_app_has_no_in_cell_editor(self):
        """真判据：GUI 文件里一处"可编辑 renderer"都不许存在。"""
        path = REPO / GUI_FILE
        self.assertTrue(path.exists(), f"找不到 {path}")
        found = offenders(path.read_text(encoding="utf-8"), GUI_FILE)
        self.assertEqual(found, [], "表格必须只读，编辑走表格下方的编辑条/表单条：\n  "
                                    + "\n  ".join(found))

    def test_the_guard_itself_is_not_vacuous(self):
        """判据要能红：喂一段"可编辑 renderer"的反例，必须逐条抓到（3 类写法都算）。"""
        found = offenders(BAD_SNIPPET)
        self.assertEqual(len(found), 4, f"反例只抓到 {len(found)} 条：{found}")
        kinds = "".join(found)
        self.assertIn("CellRendererCombo", kinds)
        self.assertIn("set_property", kinds)
        self.assertIn("构造时", kinds)
        self.assertIn("keep_dropdown_attached_to_scroll", kinds)

    def test_clean_snippet_passes(self):
        """反向自检：写法正确的表格（editable=False）不许误报 —— 否则这条判据会被绕过。"""
        good = ('r = Gtk.CellRendererText()\n'
                'r.set_property("editable", False)\n'
                'c = Gtk.ComboBox.new_with_entry()\n')
        self.assertEqual(offenders(good), [])

    def test_the_dropdown_moved_out_of_the_table(self):
        """下拉没被"删掉了事"：动作下拉 = 编辑条里的 `ComboBox.new_with_entry`，
        快速链接类型 = 表单条里的 `ComboBoxText`。"""
        tree = ast.parse((REPO / GUI_FILE).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                names.add(_dotted(node.func))
        self.assertIn("Gtk.ComboBox.new_with_entry", names,
                      "动作下拉必须在编辑条里（表格只读之后，常用动作还得能选）")
        self.assertIn("Gtk.ComboBoxText", names,
                      "类型下拉（四选一）必须在表单条里")

    def test_dead_scroll_guard_is_not_brought_back(self):
        """`keep_dropdown_attached_to_scroll` / `_open_dropdown_windows` 不许复活：
        脱开问题的根源没了，留着就是死代码 + 下一个人的困惑。"""
        text = (REPO / GUI_FILE).read_text(encoding="utf-8")
        for name in FORBIDDEN_FUNCTIONS:
            self.assertNotIn(f"def {name}", text, f"{name} 又回来了")
            self.assertNotIn(f"{name}(", text, f"{name} 又被调用了")


class TestEditorBarLogicIsPure(unittest.TestCase):
    """编辑条"第 N 行是谁"这件事必须在**不 import gi** 的纯层里 —— 否则这条判据
    就只能靠人眼在真窗口里看（那正是上一轮没兜住 bug 的原因）。"""

    def test_binding_helper_exists_in_settings_logic(self):
        from capsplus import settings_logic as SL
        for name in ("editor_binding", "editor_target", "commit_status", "desync_problem"):
            self.assertTrue(callable(getattr(SL, name, None)), f"settings_logic.{name} 没了")

    def test_form_helper_exists_in_search_config(self):
        from qbar import search_config as SC
        for name in ("form_at", "link_from_form", "form_problems", "swapped"):
            self.assertTrue(callable(getattr(SC, name, None)), f"search_config.{name} 没了")

    def test_editor_binding_is_the_row_at_that_index(self):
        """直接判据（纯函数版）：第 N 行就是第 N 行，**不是 N±1，也不是选中态之外的来源**。"""
        from capsplus import settings_logic as SL
        rows = [("capl", "e", "up", "光标 上/下/左/右", "默认", ""),
                ("capl", "k", "S-down", "选中 上/下/左/右", "默认", ""),
                ("capl", "t", "macro(up up)", "大段上/下滚（10 行）", "默认", ""),
                ("capl+alt", "b", "C-c", "复制", "覆盖", "")]
        self.assertEqual(SL.editor_binding(rows, 2)["key"], "t")
        self.assertEqual(SL.editor_binding(rows, 0)["key"], "e")
        self.assertEqual(SL.editor_binding(rows, 3)["section"], "capl+alt")
        self.assertIsNone(SL.editor_binding(rows, 4), "越界必须清空，不许留最后一行的残影")
        self.assertIsNone(SL.editor_binding(rows, None))
        self.assertIsNone(SL.editor_binding([], 0))

    def test_target_text_and_commit_wording(self):
        """「这一格要改的是 `capl / t`」这句必须在编辑条和状态栏都写得出来。"""
        from capsplus import settings_logic as SL
        self.assertEqual(SL.editor_target("capl", "t"), "capl / t")
        self.assertEqual(SL.editor_target("capl+alt", "b"), "capl+alt / b")
        text = SL.commit_status("capl", "t", "pageup")
        self.assertIn("capl / t", text)
        self.assertIn("pageup", text)
        self.assertIn("还没生效", text)
        self.assertIn("应用", text)
        self.assertNotIn("[capl]", text, "状态栏这里用的是 keyd 段名，不带方括号写法")

    def test_desync_is_named_not_silently_ignored(self):
        from capsplus import settings_logic as SL
        text = SL.desync_problem("capl / t", "capl / k")
        self.assertIn("capl / t", text)
        self.assertIn("capl / k", text)
        self.assertIn("没改", text)

    def test_form_at_is_the_row_at_that_index(self):
        from qbar import search_config as SC
        rows = [("b", "必应", "search", "网站内搜索", "https://b/?q={q}", "", "—"),
                ("ghp", "我的仓库", "url", "固定网址", "https://github.com/pulls", "", "—"),
                ("no", "笔记目录", "file", "文件或目录", "~/notes", "", "—")]
        self.assertEqual(SC.form_at(rows, 1), ("ghp", "我的仓库", "url",
                                               "https://github.com/pulls"))
        self.assertEqual(SC.form_at(rows, 2)[0], "no")
        self.assertEqual(SC.form_at(rows, 2)[3], "~/notes")
        self.assertIsNone(SC.form_at(rows, 3))
        self.assertIsNone(SC.form_at(rows, None))

    def test_form_problems_excludes_the_row_being_edited(self):
        """改自己那一行时不能把自己算成"重复"——否则原样点保存会被自己拦住（真坑）。"""
        from qbar import search_config as SC
        links = [SC.SearchLink("b", "必应", "https://b/?q={q}", "search"),
                 SC.SearchLink("gh", "GitHub", "https://gh/?q={q}", "search")]
        # ① 原样保存第 0 行：前缀还是 b（第 1 行是 gh）→ 不该报重复
        self.assertEqual(SC.form_problems(("b", "必应", "search", "https://b/?q={q}"),
                                          0, links), [])
        # ② 把第 0 行改成 gh（= 别人的前缀）→ 必须报重复，红字拦住
        found = SC.form_problems(("gh", "必应", "search", "https://b/?q={q}"), 0, links)
        self.assertEqual([p for p in found if "重复" in p], ["trigger 重复: gh"], found)
        # ③ 新增（index=None）时**所有**行都算"别人"
        added = SC.form_problems(("b", "新", "search", "https://b/?q={q}"), None, links)
        self.assertIn("trigger 重复", added[0])
        # ④ 行号越界也要拦住
        self.assertTrue(SC.form_problems(("b", "x", "search", "https://b/?q={q}"), 9, links))
        # ⑤ 大小写不算两条（`B` 和 `b` 在 Qbar 里是同一条）
        self.assertIn("trigger 重复", SC.form_problems(("B", "x", "search", "https://b/?q={q}"),
                                                       1, links)[0])

    def test_swapped_returns_new_list_or_none(self):
        from qbar import search_config as SC
        self.assertEqual(SC.swapped([1, 2, 3], 0, 1), [2, 1, 3])
        self.assertIsNone(SC.swapped([1, 2, 3], 0, -1), "上移到第 0 行之上 = 到顶了")
        self.assertIsNone(SC.swapped([1, 2, 3], 2, 3), "下移到底 = 拦住")
        source = [1, 2, 3]
        SC.swapped(source, 0, 2)
        self.assertEqual(source, [1, 2, 3], "必须返回新表，原表不许被就地改掉")


if __name__ == "__main__":
    unittest.main()
