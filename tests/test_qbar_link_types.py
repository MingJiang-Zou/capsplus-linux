# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""快速链接四类型（search / url / app / file）的纯逻辑测试 + TOML 写回。

要害三条（都是设置界面要依赖的）：
  ① 老配置不写 `type` 也照常工作（推断规则锁死，别改）；
  ② 写回 `qbar.toml` 之后**再读回来必须一模一样**（顺序、类型、转义都不许漂），
     并且用户写在文件开头的注释要原样带回去；
  ③ app / file 两种类型要能真的被"打开"——`.desktop` 名走桌面启动器，
     带参数的走命令，路径不存在也要列出来并写明，不能假装没这条。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar.logic import (  # noqa: E402
    Entry, FileFinder, History, SearchEngine, builtin_entries, desktop_index,
    settings_launch_command,
)
from qbar.search_config import (  # noqa: E402
    BUILTIN_LINKS, DEFAULT_TRIGGER, LINK_TYPES, SearchLink, duplicate_triggers,
    expand_path, leading_comments, link_problems, load_config, open_plan,
    preview_pair, render_config_toml, render_default_toml, save_config, toml_str,
)


def link(trigger, name, target, type=""):
    return SearchLink(trigger, name, target, type)


class TestResolvedType(unittest.TestCase):
    def test_old_config_without_type_is_inferred(self):
        """没写 type：有占位符 = search，没有 = url（这条推断是向后兼容的全部依据）。"""
        self.assertEqual(link("b", "必应", "https://x/?q={q}").resolved_type, "search")
        self.assertEqual(link("p", "首页", "https://x/").resolved_type, "url")

    def test_raw_placeholder_is_search(self):
        self.assertEqual(link("x", "测试", "https://x/{q_raw}").resolved_type, "search")

    def test_explicit_type_wins_over_inference(self):
        self.assertEqual(link("co", "代码", "code {q}", "app").resolved_type, "app")
        self.assertEqual(link("do", "文档", "~/docs", "file").resolved_type, "file")

    def test_unknown_type_falls_back_to_inference(self):
        """不认识的值不当成"没写"处理：推断结果仍可用，但 link_problems 必须报出来。"""
        bad = link("b", "必应", "https://x/?q={q}", "web")
        self.assertEqual(bad.resolved_type, "search")
        self.assertTrue(any("类型不认识" in m for m in link_problems(bad)))

    def test_type_labels_cover_every_type(self):
        from qbar.search_config import LINK_TYPE_LABEL
        self.assertEqual(sorted(LINK_TYPE_LABEL), sorted(LINK_TYPES))
        self.assertEqual(link("b", "必应", "https://x/?q={q}").type_label, "网站内搜索")


class TestOpenPlan(unittest.TestCase):
    def test_search_is_url_encoded(self):
        self.assertEqual(open_plan(link("b", "必应", "https://x/?q={q}"), "a b"),
                         ("url", "https://x/?q=a+b"))

    def test_fixed_url_ignores_keyword(self):
        self.assertEqual(open_plan(link("p", "首页", "https://x/"), "忽略"), ("url", "https://x/"))

    def test_app_keeps_argument_raw(self):
        """应用参数**不许** URL 编码：`code a b` 要是变成 `code+a+b` 就找不到文件了。"""
        self.assertEqual(open_plan(link("co", "代码", "code --diff {q}", "app"), "a b"),
                         ("app", "code --diff a b"))

    def test_app_without_placeholder(self):
        self.assertEqual(open_plan(link("ff", "火狐", "firefox", "app"), "随便")[0], "app")

    def test_file_expands_home(self):
        kind, payload = open_plan(link("no", "笔记", "~/notes", "file"))
        self.assertEqual(kind, "file")
        self.assertEqual(payload, str(Path.home() / "notes"))

    def test_expand_path_handles_plain_relative_and_home(self):
        self.assertEqual(expand_path("~/a/b"), str(Path.home() / "a" / "b"))
        self.assertEqual(expand_path("/tmp/x"), "/tmp/x")
        # 相对路径按家目录算：Qbar 的 CWD 没人猜得到
        self.assertEqual(expand_path("notes"), str(Path.home() / "notes"))
        self.assertEqual(expand_path(""), "")


