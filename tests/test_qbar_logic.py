"""qbar 纯逻辑测试：fuzzy 匹配、算术安全求值、URL 判定、.desktop 解析、历史排序。"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar.logic import (  # noqa: E402
    toggle_action, should_hide_on_focus_out, FOCUS_OUT_GRACE_MS,
    CalcError, Entry, History, SearchEngine, FileFinder, builtin_entries,
    format_calc, fuzzy_score, history_key, looks_like_url, parse_desktop_file,
    safe_calc, scan_desktop_files,
)
from qbar.search_config import QbarConfig  # noqa: E402

# 单测一律注入内置默认配置：绝不让 SearchEngine 去碰真实的 ~/.config/capsplus/qbar.toml
# （否则跑测试会在用户家里生成配置文件）。
BUILTIN = QbarConfig


def make_engine(apps, history):
    return SearchEngine(apps=apps, history=history,
                        file_finder=FileFinder([Path("/nonexistent")]),
                        config=BUILTIN())


class TestFuzzy(unittest.TestCase):
    def test_subsequence_hit(self):
        self.assertIsNotNone(fuzzy_score("ffx", "firefox"))
        self.assertIsNotNone(fuzzy_score("FFX", "FireFox"))

    def test_miss(self):
        self.assertIsNone(fuzzy_score("zzz", "firefox"))
        self.assertIsNone(fuzzy_score("xof", "firefox"))  # 顺序不对

    def test_contiguous_beats_scattered(self):
        self.assertGreater(fuzzy_score("fire", "firefox"), fuzzy_score("fire", "f_i_r_e_x"))

    def test_empty_query_matches_all(self):
        self.assertEqual(fuzzy_score("", "anything"), 0.0)

    def test_prefix_beats_suffix(self):
        self.assertGreater(fuzzy_score("ged", "gedit"), fuzzy_score("ged", "kate-ged"))


class TestCalc(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(safe_calc("1+2*3"), 7)
        self.assertEqual(safe_calc("(1+2)*3"), 9)
        self.assertEqual(safe_calc("2**10"), 1024)
        self.assertEqual(safe_calc("7//2"), 3)
        self.assertEqual(safe_calc("-3 + 1.5"), -1.5)

    def test_format(self):
        self.assertEqual(format_calc(safe_calc("4/2")), "2")
        self.assertEqual(format_calc(safe_calc("1/3")), "0.3333333333")

    def test_rejects_import(self):
        with self.assertRaises(CalcError):
            safe_calc("__import__('os').system('id')")

    def test_rejects_attribute_and_name(self):
        for expr in ("os.system('id')", "().__class__", "x+1", "len('ab')",
                     "[1,2][0]", "{'a':1}", "1 if 1 else 2", "a.b"):
            with self.assertRaises(CalcError, msg=expr):
                safe_calc(expr)

    def test_rejects_junk_and_divzero(self):
        for expr in ("", "1+", "1/0", "2**999"):
            with self.assertRaises(CalcError, msg=expr):
                safe_calc(expr)


class TestUrlAndBuiltins(unittest.TestCase):
    def test_url_detection(self):
        self.assertEqual(looks_like_url("https://example.com/a"), "https://example.com/a")
        self.assertEqual(looks_like_url("example.com"), "https://example.com")
        self.assertIsNone(looks_like_url("example"))
        self.assertIsNone(looks_like_url("foo bar.com"))

    def test_builtin_prefix_listing(self):
        titles = [e.title for e in builtin_entries("->")]
        self.assertIn("-> date", titles)

    def test_builtin_calc_gives_value(self):
        entry = builtin_entries("-> calc 2+3*4")[0]
        self.assertEqual(entry.kind, "calc")
        self.assertEqual(entry.payload, "14")

    def test_builtin_calc_error_is_not_payload(self):
        entry = builtin_entries("-> calc __import__('os')")[0]
        self.assertEqual(entry.kind, "cmd")
        self.assertEqual(entry.payload, "")

    def test_builtin_q_makes_search_url(self):
        entry = builtin_entries("-> q keyd 配置")[0]
        self.assertTrue(entry.payload.startswith("https://"))
        self.assertIn("keyd", entry.payload)


class TestDesktop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, body):
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_parse_ok(self):
        path = self._write("a.desktop", "[Desktop Entry]\nType=Application\n"
                                       "Name=测试应用\nExec=foo --bar\nComment=说明\n")
        entry = parse_desktop_file(path)
        self.assertIsNotNone(entry)
        self.assertEqual((entry.title, entry.kind, entry.payload),
                         ("测试应用", "app", str(path)))

    def test_parse_skips_nodisplay_and_broken(self):
        self.assertIsNone(parse_desktop_file(
            self._write("b.desktop", "[Desktop Entry]\nName=X\nExec=x\nNoDisplay=true\n")))
        self.assertIsNone(parse_desktop_file(self._write("c.desktop", "[Desktop Entry]\nName=Y\n")))

    def test_scan_prefers_first_dir(self):
        other = self.dir / "second"
        other.mkdir()
        self._write("d.desktop", "[Desktop Entry]\nName=重名\nExec=first\n")
        (other / "d.desktop").write_text("[Desktop Entry]\nName=重名\nExec=second\n", encoding="utf-8")
        entries = scan_desktop_files([self.dir, other])
        self.assertEqual([e.payload for e in entries], [str(self.dir / "d.desktop")])


class TestHistory(unittest.TestCase):
    def test_bump_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            hist = History(path)
            key = history_key("app", "/usr/share/applications/a.desktop")
            self.assertEqual(hist.score(key), 0.0)
            hist.bump(key)
            self.assertGreater(hist.score(key), 0.0)
            again = History(path)
            self.assertGreater(again.score(key), 0.0)

    def test_history_prioritises_recent_hit(self):
        """最近用过的条目要排到前面：同一 fuzzy 匹配分下，历史加成决定名次。

        注意 `beta` 是纯输入，第一条会是「默认引擎搜索」候选（见 qbar 快速链接），
        所以这里比的是**应用条目之间**的名次。
        """
        with tempfile.TemporaryDirectory() as tmp:
            hist = History(Path(tmp) / "h.json")
            hist.bump(history_key("app", "/x/beta.desktop"))
            engine = make_engine(apps=[Entry("beta", "app", 0.0, "", "/x/other.desktop")],
                                 history=hist)
            out = engine.search("beta")
            apps = [e for e in out.entries if e.kind == "app"]
            self.assertEqual(apps[0].payload, "/x/beta.desktop")
            self.assertEqual(apps[0].kind, "app")
            self.assertGreater(apps[0].score, out.entries[0].score - 200.0)


class TestSearchEngine(unittest.TestCase):
    def setUp(self):
        # 原来这里用 tempfile.mkdtemp()：它不会自己回收，每跑一次漏一个空目录，
        # 本机已积到 568 个（2026-10-15 实测）。换成 TemporaryDirectory + addCleanup。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine = make_engine(
            apps=[Entry("Firefox", "app", 0.0, "浏览器", "/usr/share/applications/firefox.desktop")],
            history=History(Path(self._tmp.name) / "h.json"))

    def test_equals_calc(self):
        out = self.engine.search("=1+2*3")
        self.assertEqual(out.entries[0].payload, "7")
        self.assertFalse(out.needs_files)

    def test_equals_calc_error(self):
        out = self.engine.search("=__import__('os')")
        self.assertEqual(out.entries[0].payload, "")
        self.assertIn("不允许", out.entries[0].subtitle)

    def test_url_no_file_search(self):
        out = self.engine.search("https://example.com")
        self.assertEqual(out.entries[0].kind, "url")
        self.assertFalse(out.needs_files)

    def test_app_search_and_file_search(self):
        """纯输入：默认引擎搜索排第一（回车即搜），应用紧随其后（还能按 ↓ 选应用）。"""
        out = self.engine.search("fire")
        self.assertEqual(out.entries[0].title, "必应搜索：fire")
        self.assertEqual(out.entries[0].kind, "url")
        self.assertEqual(out.entries[1].title, "Firefox")
        self.assertTrue(out.needs_files)

    def test_empty_query_lists_apps_without_scan(self):
        out = self.engine.search("")
        self.assertFalse(out.needs_files)
        self.assertTrue(out.entries)

    def test_merge_files_dedupes(self):
        current = [Entry("a.txt", "file", 0.0, "/x", "/x/a.txt")]
        files = [Entry("a.txt", "file", 0.0, "/x", "/x/a.txt"),
                 Entry("ab.txt", "file", 0.0, "/x", "/x/ab.txt")]
        merged = self.engine.merge_files(current, files, "a")
        self.assertEqual(len(merged), 2)


class ToggleActionTests(unittest.TestCase):
    """Caps+Q 的切换决策：Wayland 没 keep-above，被盖住时窗口仍 visible 但没焦点。"""

    def test_hidden_shows(self):
        self.assertEqual(toggle_action(False, False), "show")
        self.assertEqual(toggle_action(False, True), "show")

    def test_visible_but_unfocused_raises(self):
        self.assertEqual(toggle_action(True, False), "raise")

    def test_visible_and_focused_hides(self):
        self.assertEqual(toggle_action(True, True), "hide")


class FocusOutHideTests(unittest.TestCase):
    """失焦即隐藏（默认开）：点别处 = 再按一次 Caps+Q，下次唤起一次就能显示。"""

    def test_flag_off_never_hides(self):
        """hide_on_focus_out=false = 老行为：不隐藏，交给 toggle 的 raise 拉回前台。"""
        self.assertFalse(should_hide_on_focus_out(True, False, 10_000, False))

    def test_not_visible_does_not_hide(self):
        self.assertFalse(should_hide_on_focus_out(False, False, 10_000, True))

    def test_still_focused_does_not_hide(self):
        self.assertFalse(should_hide_on_focus_out(True, True, 10_000, True))

    def test_just_shown_200ms_is_ignored(self):
        """刚显示 200ms 的失焦是冷启动抖动，不算用户点了别处。"""
        self.assertFalse(should_hide_on_focus_out(True, False, 200, True))

    def test_400ms_after_show_hides(self):
        self.assertTrue(should_hide_on_focus_out(True, False, 400, True))

    def test_exactly_at_grace_boundary_hides(self):
        self.assertTrue(should_hide_on_focus_out(True, False, FOCUS_OUT_GRACE_MS, True))

    def test_grace_boundary_default_flag(self):
        self.assertFalse(should_hide_on_focus_out(True, False, FOCUS_OUT_GRACE_MS - 1))


class TestPygiMethodNotUsedAsProperty(unittest.TestCase):
    """护栏：PyGI 里 `window.is_active` 是**方法**，漏了括号就是个恒真的 bound method。

    这类错不会抛异常、类型也对（truthy），单测里只要不真改焦点就看不出来 ——
    本轮"点别处不消失"的根因就是它（`should_hide_on_focus_out(..., self.is_active, ...)`
    永远收到真值 ⇒ 永不隐藏）。所以直接在源码层面拦：GUI 模块里这些名字必须带括号调用。
    """

    METHODS = ("is_active", "get_mapped", "get_realized", "get_visible", "is_visible")
    FILES = ("qbar/qbar_app.py", "tray/tray_app.py", "settings/settings_app.py")

    def test_no_bare_pygi_method_attribute(self):
        import ast
        root = Path(__file__).resolve().parent.parent
        bad = []
        for rel in self.FILES:
            path = root / rel
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    called.add(id(node.func))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr in self.METHODS
                        and id(node) not in called):
                    bad.append(f"{rel}:{node.lineno} {node.attr} 漏了括号（当属性用了）")
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
