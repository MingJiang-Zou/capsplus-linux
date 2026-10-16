# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""Qbar 快速链接 / 网页搜索的纯逻辑测试（标准库 unittest，无需显示服务器）。

覆盖：trigger 最长前缀优先、默认引擎、URL 编码（中文/空格/&）、固定链接、坏 TOML 容错、
`-> q` 兼容、`looks_like_url` 不被抢、每次打开重读配置。
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar.logic import (  # noqa: E402
    Entry, FileFinder, History, SearchEngine, browser_search_url, builtin_entries,
    looks_like_url,
)
from qbar.search_config import (  # noqa: E402
    BUILTIN_LINKS, DEFAULT_TRIGGER, QbarConfig, SearchLink, ensure_config_file,
    load_config, open_command, parse_search_cmd, render_default_toml,
)


class TempConfigMixin:
    """每个用例一个临时配置目录，绝不碰 ~/.config/capsplus。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.cfg_path = self.dir / "qbar.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, body: str) -> Path:
        self.cfg_path.write_text(body, encoding="utf-8")
        return self.cfg_path

    def load(self, create: bool = True) -> QbarConfig:
        return load_config(self.cfg_path, create=create)

    def engine(self, **kwargs) -> SearchEngine:
        return SearchEngine(
            apps=kwargs.pop("apps", []),
            history=kwargs.pop("history", History(self.dir / "h.json")),
            file_finder=kwargs.pop("file_finder", FileFinder([Path("/nonexistent")])),
            config_path=kwargs.pop("config_path", self.cfg_path),
            **kwargs)


# ---------------------------------------------------------------------------
# 匹配规则
# ---------------------------------------------------------------------------
class TestMatching(TempConfigMixin, unittest.TestCase):
    def test_prefix_hit(self):
        cfg = self.load()
        hit = cfg.match("b keyd 配置")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.link.trigger, "b")
        self.assertEqual(hit.keyword, "keyd 配置")

    def test_longest_prefix_wins(self):
        """`gh 关键词` 必须命中 gh，不能被 g 抢走。"""
        cfg = self.load()
        self.assertEqual(cfg.match("gh cli").link.trigger, "gh")
        self.assertEqual(cfg.match("g cli").link.trigger, "g")
        # 最长的 trigger 先试：`ghcli cli` 不能落到 `gh`（落到 gh 就变成搜 "cli cli"）
        self.assertEqual(cfg.match("ghcli cli").link.trigger, "ghcli")
        self.assertEqual(cfg.match("ghcli cli").url, "https://github.com/cli/cli")
        # 无空格写法同样最长优先；`ghcli` 缺关键词时提示它自己，而不是退回 gh
        self.assertEqual(cfg.match("ghcli"), None)
        self.assertEqual(cfg.no_keyword_hint("ghcli").trigger, "ghcli")

    def test_exact_trigger_needs_keyword_but_fixed_link_does_not(self):
        cfg = self.load()
        self.assertIsNone(cfg.match("gh"))
        self.assertEqual(cfg.no_keyword_hint("gh").trigger, "gh")
        self.assertEqual(cfg.match("ghp").url, "https://github.com/pulls")

    def test_plain_input_uses_default_search(self):
        cfg = self.load()
        hit = cfg.match("keyd 配置")           # 含空格但首段不是 trigger
        self.assertIsNotNone(hit)
        self.assertEqual(hit.link.trigger, DEFAULT_TRIGGER)
        self.assertEqual(hit.keyword, "keyd 配置")
        self.assertEqual(cfg.match("beta 词").link.trigger, DEFAULT_TRIGGER)

    def test_plain_single_word_uses_default_search(self):
        hit = self.load().match("keyd")
        self.assertEqual(hit.link.trigger, DEFAULT_TRIGGER)

    def test_default_search_follows_config(self):
        self.write_config('[general]\ndefault_search = "g"\n'
                          '[[link]]\ntrigger = "g"\nname = "Google"\n'
                          'url = "https://g.test/?q={q}"\n')
        cfg = self.load()
        self.assertEqual(cfg.match("任意词").link.trigger, "g")

    def test_prefix_without_keyword(self):
        """只敲了 `b ` 或 `b` → 不给搜索候选，由上层提示「输入关键词」。"""
        cfg = self.load()
        self.assertIsNone(cfg.match("b "))
        self.assertIsNone(cfg.match("b"))
        self.assertIsNotNone(cfg.no_keyword_hint("b "))
        self.assertIsNotNone(cfg.no_keyword_hint("b"))

    def test_no_keyword_hint_absent_for_normal_input(self):
        cfg = self.load()
        self.assertIsNone(cfg.no_keyword_hint("b 关键词"))
        self.assertIsNone(cfg.no_keyword_hint("随便打的词"))

    def test_case_insensitive_trigger(self):
        cfg = self.load()
        self.assertEqual(cfg.match("GH cli").link.trigger, "gh")
        self.assertEqual(cfg.match("Gh cli").link.trigger, "gh")

    def test_single_letter_trigger_needs_space(self):
        """单字母 trigger 不带空格不算前缀：`beta` 是普通输入，不是「b + eta」。

        （这条是回归：一开始允许无空格前缀，结果纯输入 `beta` 被解析成 b + eta，
        用户搜不到任何以 b/g/w 开头的东西。）
        """
        cfg = self.load()
        self.assertEqual(cfg.match(" beta").link.trigger, DEFAULT_TRIGGER)
        self.assertEqual(cfg.match("beta").link.trigger, DEFAULT_TRIGGER)
        self.assertEqual(cfg.match("beta").keyword, "beta")
        self.assertEqual(cfg.match("google").keyword, "google")
        self.assertEqual(cfg.match("what").keyword, "what")
        # 带空格就还是前缀
        self.assertEqual(cfg.match("b beta").link.trigger, "b")
        self.assertEqual(cfg.match("b beta").keyword, "beta")

    def test_two_letter_trigger_works_without_space(self):
        cfg = self.load()
        self.assertEqual(cfg.match("gh cli").link.trigger, "gh")
        self.assertEqual(cfg.match("ghcli cli").link.trigger, "ghcli")

    def test_empty_input_matches_nothing(self):
        self.assertIsNone(self.load().match(""))
        self.assertIsNone(self.load().match("   "))


# ---------------------------------------------------------------------------
# URL 编码与固定链接
# ---------------------------------------------------------------------------
class TestUrlTemplate(TempConfigMixin, unittest.TestCase):
    def test_chinese_keyword_is_encoded(self):
        url = self.load().match("b 中文").url
        self.assertIn("%E4%B8%AD%E6%96%87", url)
        self.assertNotIn("中文", url)

    def test_space_becomes_plus(self):
        self.assertTrue(self.load().match("b hello world").url.endswith("q=hello+world"))

    def test_ampersand_is_encoded_not_raw(self):
        url = self.load().match("b a&b").url
        self.assertTrue(url.endswith("q=a%26b"), url)

    def test_fixed_link_opens_without_keyword(self):
        """固定链接本身就是完整命令：敲 `ghp` 直接打开，不要"输入关键词"。"""
        cfg = self.load()
        hit = cfg.match("ghp")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.url, "https://github.com/pulls")
        self.assertTrue(hit.link.is_fixed)
        self.assertIsNone(cfg.no_keyword_hint("ghp"), "固定链接不该提示缺关键词")

    def test_raw_placeholder_keeps_spaces(self):
        """`{q_raw}` 给 `gh cli` 这类需要保留原样的地址用（GitHub 搜索用 + 也能认，但路径不能）。"""
        link = SearchLink("x", "测试", "https://example.test/" + "{q_raw}")
        self.assertEqual(link.target_url("a b"), "https://example.test/a%20b")
        self.assertFalse(link.is_fixed)

    def test_target_url_fixed_returns_template(self):
        self.assertEqual(SearchLink("x", "首页", "https://example.test/").target_url("忽略"), "https://example.test/")

    def test_open_command_default_is_xdg_open(self):
        self.assertEqual(open_command("", "https://a.b/c?d=1"),
                         "xdg-open 'https://a.b/c?d=1'")

    def test_open_command_quotes_only_when_needed(self):
        """shlex.quote 只在含特殊字符时加引号：单纯的 https 地址不加也行。"""
        self.assertEqual(open_command("", "https://a.b/c"), "xdg-open https://a.b/c")
        self.assertEqual(open_command("firefox --new-tab {url}", "https://a.b/"),
                         "firefox --new-tab https://a.b/")

    def test_open_command_browser_template(self):
        self.assertEqual(open_command("firefox --new-tab {url}", "https://a.b/c?d=1&e=2"),
                         "firefox --new-tab 'https://a.b/c?d=1&e=2'")


# ---------------------------------------------------------------------------
# 配置文件：生成 / 容错
# ---------------------------------------------------------------------------
class TestConfigFile(TempConfigMixin, unittest.TestCase):
    def test_first_run_writes_default_file(self):
        cfg = self.load()
        self.assertTrue(self.cfg_path.exists())
        self.assertEqual(cfg.error, "")
        triggers = [link.trigger for link in cfg.links]
        # 需求点名的引擎一个都不能少
        for want in ("b", "g", "bd", "w", "gh", "zh", "bl", "tr"):
            self.assertIn(want, triggers)
        self.assertIn(DEFAULT_TRIGGER, triggers)
        self.assertEqual(len(triggers), len(set(triggers)), "trigger 不许重复")

    def test_generated_file_has_chinese_comments_and_reparses(self):
        text = render_default_toml()
        self.assertIn("# capsplus Qbar 快速链接配置", text)
        self.assertIn("{q}", text)
        self.write_config(text)
        cfg = self.load()
        self.assertEqual(cfg.error, "")
        self.assertEqual([l.trigger for l in cfg.links], [l.trigger for l in BUILTIN_LINKS])

    def test_broken_toml_does_not_raise_and_falls_back(self):
        self.write_config('[general]\ndefault_search = "b"\n[[link]\ntrigger = "x"\n')
        cfg = self.load()
        self.assertIn("TOML", cfg.error)
        self.assertEqual([l.trigger for l in cfg.links], [l.trigger for l in BUILTIN_LINKS])
        # 兜底引擎仍然能搜
        self.assertIn("bing", cfg.match("hello").url)

    def test_non_utf8_does_not_raise(self):
        self.cfg_path.write_bytes(b'[general]\ndefault_search = "\xff\xfe"\n')
        cfg = self.load()
        self.assertTrue(cfg.error)

    def test_missing_link_table_reports_and_falls_back(self):
        self.write_config('[general]\ndefault_search = "b"\n')
        cfg = self.load()
        self.assertIn("没有", cfg.error)
        self.assertTrue(cfg.links)

    def test_bad_default_search_reports(self):
        self.write_config('[general]\ndefault_search = "nope"\n'
                          '[[link]]\ntrigger = "b"\nname = "必应"\n'
                          'url = "https://b.test/?q={q}"\n')
        cfg = self.load()
        self.assertIn("default_search", cfg.error)
        self.assertEqual(cfg.match("词").link.trigger, "b")   # 退回第一条可用

    def test_hide_on_focus_out_defaults_to_true(self):
        """不写这一项 = 开（既定口径：点别处就收起来）。"""
        self.write_config('[general]\ndefault_search = "b"\n'
                          '[[link]]\ntrigger = "b"\nname = "必应"\nurl = "https://b/?q={q}"\n')
        self.assertIs(self.load().hide_on_focus_out, True)

    def test_hide_on_focus_out_false_is_parsed(self):
        self.write_config('[general]\nhide_on_focus_out = false\n'
                          '[[link]]\ntrigger = "b"\nname = "必应"\nurl = "https://b/?q={q}"\n')
        self.assertIs(self.load().hide_on_focus_out, False)

    def test_bad_hide_on_focus_out_reports_and_falls_back_to_true(self):
        """写成字符串这种常见手滑：报错但退回默认开，界面不瘫。"""
        self.write_config('[general]\nhide_on_focus_out = "yes"\n'
                          '[[link]]\ntrigger = "b"\nname = "必应"\nurl = "https://b/?q={q}"\n')
        cfg = self.load()
        self.assertIn("hide_on_focus_out", cfg.error)
        self.assertIs(cfg.hide_on_focus_out, True)

    def test_link_missing_field_reports(self):
        """坏条目被跳过，但**好条目不许被默认引擎顶掉**（否则用户看不出自己配的那条没了）。"""
        self.write_config('[[link]]\ntrigger = "b"\nname = "必应"\n'
                          '[[link]]\ntrigger = "x"\nurl = "https://x.test/?q={q}"\n'
                          '[[link]]\ntrigger = "y"\nname = "好的"\nurl = "https://y.test/?q={q}"\n')
        cfg = self.load()
        self.assertIn("缺少 name", cfg.error)
        self.assertIn("缺少 url", cfg.error)
        self.assertEqual([l.trigger for l in cfg.links], ["y"])
        self.assertEqual(cfg.error.count("已用内置默认引擎"), 0, "还有可用条目就不该退回默认")

    def test_all_links_broken_falls_back_to_defaults(self):
        self.write_config('[[link]]\ntrigger = "b"\nname = "缺 url"\n'
                          '[[link]]\ntrigger = "x"\nurl = "https://x.test/?q={q}"\n')
        cfg = self.load()
        self.assertIn("内置默认引擎", cfg.error)
        self.assertEqual([l.trigger for l in cfg.links], [l.trigger for l in BUILTIN_LINKS])

    def test_duplicate_trigger_reports(self):
        self.write_config('[[link]]\ntrigger = "b"\nname = "一"\nurl = "https://a/?q={q}"\n'
                          '[[link]]\ntrigger = "b"\nname = "二"\nurl = "https://b/?q={q}"\n')
        cfg = self.load()
        self.assertIn("重复", cfg.error)
        self.assertEqual(len(cfg.links), 1)

    def test_single_link_table_not_list(self):
        """只写一个 [[link]] 时 tomllib 给的是 dict，不能被当成"格式错"。"""
        self.write_config('[general]\ndefault_search = "x"\n'
                          '[[link]]\ntrigger = "x"\nname = "测试"\nurl = "https://x.test/?q={q}"\n')
        cfg = self.load()
        self.assertEqual(cfg.error, "")
        self.assertEqual(len(cfg.links), 1)

    def test_ensure_config_file_is_idempotent(self):
        first = ensure_config_file(self.cfg_path)
        body = first.read_text(encoding="utf-8")
        first.write_text(body + "\n# 我自己加的注释\n", encoding="utf-8")
        again = ensure_config_file(self.cfg_path)
        self.assertIn("我自己加的注释", again.read_text(encoding="utf-8"), "已存在的配置不许被覆盖")

    def test_create_false_does_not_write(self):
        cfg = load_config(self.cfg_path, create=False)
        self.assertFalse(self.cfg_path.exists())
        self.assertEqual(cfg.error, "")


# ---------------------------------------------------------------------------
# SearchEngine 集成（含 `-> q` 兼容、网址不被抢、重读配置）
# ---------------------------------------------------------------------------
class TestEngineSearch(TempConfigMixin, unittest.TestCase):
    def test_b_opens_bing(self):
        out = self.engine().search("b keyd 配置")
        self.assertEqual(len(out.entries), 1, [e.title for e in out.entries])
        self.assertEqual(out.entries[0].kind, "url")
        self.assertEqual(out.entries[0].title, "必应搜索：keyd 配置")
        self.assertTrue(out.entries[0].payload.startswith("https://www.bing.com/search?q="))
        self.assertFalse(out.needs_files)

    def test_gh_not_hijacked_by_g(self):
        out = self.engine().search("gh cli")
        self.assertEqual(out.entries[0].payload, "https://github.com/search?q=cli")

    def test_plain_input_keeps_search_first_and_apps_after(self):
        """纯输入要能搜网页，但**不能**把应用从列表里挤掉。"""
        engine = self.engine(apps=[Entry("Firefox", "app", 0.0, "浏览器", "/x/ff.desktop")])
        out = engine.search("firefox")
        self.assertEqual(out.entries[0].kind, "url")
        self.assertIn("必应搜索：firefox", out.entries[0].title)
        self.assertIn("Firefox", [e.title for e in out.entries])

    def test_prefix_without_keyword_hint(self):
        out = self.engine().search("b ")
        self.assertEqual(out.entries[0].kind, "cmd")
        self.assertIn("输入关键词", out.entries[0].title)
        self.assertFalse(out.needs_files)

    def test_real_url_is_not_hijacked(self):
        out = self.engine().search("example.com/path")
        self.assertEqual(out.entries[0].payload, "https://example.com/path")
        self.assertEqual(out.entries[0].kind, "url")

    def test_http_url_unchanged(self):
        out = self.engine().search("https://example.com/a?b=1")
        self.assertEqual(out.entries[0].payload, "https://example.com/a?b=1")

    def test_looks_like_url_priority_over_trigger(self):
        """`g 关键词` 不是网址；`g.com` 是网址 —— 两者互不干扰。"""
        self.assertIsNone(looks_like_url("g 关键词"))
        self.assertEqual(looks_like_url("g.com"), "https://g.com")
        self.assertEqual(self.engine().search("g.com").entries[0].payload, "https://g.com")

    def test_legacy_builtin_q_still_works(self):
        out = self.engine().search("-> q keyd 配置")
        self.assertEqual(out.entries[0].kind, "url")
        self.assertIn("keyd", out.entries[0].payload)
        # 与默认引擎一致（等价于纯输入搜索）
        self.assertEqual(out.entries[0].payload, self.engine().search("keyd 配置").entries[0].payload)

    def test_builtin_q_without_keyword(self):
        out = self.engine().search("-> q")
        self.assertEqual(out.entries[0].kind, "cmd")
        self.assertIn("关键词", out.entries[0].subtitle)

    def test_builtin_q_does_not_reparse_trigger(self):
        """`-> q b 词` 搜的是 "b 词" 本身，不是"用必应搜 词"。"""
        out = self.engine().search("-> q b 词")
        self.assertIn("%22" if False else "b+%E8%AF%8D", out.entries[0].payload)

    def test_browser_search_url_uses_config(self):
        self.write_config('[general]\ndefault_search = "x"\n'
                          '[[link]]\ntrigger = "x"\nname = "测试"\nurl = "https://x.test/?q={q}"\n')
        cfg = self.load()
        self.assertEqual(browser_search_url("a b", cfg), "https://x.test/?q=a+b")

    def test_browser_search_url_without_config_is_builtin(self):
        self.assertTrue(browser_search_url("a b").startswith("https://www.bing.com/search?q=a+b"))

    def test_broken_config_shows_warning_entry_and_still_searches(self):
        self.write_config("这不是 toml[[[\n")
        out = self.engine().search("hello")
        self.assertEqual(out.entries[0].kind, "cfg")
        self.assertIn("配置有误", out.entries[0].title)
        self.assertTrue(any(e.kind == "url" for e in out.entries), "坏配置也要能搜")

    def test_broken_config_warning_also_on_builtin_and_calc(self):
        self.write_config("[[[")
        engine = self.engine()
        self.assertEqual(engine.search("-> date").entries[0].kind, "cfg")
        self.assertEqual(engine.search("=1+1").entries[0].kind, "cfg")

    def test_config_reloaded_on_demand(self):
        """每次打开 Qbar 重读配置：改完文件不用重启进程。"""
        engine = self.engine()
        self.assertIn("bing", engine.search("hello").entries[0].payload)
        self.write_config('[general]\ndefault_search = "x"\n'
                          '[[link]]\ntrigger = "x"\nname = "换过了"\nurl = "https://x.test/?q={q}"\n')
        self.assertIn("bing", engine.search("hello").entries[0].payload, "没重读前还是老配置")
        engine.reload_config()
        self.assertIn("x.test", engine.search("hello").entries[0].payload)
        self.assertEqual(engine.search("hello").entries[0].title, "换过了搜索：hello")

    def test_dead_config_path_does_not_crash(self):
        """配置路径写不了（父目录是个文件）→ 出错误行 + 仍能搜，进程不崩。"""
        blocker = self.dir / "blocker"
        blocker.write_text("x", encoding="utf-8")
        engine = self.engine(config_path=blocker / "qbar.toml")
        self.assertTrue(engine.config.error)
        out = engine.search("hello")
        self.assertEqual(out.entries[0].kind, "cfg")
        self.assertTrue(any(e.kind == "url" for e in out.entries))

    def test_missing_parent_dir_is_created(self):
        """父目录不存在不算错：ensure_config_file 会把 ~/.config/capsplus 建出来。"""
        engine = self.engine(config_path=self.dir / "没有这个目录" / "qbar.toml")
        self.assertEqual(engine.config.error, "")
        self.assertTrue((self.dir / "没有这个目录" / "qbar.toml").exists())

    def test_open_config_creates_file(self):
        engine = self.engine()
        path = engine.open_config()
        self.assertTrue(Path(path).exists())

    def test_empty_input_lists_apps_no_warning(self):
        engine = self.engine(apps=[Entry("Firefox", "app", 0.0, "浏览器", "/x/ff.desktop")])
        out = engine.search("")
        self.assertEqual(out.entries[0].kind, "app")
        self.assertFalse(out.needs_files)


class TestBuiltinEntries(TempConfigMixin, unittest.TestCase):
    def test_listing_includes_link_command(self):
        titles = [e.title for e in builtin_entries("->", self.load())]
        self.assertIn("-> link", titles)

    def test_parse_search_cmd_actions(self):
        self.assertEqual(parse_search_cmd("q"), ("hint", ""))
        self.assertEqual(parse_search_cmd("q   "), ("hint", ""))
        self.assertEqual(parse_search_cmd("q a b"), ("search", "a b"))
        self.assertEqual(parse_search_cmd("搜 词"), ("search", "词"))
        self.assertEqual(parse_search_cmd(""), ("list", ""))
        self.assertEqual(parse_search_cmd("date"), ("none", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