class TestLinkProblems(unittest.TestCase):
    def test_good_links_have_no_problem(self):
        for item in BUILTIN_LINKS:
            self.assertEqual(link_problems(item), [], item.trigger)

    def test_missing_fields(self):
        self.assertIn("缺少 trigger", link_problems(link("", "名", "https://x/")))
        self.assertIn("缺少 name", link_problems(link("t", "", "https://x/")))
        self.assertIn("缺少 url", link_problems(link("t", "名", "")))

    def test_trigger_with_space_and_duplicate(self):
        self.assertTrue(any("不能含空格" in m for m in link_problems(link("a b", "名", "https://x/"))))
        self.assertIn("trigger 重复: b", link_problems(link("b", "名", "https://x/"), ("b",)))

    def test_duplicate_is_case_insensitive(self):
        self.assertIn("trigger 重复: B", link_problems(link("B", "名", "https://x/"), ("b",)))

    def test_search_without_placeholder_rejected(self):
        problems = link_problems(link("b", "必应", "https://x/", "search"))
        self.assertTrue(any("{q}" in m for m in problems))

    def test_fixed_url_with_placeholder_rejected(self):
        problems = link_problems(link("p", "首页", "https://x/?q={q}", "url"))
        self.assertTrue(any("固定网址" in m for m in problems))

    def test_file_with_placeholder_rejected(self):
        problems = link_problems(link("no", "笔记", "~/notes/{q}", "file"))
        self.assertTrue(any("不支持 {q}" in m for m in problems))

    def test_app_with_newline_rejected(self):
        problems = link_problems(link("co", "代码", "code\nrm -rf /", "app"))
        self.assertTrue(any("不许换行" in m for m in problems))

    def test_problems_keep_every_complaint(self):
        """一处填错就全说清楚，别让界面改一轮才报一条。"""
        problems = link_problems(link("", "", "", "web"))
        self.assertGreaterEqual(len(problems), 3)


class TestDuplicateTriggers(unittest.TestCase):
    def test_finds_case_insensitive_duplicates_only(self):
        links = [link("b", "一", "https://a/?q={q}"), link("B", "二", "https://b/?q={q}"),
                 link("gh", "三", "https://c/?q={q}")]
        # 报的是"重复的那一行"自己怎么写的，界面按小写比对高亮两行
        self.assertEqual(duplicate_triggers(links), ["B"])
        self.assertEqual(duplicate_triggers([link("b", "一", "https://a/?q={q}")]), [])


class TestPreview(unittest.TestCase):
    def test_search_preview_shows_input_and_final_url(self):
        text, url = preview_pair(link("b", "必应", "https://www.bing.com/search?q={q}"), "keyd 配置")
        self.assertEqual(text, "b keyd 配置")
        self.assertEqual(url, "https://www.bing.com/search?q=keyd+%E9%85%8D%E7%BD%AE")

    def test_search_preview_without_sample_uses_placeholder(self):
        text, url = preview_pair(link("b", "必应", "https://x/?q={q}"), "")
        self.assertEqual(text, "b 关键词")
        self.assertTrue(url.endswith("q="))

    def test_fixed_url_and_file_preview_show_target(self):
        self.assertEqual(preview_pair(link("p", "首页", "https://x/"), "忽略"),
                         ("p", "https://x/"))
        self.assertEqual(preview_pair(link("no", "笔记", "~/notes", "file"))[0], "no")

    def test_app_preview_keeps_argument(self):
        text, payload = preview_pair(link("co", "代码", "code --diff {q}", "app"), "a b")
        self.assertEqual(text, "co a b")
        self.assertEqual(payload, "code --diff a b")


