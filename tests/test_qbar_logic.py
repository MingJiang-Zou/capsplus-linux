"""qbar 纯逻辑测试：fuzzy 匹配、算术安全求值、URL 判定、.desktop 解析、历史排序。"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar.logic import (  # noqa: E402
    CalcError, Entry, History, SearchEngine, FileFinder, builtin_entries,
    format_calc, fuzzy_score, history_key, looks_like_url, parse_desktop_file,
    safe_calc, scan_desktop_files,
)


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
        """最近用过的条目要排到前面：同一 fuzzy 匹配分下，历史加成决定名次。"""
        with tempfile.TemporaryDirectory() as tmp:
            hist = History(Path(tmp) / "h.json")
            hist.bump(history_key("app", "/x/beta.desktop"))
            engine = SearchEngine(
                apps=[Entry("beta", "app", 0.0, "", "/x/other.desktop")],
                history=hist,
                file_finder=FileFinder([Path("/nonexistent")]))
            out = engine.search("beta")
            self.assertEqual(out.entries[0].kind, "app")
            self.assertEqual(out.entries[0].payload, "/x/beta.desktop")
            self.assertGreater(out.entries[0].score, out.entries[1].score if len(out.entries) > 1 else 0.0)


class TestSearchEngine(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine(
            apps=[Entry("Firefox", "app", 0.0, "浏览器", "/usr/share/applications/firefox.desktop")],
            history=History(Path(tempfile.mkdtemp()) / "h.json"),
            file_finder=FileFinder([Path("/nonexistent")]))

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
        out = self.engine.search("fire")
        self.assertEqual(out.entries[0].title, "Firefox")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