class TestTomlWriter(unittest.TestCase):
    def test_toml_str_escapes(self):
        self.assertEqual(toml_str('a"b'), '"a\\"b"')
        self.assertEqual(toml_str("a\\b"), '"a\\\\b"')
        self.assertEqual(toml_str("a\nb"), '"a\\nb"')
        self.assertEqual(toml_str("中文"), '"中文"')

    def test_order_is_preserved_and_type_written(self):
        links = [link("z", "甲", "https://z/?q={q}", "search"),
                 link("a", "乙", "https://a/", "url"),
                 link("k", "丙", "keepassxc", "app")]
        text = render_config_toml(links, "a", "", True)
        self.assertEqual([l for l in text.splitlines() if l.startswith("trigger =")],
                         ['trigger = "z"', 'trigger = "a"', 'trigger = "k"'])
        self.assertIn('type = "app"', text)

    def test_round_trip_is_lossless(self):
        """写回去再读回来必须一模一样（顺序 + 类型 + 转义），设置界面全靠这条。"""
        links = [link("b", "必应", "https://x/?q={q}"),
                 link("co", '带"引号"的名字', "code --diff {q}", "app"),
                 link("no", "笔记", "~/notes", "file"),
                 link("p", "首页", "https://p/")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qbar.toml"
            save_config(links, "b", 'firefox --new-tab {url}', False, path)
            back = load_config(path, create=False)
        self.assertEqual(back.error, "")
        self.assertEqual([(l.trigger, l.name, l.url, l.resolved_type) for l in back.links],
                         [(l.trigger, l.name, l.url, l.resolved_type) for l in links])
        self.assertEqual(back.default_search, "b")
        self.assertEqual(back.browser, 'firefox --new-tab {url}')
        self.assertIs(back.hide_on_focus_out, False)

    def test_leading_comments_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qbar.toml"
            path.write_text("# 我自己的备忘：bd 是百度\n# 第二行\n\n[general]\n"
                            '[[link]]\ntrigger = "b"\nname = "必应"\nurl = "https://x/?q={q}"\n',
                            encoding="utf-8")
            save_config([link("b2", "新", "https://y/?q={q}")], "b2", "", True, path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("# 我自己的备忘：bd 是百度", text)
        self.assertIn("# 第二行", text)
        self.assertIn('trigger = "b2"', text)

    def test_leading_comments_stops_at_first_real_line(self):
        text = "# 头\n\n[general]\n# 中间的注释\nhide = 1\n"
        self.assertEqual(leading_comments(text), "# 头")

    def test_default_file_reparses_and_has_types(self):
        cfg_text = render_default_toml()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qbar.toml"
            path.write_text(cfg_text, encoding="utf-8")
            back = load_config(path, create=False)
        self.assertEqual(back.error, "")
        self.assertEqual([l.trigger for l in back.links], [l.trigger for l in BUILTIN_LINKS])
        # 内置表里带占位符的是 search、ghp 是固定网址
        self.assertEqual(back.find("b").resolved_type, "search")
        self.assertEqual(back.find("ghp").resolved_type, "url")

    def test_target_alias_is_accepted(self):
        """手改配置时 `target =` 也认（和 `url =` 同一个键，只存一份）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qbar.toml"
            path.write_text('[[link]]\ntrigger = "no"\nname = "笔记"\ntype = "file"\n'
                            'target = "~/notes"\n', encoding="utf-8")
            back = load_config(path, create=False)
        self.assertEqual(back.error, "default_search='b' 没有对应的 link")
        self.assertEqual(back.find("no").url, "~/notes")
        self.assertEqual(back.find("no").resolved_type, "file")


class TempConfigMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.cfg_path = self.dir / "qbar.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, body: str) -> Path:
        self.cfg_path.write_text(body, encoding="utf-8")
        return self.cfg_path

    def only_link(self, trigger: str, name: str, target: str, type: str = "") -> str:
        """写一份**只有这一条链接**的配置。

        default_search 必须跟着指到这条，否则 `search()` 的第一行是
        「⚠ default_search 没有对应的 link」那条提示，测试就会去断言提示行。
        """
        return self.write_config(f'[general]\ndefault_search = "{trigger}"\n'
                                 f'[[link]]\ntrigger = "{trigger}"\nname = "{name}"\n'
                                 f'type = "{type}"\nurl = "{target}"\n')

    def engine(self, apps=None) -> SearchEngine:
        return SearchEngine(apps or [], History(self.dir / "h.json"),
                            FileFinder([self.dir]), config_path=self.cfg_path)


class TestEngineOpenKinds(TempConfigMixin, unittest.TestCase):
    """四类型变成 Entry 之后，`kind` 必须能告诉上层"怎么打开"。"""

    def firefox_apps(self):
        return [Entry("Firefox", "app", 0.0, "浏览器", "/usr/share/applications/firefox.desktop")]

    def test_app_link_resolves_to_desktop_file(self):
        self.only_link("ff", "火狐", "firefox", "app")
        entry = self.engine(self.firefox_apps()).search("ff").entries[0]
        self.assertEqual(entry.kind, "app")
        self.assertEqual(entry.payload, "/usr/share/applications/firefox.desktop")

    def test_app_link_with_arguments_goes_to_exec(self):
        """带参数就走命令，不许退回 .desktop（参数会被启动器丢掉）。"""
        self.only_link("co", "代码", "code --diff {q}", "app")
        entry = self.engine(self.firefox_apps()).search("co a b").entries[0]
        self.assertEqual(entry.kind, "exec")
        self.assertEqual(entry.payload, "code --diff a b")

    def test_app_link_unknown_name_goes_to_exec(self):
        self.only_link("xx", "某程序", "nosuchapp", "app")
        entry = self.engine(self.firefox_apps()).search("xx").entries[0]
        self.assertEqual((entry.kind, entry.payload), ("exec", "nosuchapp"))

    def test_file_link_dir_and_missing_note(self):
        real = self.dir / "somefile.txt"
        real.write_text("x", encoding="utf-8")
        self.write_config(f'[general]\ndefault_search = "sf"\n'
                          f'[[link]]\ntrigger = "sf"\nname = "文件"\ntype = "file"\n'
                          f'url = "{real}"\n'
                          f'[[link]]\ntrigger = "sd"\nname = "目录"\ntype = "file"\n'
                          f'url = "{self.dir}"\n')
        engine = self.engine()
        file_entry = engine.search("sf").entries[0]
        dir_entry = engine.search("sd").entries[0]
        self.assertEqual(file_entry.kind, "file")
        self.assertEqual(file_entry.payload, str(real))
        self.assertEqual(dir_entry.kind, "dir")
        self.assertEqual(dir_entry.subtitle, str(self.dir))

    def test_missing_file_is_listed_with_a_note(self):
        """路径不存在也要列出来 + 写明原因，不能静默消失（否则用户以为界面坏了）。"""
        ghost = self.dir / "ghost"
        self.only_link("gh2", "幽灵", str(ghost), "file")
        entry = self.engine().search("gh2").entries[0]
        self.assertIn("不存在", entry.subtitle)
        self.assertEqual(entry.payload, str(ghost))

    def test_search_and_url_kinds_unchanged(self):
        """老两类必须还是 "url" + 填好关键词的地址（既有用例的口径不许漂）。"""
        self.write_config('[general]\ndefault_search = "b"\n'
                          '[[link]]\ntrigger = "b"\nname = "必应"\nurl = "https://x/?q={q}"\n')
        entry = self.engine().search("b 词").entries[0]
        self.assertEqual(entry.kind, "url")
        self.assertEqual(entry.payload, "https://x/?q=%E8%AF%8D")


class TestDesktopIndex(unittest.TestCase):
    def test_three_spellings_all_resolve(self):
        apps = [Entry("Firefox", "app", 0.0, "", "/usr/share/applications/firefox.desktop")]
        index = desktop_index(apps)
        for key in ("firefox", "firefox.desktop", "Firefox".lower()):
            self.assertEqual(index.get(key), "/usr/share/applications/firefox.desktop")

    def test_non_app_entries_ignored(self):
        self.assertEqual(desktop_index([Entry("x", "file", payload="/a/b.desktop")]), {})


class TestSettingsCommand(unittest.TestCase):
    def test_uses_path_binary_when_installed(self):
        with mock.patch.dict(os.environ, {"PATH": "/fake/bin"}), \
             mock.patch("shutil.which", return_value="/fake/bin/capsplus-settings"):
            self.assertEqual(settings_launch_command(), ["/fake/bin/capsplus-settings"])

    def test_falls_back_to_repo_bin(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(settings_launch_command(), [str(REPO / "bin" / "capsplus-settings")])

    def test_empty_when_nowhere_to_find(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("shutil.which", return_value=None):
            self.assertEqual(settings_launch_command(Path(tmp)), [])

    def test_builtin_command_is_listed_and_named(self):
        titles = [e.title for e in builtin_entries("->")]
        self.assertIn("-> settings", titles)
        entry = builtin_entries("-> settings")[0]
        self.assertEqual((entry.kind, entry.payload), ("cmd", "settings"))

    def test_prefix_completion_offers_settings(self):
        self.assertIn("-> settings", [e.title for e in builtin_entries("-> set")])


class TestDefaultSearchChoices(TempConfigMixin, unittest.TestCase):
    """通用页的 default_search 下拉只该列**搜索类**（选了 app 会把纯输入变成开应用）。"""

    def test_only_search_links_are_choices(self):
        self.write_config('[[link]]\ntrigger = "b"\nname = "必应"\ntype = "search"\n'
                          'url = "https://x/?q={q}"\n'
                          '[[link]]\ntrigger = "ff"\nname = "火狐"\ntype = "app"\nurl = "firefox"\n'
                          '[[link]]\ntrigger = "p"\nname = "首页"\ntype = "url"\nurl = "https://p/"\n')
        cfg = load_config(self.cfg_path, create=False)
        choices = [l.trigger for l in cfg.links
                   if l.resolved_type in ("search",)]
        self.assertEqual(choices, ["b"])
        self.assertEqual(cfg.default_search, DEFAULT_TRIGGER)


if __name__ == "__main__":
    unittest.main()


class TestBuiltinDocsMatchCode(unittest.TestCase):
    """内置引擎改了，README 里那张表必须跟着改（别让文档成为第二份过期的真相）。

    2026-10-16 加：README 那张表当时就漏了 `tb` / `xy` —— 说明"靠人记得"这条路不可信。
    """

    def test_every_builtin_trigger_is_listed_in_readme(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        missing = [l.trigger for l in BUILTIN_LINKS if f"`{l.trigger}`" not in readme]
        self.assertEqual(missing, [], "这些内置引擎没在 README 的表里出现：\n  " + "\n  ".join(missing))

    def test_the_guard_catches_an_unlisted_trigger(self):
        """反例具型：从内置表里挑一个 trigger 把 README 里那一处删掉，判据必须红。"""
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        target = BUILTIN_LINKS[0].trigger
        # 注意要**全部**替换：同一个 trigger 在示例文字里也出现，只删第一处会删到示例、
        # 表格那行还在 —— 我第一版就这么写歪过，反例不成立不等于判据没牙。
        thinned = readme.replace(f"`{target}`", "`__消掉了__`")
        missing = [l.trigger for l in BUILTIN_LINKS if f"`{l.trigger}`" not in thinned]
        self.assertIn(target, missing, "README 里抹掉一个 trigger 都没被抓到，这判据是摆设")
