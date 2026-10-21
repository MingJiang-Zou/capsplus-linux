# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""第五种快速链接类型 `ask`（复制 + 打开）：纯逻辑 + 历史往返 + 动作层（全 mock）。

为什么要 `ask`（2026-10-16 实测，证据全文见 docs/KNOWN-LIMITS.md）：
「把问题塞进网址让网页端自己问」**只有站方给了口子才成立**。DeepSeek 给了 → `ds` 走 `search`；
千问没给（业务侧只读夸克集成参数、唯一的 `?q=` 是夸克搜索构造器、代码里明确抛
`"Workbench URL auto-send is not supported"`）→ `qw` 走 `ask`：回车把**问题原文**放进剪贴板 +
打开网页，用户在输入框 `Ctrl+V` 再回车。

这个文件锁住五件事（都是"改错了界面不会自己报警"的那类）：
  ① 建模：历史载荷**不许**用字符串分隔符拼两个字段 —— 用 `JSON([网址, 问题])`；
     问题里含 **换行 / 逗号 / `|` / 中文长句 / 引号反斜杠** 时往返逐字符一致，
     且历史键里**只有一个裸制表符**（`history_key` 就是靠它拆 kind 与载荷的）；
  ② 校验：`ask` 的 url 必须以 `http://` / `https://` 开头，`form_problems()` 那条路也报；
  ③ 预览：`preview_pair` 对 `ask` 同时给出**目标 URL** 与**两步动作**的副文案；
  ④ 动作（源码级，headless 也跑）：`_activate` 的 ask 分支必须
     `set_text(entry.clip)` + `self._open_url(entry.payload)`，不许自己 launch；
  ⑤ 动作（行为级，mock）：剪贴板 `set_text` 收到的是**问题原文**、`_open_url` 收到的是
     期望 URL、窗口关闭、历史键里两个字段都在。**不建窗口、不弹屏幕、不唤起浏览器。**
"""

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qbar.logic import (  # noqa: E402
    KIND_LABEL, Entry, FileFinder, History, SearchEngine, ask_payload_text,
    entry_history_payload, fuzzy_filter, history_key, split_ask_payload,
)
from qbar.search_config import (  # noqa: E402
    ASK_HINT, BUILTIN_LINKS, LINK_TYPES, LINK_TYPE_LABEL, QbarConfig, SearchLink,
    duplicate_triggers, form_at, form_problems, is_http_url, link_from_form,
    link_problems, load_config, open_plan, preview_pair, render_default_toml,
    save_config,
)

APP_PY = REPO / "qbar" / "qbar_app.py"
SETTINGS_PY = REPO / "settings" / "settings_app.py"
QW_URL = "https://www.qianwen.com/chat"

# 硬要求那一条：这些"脏"内容都不许被截断或串位
NASTY_QUESTIONS = {
    "含换行": "第一行\n第二行\t还有制表符",
    "含逗号": "keyd, capsplus, 和 GTK3 哪个先启动？",
    "含竖线": "a | b || c —— 分隔符方案今天就死在这儿",
    "中文长句": ("把 CapsLock 改成修饰键之后再按 Q 唤起启动器，"
                 "Wayland 下窗口拿不到键盘焦点是因为 present() 早于合成器给焦点，"
                 "这么长一段话不该被任何分隔符方案切开"),
    "带引号反斜杠括号": '他说"回车"再按 \\ 反斜杠 以及 ] 中括号',
    "空问题": "",
}


def link(trigger, name, target, type=""):
    return SearchLink(trigger, name, target, type)


def ask_link(target=QW_URL, trigger="qw", name="千问（复制+打开）"):
    return link(trigger, name, target, "ask")


def entry_for(ln: SearchLink, keyword: str) -> Entry:
    """link + 关键词 → Entry：走 `QbarConfig.match_link` → `SearchEngine._entry_from_match`
    这条**正路**（`desktops` 只有 app 类才用，这里给个空表）。"""
    match = QbarConfig(links=[ln]).match_link(ln, keyword)
    stub = SearchEngine.__new__(SearchEngine)
    stub.desktops = {}
    return SearchEngine._entry_from_match(stub, match)


# ---------------------------------------------------------------------------
# ① 类型本身：ask 是注册过的第五种，且**绝不**被推断出来
# ---------------------------------------------------------------------------
class TestAskIsAType(unittest.TestCase):
    def test_ask_is_registered_with_a_chinese_label(self):
        self.assertIn("ask", LINK_TYPES)
        self.assertEqual(LINK_TYPE_LABEL["ask"], "问 AI（复制+打开）")

    def test_ask_resolved_type_and_label(self):
        self.assertEqual(ask_link().resolved_type, "ask")
        self.assertEqual(ask_link().type_label, "问 AI（复制+打开）")

    def test_ask_is_never_inferred(self):
        """不写 `type` 的固定网址**不许**变成 ask：动作差一整条链，猜错等于啥也没复制。"""
        self.assertEqual(link("qw", "千问", QW_URL).resolved_type, "url")

    def test_entry_kind_ask_has_a_label(self):
        self.assertEqual(KIND_LABEL["ask"], "问 AI")

    def test_type_dropdown_verb_table_covers_ask(self):
        """设置界面那行预览是 `VERB[link.resolved_type]` —— 漏了 ask 就是当场 KeyError，
        而 KeyError 只在**真人点开设置界面**时才炸。所以在源码层拦住。"""
        tree = ast.parse(SETTINGS_PY.read_text(encoding="utf-8"))
        verbs = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "VERB":
                        verbs = {k.value for k in node.value.keys}
        self.assertTrue(verbs, "没找到 VERB 表（改名了？那这条判据要跟着改）")
        self.assertEqual(verbs, set(LINK_TYPES), f"VERB 与 LINK_TYPES 不一致：{verbs}")


# ---------------------------------------------------------------------------
# ② open_plan / Entry：网址走 payload、问题原文走 clip，两条互不污染
# ---------------------------------------------------------------------------
class TestAskOpenPlan(unittest.TestCase):
    def test_fixed_ask_returns_ask_kind_and_the_original_url(self):
        self.assertEqual(open_plan(ask_link(), "任意问题"), ("ask", QW_URL))

    def test_ask_with_placeholder_fills_the_opened_url(self):
        """允许 url 里带 `{q}`：那时打开的是**填好的地址**。"""
        self.assertEqual(open_plan(ask_link("https://a.com/chat?topic={q}"), "a b"),
                         ("ask", "https://a.com/chat?topic=a+b"))

    def test_clip_is_the_raw_question_even_when_url_is_encoded(self):
        """硬要求：写进剪贴板的是**用户输入的原始问题**，不是 URL、不是编码串。"""
        entry = entry_for(ask_link("https://a.com/chat?topic={q}"), "中文 a b")
        self.assertEqual(entry.clip, "中文 a b")
        self.assertNotIn("%E4", entry.clip)
        self.assertNotIn("+", entry.clip)
        self.assertEqual(entry.payload, "https://a.com/chat?topic=%E4%B8%AD%E6%96%87+a+b")

    def test_entry_from_match_carries_both_fields(self):
        entry = entry_for(ask_link(), "我的问题")
        self.assertEqual((entry.kind, entry.payload, entry.clip), ("ask", QW_URL, "我的问题"))
        self.assertEqual(entry.label, "问 AI")
        self.assertIn("我的问题", entry.title)          # 标题里看得见问题（用户能核对）

    def test_ask_is_not_mistaken_for_the_url_kind(self):
        """ask 走自己那条路，不许被 `search`/`url` 的 `"url"` 方式接走（那样就没复制了）。"""
        self.assertEqual(open_plan(ask_link())[0], "ask")
        self.assertEqual(open_plan(link("p", "首页", QW_URL))[0], "url")

    def test_bare_trigger_gives_an_ask_entry_with_nothing_to_copy(self):
        """光敲 `qw`（内置那条不带 {q}）：照样是 ask 条目，clip 是空串 ——
        动作层靠这个空串决定**不动剪贴板**。"""
        self.assertEqual(entry_for(ask_link(), "").clip, "")


# ---------------------------------------------------------------------------
# ③ 校验（纯逻辑层）：说人话，且 `form_problems()` 那条路也报
# ---------------------------------------------------------------------------
class TestAskValidation(unittest.TestCase):
    def test_http_and_https_are_accepted(self):
        for good in ("http://a.com/chat", QW_URL, "HTTPS://A.COM/chat",
                     "https://a.com/chat?topic={q}"):
            with self.subTest(url=good):
                self.assertEqual(link_problems(ask_link(good)), [])

    def test_non_url_target_is_rejected_with_plain_words(self):
        for bad in ("~/notes", "firefox", "ftp://a/b", "www.qianwen.com/chat",
                   QW_URL.replace("https://", ""), "chat?topic={q}"):
            with self.subTest(url=bad):
                problems = link_problems(ask_link(bad))
                self.assertTrue(any("http://" in m and "https://" in m for m in problems),
                                f"这种目标该拦下来：{bad!r} → {problems}")

    def test_form_problems_reports_it_too(self):
        """设置界面走的是 `form_problems()`（界面只摆控件）。"""
        others = [link("b", "必应", "https://x/?q={q}")]
        problems = form_problems(("qw", "千问", "ask", "~/notes"), None, others)
        self.assertTrue(any("http://" in m for m in problems), problems)
        self.assertEqual(form_problems(("qw", "千问", "ask", QW_URL), None, others), [])

    def test_placeholder_is_optional_for_ask(self):
        self.assertEqual(link_problems(ask_link(QW_URL)), [])
        self.assertEqual(link_problems(ask_link("https://a.com/chat?topic={q}")), [])
        self.assertEqual(link_problems(ask_link("https://a.com/chat?topic={q_raw}")), [])

    def test_is_http_url_helper(self):
        self.assertTrue(is_http_url("  https://x/  "))
        self.assertTrue(is_http_url("HTTP://x"))
        self.assertFalse(is_http_url("https:/x"))
        self.assertFalse(is_http_url(""))

    def test_builtin_qw_engine_is_valid_and_unique(self):
        qw = [l for l in BUILTIN_LINKS if l.trigger == "qw"]
        self.assertEqual(len(qw), 1)
        self.assertEqual((qw[0].name, qw[0].url, qw[0].resolved_type),
                         ("千问（复制+打开）", QW_URL, "ask"))
        self.assertEqual(link_problems(qw[0]), [])
        self.assertEqual(duplicate_triggers(BUILTIN_LINKS), [])


# ---------------------------------------------------------------------------
# ④ 预览：目标 URL 与两步动作都要写清楚
# ---------------------------------------------------------------------------
class TestAskPreview(unittest.TestCase):
    def test_preview_shows_url_and_the_two_step_hint(self):
        text, result = preview_pair(ask_link(), "keyd 怎么配常驻")
        self.assertEqual(text, "qw keyd 怎么配常驻")
        self.assertIn(QW_URL, result)                    # 目标显示 URL
        self.assertIn(ASK_HINT, result)                   # 副文案：两步动作
        self.assertIn("剪贴板", result)
        self.assertIn("打开", result)

    def test_preview_without_sample_says_question_not_keyword(self):
        text, _result = preview_pair(ask_link(), "")
        self.assertEqual(text, "qw 问题")

    def test_preview_with_placeholder_shows_the_filled_url(self):
        _t, result = preview_pair(ask_link("https://a.com/chat?topic={q}"), "a b")
        self.assertIn("https://a.com/chat?topic=a+b", result)

    def test_hint_words_say_both_actions_in_order(self):
        self.assertEqual(ASK_HINT, "回车：问题进剪贴板 + 打开网页，粘贴后回车")

    def test_result_is_two_lines_so_the_ui_can_grey_the_hint(self):
        """界面按 `\\n` 拆成两行画（网址等宽、动作灰字）—— 这是两边的约定，锁住。"""
        _t, result = preview_pair(ask_link(), "问题")
        head, sep, tail = result.partition("\n")
        self.assertTrue(sep, "预览少了那行副文案，界面就只会画一条网址")
        self.assertEqual((head, tail), (QW_URL, ASK_HINT))


# ---------------------------------------------------------------------------
# ⑤ 建模与往返：JSON 数组，**不用字符串分隔符**
# ---------------------------------------------------------------------------
class TestAskPayloadModelling(unittest.TestCase):
    def test_pair_is_json_not_a_separator_join(self):
        self.assertEqual(json.loads(ask_payload_text("u", "q")), ["u", "q"])

    def test_roundtrip_survives_nasty_questions(self):
        for tag, question in NASTY_QUESTIONS.items():
            with self.subTest(问题=tag):
                self.assertEqual(split_ask_payload(ask_payload_text(QW_URL, question)),
                                 (QW_URL, question))

    def test_history_key_keeps_exactly_one_raw_tab(self):
        """`history_key` 靠制表符拆 kind/载荷。JSON 把制表符与换行都转义成两个字符，
        所以 ask 的载荷里**不会**多出裸制表符 —— 拆出来不会串位。"""
        for tag, question in NASTY_QUESTIONS.items():
            with self.subTest(问题=tag):
                key = history_key("ask", ask_payload_text(QW_URL, question))
                self.assertEqual(key.count("\t"), 1)
                kind, _, payload = key.partition("\t")
                self.assertEqual(kind, "ask")
                self.assertNotIn("\n", payload)
                self.assertEqual(split_ask_payload(payload), (QW_URL, question))

    def test_url_with_comma_quote_or_pipe_still_roundtrips(self):
        nasty_url = 'https://a.com/p?q=1,2&x="y"|z'
        self.assertEqual(split_ask_payload(ask_payload_text(nasty_url, "问题")),
                         (nasty_url, "问题"))
        # 两边**都**带竖线：任何"拿 `|` 当分隔符"的实现在这里必切错（partition 只切第一个）
        both = ('https://a.com/x|y', '问题里也有 | 竖线')
        self.assertEqual(split_ask_payload(ask_payload_text(*both)), both)

    def test_broken_payload_returns_empty_pair_and_never_raises(self):
        """历史文件是用户能手改的：解不开就是解不开，**不猜、不把垃圾当网址**，也绝不抛。"""
        for bad in ("", "not json", '{"a": 1}', '["one"]', '["a", 2]', "[[1,2],3]", "null"):
            with self.subTest(payload=bad):
                self.assertEqual(split_ask_payload(bad), ("", ""))

    def test_entry_history_payload_only_changes_ask(self):
        e = Entry("t", "ask", 0.0, "s", QW_URL, "问题")
        self.assertEqual(entry_history_payload(e), ask_payload_text(QW_URL, "问题"))
        for kind, payload in (("url", "https://x/"), ("calc", "42"), ("app", "/a/b.desktop")):
            self.assertEqual(entry_history_payload(Entry("t", kind, 0.0, "s", payload)), payload)
        self.assertEqual(entry_history_payload(Entry("标题", "cmd", 0.0, "", "")), "标题")

    def test_fuzzy_filter_keeps_the_clip_field(self):
        """`fuzzy_filter` 会**重建** Entry：漏带 clip 的话，历史里的 ask 条目过一遍 fuzzy
        就"只剩网址、没问题可复制"—— 这条就是防那个回归。"""
        entry = Entry("问题原文", "ask", 1.0, "副标题", QW_URL, "问题原文")
        kept = fuzzy_filter("问题", [entry])
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0].clip, kept[0].payload), ("问题原文", QW_URL))

    def test_merge_files_does_not_drop_a_second_question_on_the_same_url(self):
        """同一网址、两个问题的两条历史都得在：只比网址会把其中一条悄悄吃掉。"""
        engine = SearchEngine.__new__(SearchEngine)
        entries = [Entry(f"问 {i}", "ask", 200.0, "", QW_URL, f"问题{i}") for i in (1, 2)]
        merged = SearchEngine.merge_files(engine, entries, [], "")
        self.assertEqual([e.clip for e in merged], ["问题1", "问题2"])
        same = [Entry("问 1", "ask", 200.0, "", QW_URL, "问题1"),
                Entry("问 1 副本", "ask", 100.0, "", QW_URL, "问题1")]
        self.assertEqual(len(SearchEngine.merge_files(engine, same, [], "")), 1)


# ---------------------------------------------------------------------------
# ⑥ 真文件端到端（临时目录，**绝不碰 ~/.config/capsplus/ 与历史文件**）
# ---------------------------------------------------------------------------
class TempAskMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cfg_path = self.dir / "qbar.toml"

    def engine(self, history_name="h.json") -> SearchEngine:
        return SearchEngine([], History(self.dir / history_name), FileFinder([self.dir]),
                            config_path=self.cfg_path)

    def write_ask_config(self, target=QW_URL, trigger="qw"):
        save_config([ask_link(target, trigger)], trigger, "", True, self.cfg_path)


class TestAskConfigRoundTripOnDisk(TempAskMixin, unittest.TestCase):
    def test_saved_ask_entry_loads_back_with_same_form_and_index(self):
        """验收第 6 条：`save_config()` 写出的 ask 条目能被 `load_config()` 读回，
        `form_at` / index 一致（临时目录里跑）。"""
        links = [link("b", "必应", "https://x/?q={q}", "search"),
                 ask_link(),
                 link("no", "笔记", "~/notes", "file")]
        save_config(links, "b", "", True, self.cfg_path)
        back = load_config(self.cfg_path, create=False)
        self.assertEqual(back.error, "")
        self.assertEqual([l.resolved_type for l in back.links], ["search", "ask", "file"])
        rows = [(l.trigger, l.name, l.resolved_type, l.type_label, l.url, "") for l in back.links]
        self.assertEqual(form_at(rows, 1), ("qw", "千问（复制+打开）", "ask", QW_URL))
        self.assertEqual(form_at(rows, 0), ("b", "必应", "search", "https://x/?q={q}"))
        self.assertEqual(form_at(rows, 3), None)                       # 越界
        self.assertEqual(form_at(rows, None), None)                    # 没选中
        rebuilt = link_from_form(*form_at(rows, 1))
        self.assertEqual((rebuilt.url, rebuilt.resolved_type), (QW_URL, "ask"))
        self.assertEqual(link_problems(rebuilt, ()), [])
        self.assertEqual(form_problems(form_at(rows, 1), 1, back.links), [])   # 改自己不报重复

    def test_default_generated_config_reparses_and_keeps_ask(self):
        text = render_default_toml()
        self.assertIn('type = "ask"', text)
        self.assertIn(QW_URL, text)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qbar.toml"
            path.write_text(text, encoding="utf-8")
            back = load_config(path, create=False)
        self.assertEqual(back.error, "")
        self.assertEqual(back.find("qw").resolved_type, "ask")
        self.assertEqual([l.trigger for l in back.links], [l.trigger for l in BUILTIN_LINKS])

    def test_ask_history_survives_a_restart(self):
        """第 3 条的正路：问过的每一句都要能"重新复制 + 重新打开"，逐字符一致。"""
        self.write_ask_config()
        engine = self.engine()
        for question in NASTY_QUESTIONS.values():
            entry = engine.search(f"qw {question}").entries[0]
            engine.history.bump(history_key(entry.kind, entry_history_payload(entry)))
        again = self.engine()                       # 新 History + 新引擎 = 模拟重启
        restored = {e.clip: e.payload for e in again._history_entries() if e.kind == "ask"}
        for tag, question in NASTY_QUESTIONS.items():
            with self.subTest(问题=tag):
                self.assertEqual(restored.get(question), QW_URL, "问题被截断/串位/丢了")
        self.assertEqual(len(again.history.data), len(NASTY_QUESTIONS))

    def test_undecodable_ask_history_key_is_skipped_not_crash(self):
        engine = self.engine()
        engine.history.data["ask\t用户手改的垃圾"] = {"count": 1, "ts": 1}
        self.assertEqual([e for e in engine._history_entries() if e.kind == "ask"], [])

    def test_history_ask_entry_shows_the_question_as_title(self):
        """标题显**问题原文**：用户就是靠它在结果列表里找回那一条的。"""
        self.write_ask_config()
        engine = self.engine()
        entry = engine.search("qw 千问到底能不能预填").entries[0]
        engine.history.bump(history_key(entry.kind, entry_history_payload(entry)))
        again = self.engine()
        ask_entries = [e for e in again._history_entries() if e.kind == "ask"]
        self.assertEqual(len(ask_entries), 1)
        self.assertEqual(ask_entries[0].title, "千问到底能不能预填")
        self.assertIn(QW_URL, ask_entries[0].subtitle)


# ---------------------------------------------------------------------------
# ⑦ 源码级判据（headless 也跑，nogi 跑法照样跑）：ask 那一支必须
#    「set_text(entry.clip)」+「self._open_url(entry.payload)」，不许绕过 _open_url
# ---------------------------------------------------------------------------
def _dotted(node) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_entry_attr(node, attr: str) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == attr
            and isinstance(node.value, ast.Name) and node.value.id == "entry")


def _is_ask_test(test_node) -> bool:
    return (isinstance(test_node, ast.Compare)
            and _dotted(test_node.left) == "entry.kind"
            and any(isinstance(c, ast.Constant) and c.value == "ask"
                    for c in test_node.comparators))


def ask_branch_violations(source: str) -> list[str]:
    """体检 `_activate` 里 `entry.kind == "ask"` 那一支，返回人话毛病列表（干净 = []）。

    查的三件事全是"改错了没人报警"的：
      ① 复制的是 `entry.clip`（问题原文），不是 `entry.payload`（那是网址）；
      ② 打开走 `self._open_url(entry.payload)` —— 自己 launch 就绕开了 `_launch_context()`，
         那是 2026-10-16「浏览器另起窗口」那个 bug 的根；
      ③ 不许出现 `launch_default_for_uri` / `subprocess` 这类绕过写法。

    注意**只看 branch.body**：`if/elif` 链在 AST 里把后续分支挂在 `orelse` 上，
    走整个 If 会把 `hist` / `calc` 支也卷进来（`calc` 就是 `set_text(entry.payload)`，
    那样会误报成"ask 复制成了网址"）。
    """
    tree = ast.parse(source)
    activate = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_activate"), None)
    if activate is None:
        return ["找不到 `_activate`"]
    branch = next((n for n in ast.walk(activate)
                   if isinstance(n, ast.If) and _is_ask_test(n.test)), None)
    if branch is None:
        return ['`_activate` 里没有 `entry.kind == "ask"` 这一支（ask 回车什么都不会做）']

    problems: list[str] = []
    copied = 0
    for node in ast.walk(ast.Module(body=branch.body, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name.endswith("set_text"):
            if node.args and _is_entry_attr(node.args[0], "clip"):
                copied += 1
            elif node.args and _is_entry_attr(node.args[0], "payload"):
                problems.append("set_text 复制的是 entry.payload（那是**网址**），"
                                "ask 要复制 entry.clip（问题原文）")
            else:
                shown = ast.unparse(node.args[0]) if node.args else "（没有实参）"
                problems.append(f"set_text 的第一个实参不是 entry.clip：{shown}")
        elif name.split(".")[0] in ("Gio", "subprocess", "os", "xdg-open"):
            problems.append(f"ask 分支自己起了进程/直接 launch（{name}），"
                            "绕开了 _open_url 与激活上下文")
    if not copied:
        problems.append("ask 分支没有 `set_text(entry.clip, ...)`：回车不复制问题 = 类型白加")

    opened = [n for n in ast.walk(ast.Module(body=branch.body, type_ignores=[]))
              if isinstance(n, ast.Call) and _dotted(n.func) == "self._open_url"]
    if not opened:
        problems.append("ask 分支没有 self._open_url(...)：不打开网页 = 只做了一半")
    elif not any(n.args and _is_entry_attr(n.args[0], "payload") for n in opened):
        problems.append("ask 分支的 _open_url 实参不是 entry.payload")
    return problems


def ask_branch_source(source: str) -> tuple[int, int]:
    """ask 分支体的首末行号（1 基，含）—— 反例具型要**只**改这一段的副本。"""
    tree = ast.parse(source)
    activate = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_activate")
    branch = next(n for n in ast.walk(activate)
                  if isinstance(n, ast.If) and _is_ask_test(n.test))
    return (branch.body[0].lineno, branch.body[-1].end_lineno)


def with_broken_ask_branch(source: str, *swaps: tuple[str, str]) -> str:
    """只在 ask 分支那几行里做替换，生成一份"违例副本"（原文件一个字节都不动）。

    限定行范围是**必须的**：`self._open_url(entry.payload)` 在 `url` 分支里一模一样，
    整文件替换会改到别人那一段，反例就白造了。
    """
    lines = source.splitlines(keepends=True)
    start, end = ask_branch_source(source)
    body = "".join(lines[start - 1:end])
    for old, new in swaps:
        assert old in body, f"反例要改的这一句不在 ask 分支里：{old}"
        body = body.replace(old, new, 1)
    return "".join(lines[:start - 1]) + body + "".join(lines[end:])


class TestAskActivationSourceGuard(unittest.TestCase):
    def test_current_code_passes_the_check(self):
        self.assertEqual(ask_branch_violations(APP_PY.read_text(encoding="utf-8")), [])

    def test_guard_catches_forgotten_copy(self):
        """反例具型①：复制那一句被删掉/换成 pass → 必须红（这正是"类型白加"）。"""
        src = APP_PY.read_text(encoding="utf-8")
        broken = with_broken_ask_branch(
            src, ("Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(entry.clip, -1)",
                  "Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)"))   # 复制那一句整条没了
        hits = "；".join(ask_branch_violations(broken))
        self.assertIn("set_text", hits, f"漏掉复制没被抓到：{hits}")

    def test_guard_copies_the_url_instead_of_the_question(self):
        """反例具型②：复制成 entry.payload（网址）→ 必须红。"""
        src = APP_PY.read_text(encoding="utf-8")
        broken = with_broken_ask_branch(src, ("set_text(entry.clip, -1)",
                                              "set_text(entry.payload, -1)"))
        hits = "；".join(ask_branch_violations(broken))
        self.assertIn("网址", hits, f"复制成 URL 没被抓到：{hits}")

    def test_guard_catches_no_open(self):
        src = APP_PY.read_text(encoding="utf-8")
        broken = with_broken_ask_branch(src, ("self._open_url(entry.payload)", "pass"))
        hits = "；".join(ask_branch_violations(broken))
        self.assertIn("_open_url", hits, f"忘了打开没被抓到：{hits}")

    def test_guard_catches_launch_bypassing_the_context(self):
        """反例具型③：绕过 `_open_url` 自己 launch 且传 None（2026-10-16 那个 bug 的形状）。"""
        src = APP_PY.read_text(encoding="utf-8")
        broken = with_broken_ask_branch(
            src, ("self._open_url(entry.payload)",
                  "Gio.AppInfo.launch_default_for_uri(entry.payload, None)"))
        hits = "；".join(ask_branch_violations(broken))
        self.assertIn("绕开", hits, f"绕过激活上下文没被抓到：{hits}")

    def test_guard_does_not_drag_in_the_calc_branch(self):
        """正例边界：`calc` 分支就是 `set_text(entry.payload)`（复制算出来的结果），
        那是**对的**。判据不许因为看见它而误报 ask（elif 链的 orelse 陷阱）。"""
        self.assertIn("set_text(entry.payload, -1)", APP_PY.read_text(encoding="utf-8"))
        self.assertEqual(ask_branch_violations(APP_PY.read_text(encoding="utf-8")), [])


# ---------------------------------------------------------------------------
# ⑧ 动作层（mock）：不建窗口、不映射、不唤起浏览器
# ---------------------------------------------------------------------------
try:                                                    # 没装 PyGObject 也要能被 import（收集阶段）
    import gi                                            # noqa: F401
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from qbar import qbar_app as QB                      # noqa: E402
    GI_OK, GI_ERROR = True, ""
except Exception as exc:                                 # noqa: BLE001
    QB = None
    GI_OK, GI_ERROR = False, str(exc)


class StubBar:
    """只摆 `_activate` 用到的成员，**调用真的 `QbarWindow._activate`**。

    为什么不建窗口：界面用例在用户的机器上不许弹任何东西（`CAPSPLUS_GUI` 那套管的是
    真映射窗口）。这里 `Gtk` 整个换成假模块 → `set_text` 不碰真剪贴板；`_open_url` 是
    Mock → 不叫浏览器。于是"回车做了哪两件事"这件事照样能断言。
    """
    def __init__(self):
        self.entry = mock.Mock()
        self.history = mock.Mock()
        self.engine = mock.Mock()
        self.hide = mock.Mock()
        self._open_url = mock.Mock()
        self._launch_context = mock.Mock()
        self._run_command = mock.Mock()
        self._run_builtin = mock.Mock(return_value=True)
        self._open_file = mock.Mock()
        self.order: list[str] = []
        self._open_url.side_effect = lambda url: self.order.append("open")

    def activate(self, entry: Entry):
        QB.QbarWindow._activate(self, entry)


@unittest.skipUnless(GI_OK, f"没有 PyGObject：动作层用例跳过（{GI_ERROR}）")
class TestAskActivationMocked(TempAskMixin, unittest.TestCase):
    """跑**真**的 `QbarWindow._activate`：`Gtk` 换成假模块 → 不碰真剪贴板；
    `_open_url` 是 Mock → 不叫浏览器；不建窗口 → 用户屏幕上什么都不会多出来。
    """

    def setUp(self):
        super().setUp()
        # 诊断日志落临时目录：默认跑法绝不许往 ~/.local/state/capsplus/ 留测试痕迹
        for name, value in (("LOG_PATH", self.dir / "qbar.log"), ("STATE_DIR", self.dir)):
            patcher = mock.patch.object(QB, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_activate(self, entry: Entry):
        """独立跑一次 `_activate`，返回 `(bar, 假 Gtk, 假剪贴板)`。

        每次都是**新的**假对象：mock 的调用记录会累加，subTest 里复用就会误判。
        """
        bar = StubBar()
        clip = mock.Mock()
        fake_gtk = mock.Mock()
        fake_gtk.Clipboard.get.return_value = clip
        with mock.patch.object(QB, "Gtk", fake_gtk):
            bar.activate(entry)
        return bar, fake_gtk, clip

    @staticmethod
    def copied(clip) -> list[str]:
        return [c.args[0] for c in clip.set_text.call_args_list]

    @staticmethod
    def opened(bar) -> list[str]:
        return [c.args[0] for c in bar._open_url.call_args_list]

    def test_enter_copies_the_question_and_opens_the_url(self):
        for tag, question in NASTY_QUESTIONS.items():
            if not question:
                continue
            with self.subTest(问题=tag):
                bar, fake_gtk, clip = self.run_activate(
                    Entry("千问：" + question, "ask", 200.0, "", QW_URL, question))
                self.assertEqual(self.copied(clip), [question])    # 剪贴板收问题原文
                self.assertEqual(self.opened(bar), [QW_URL])       # 浏览器收网址
                # 与算数那条同一个剪贴板：Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                self.assertEqual(fake_gtk.Clipboard.get.call_args.args,
                                 (QB.Gdk.SELECTION_CLIPBOARD,))
                self.assertTrue(bar.hide.called)                   # 与其他类型一致：关闭

    def test_copy_happens_before_the_browser_launch(self):
        """顺序是刻意的：先落剪贴板再叫浏览器。反过来用户回来粘贴可能还是旧内容。"""
        bar = StubBar()
        clip = mock.Mock()
        clip.set_text.side_effect = lambda *_a: bar.order.append("copy")
        fake_gtk = mock.Mock()
        fake_gtk.Clipboard.get.return_value = clip
        with mock.patch.object(QB, "Gtk", fake_gtk):
            bar.activate(Entry("t", "ask", 200.0, "", QW_URL, "问题"))
        self.assertEqual(bar.order, ["copy", "open"])

    def test_empty_question_does_not_wipe_the_clipboard(self):
        """光敲 trigger（没问题）：**不许** set_text —— 把人家的剪贴板洗成空串更糟。"""
        bar, _gtk, clip = self.run_activate(Entry("千问（复制+打开）", "ask", 200.0, "", QW_URL, ""))
        clip.set_text.assert_not_called()
        self.assertEqual(self.opened(bar), [QW_URL])

    def test_history_entry_replays_the_same_question_and_url(self):
        """第 3 条整条路：搜索 → 存历史 → 重启 → 点历史条目 = 复制**同一个问题** + 重新打开。"""
        self.write_ask_config()
        question = NASTY_QUESTIONS["含换行"] + " " + NASTY_QUESTIONS["含竖线"]
        entry = self.engine().search(f"qw {question}").entries[0]
        bar, _gtk, _clip = self.run_activate(entry)
        key = bar.history.bump.call_args.args[0]
        self.assertTrue(key.startswith("ask\t"))
        self.assertEqual(split_ask_payload(key.partition("\t")[2]), (QW_URL, question))

        history = History(self.dir / "h.json")
        history.bump(key)                                   # 真的写一次盘（临时目录）
        restored = [e for e in SearchEngine([], history, FileFinder([self.dir]),
                                            config_path=self.cfg_path)
                    ._history_entries() if e.kind == "ask"]
        self.assertEqual(len(restored), 1)
        bar2, _gtk2, clip2 = self.run_activate(restored[0])
        self.assertEqual(self.copied(clip2), [question])
        self.assertEqual(self.opened(bar2), [QW_URL])

    def test_ask_does_not_touch_the_launch_helpers_directly(self):
        """ask 不许自己碰 `_launch_context` / 起进程 —— 那条路归 `_open_url`。"""
        bar, _gtk, _clip = self.run_activate(Entry("t", "ask", 200.0, "", QW_URL, "问题"))
        bar._launch_context.assert_not_called()
        bar._run_command.assert_not_called()
        bar._open_file.assert_not_called()

    def test_the_stub_really_runs_the_app_code(self):
        """自证这出戏跑的是 `qbar_app._activate` 本体，不是测试自己编的：
        ① 函数的 qualname 指向 QbarWindow；② 真代码里 ask 支若不存在就会掉进 else 支
        （`_run_builtin`），那条一被调用本用例就红。"""
        bar, _gtk, clip = self.run_activate(Entry("t", "ask", 200.0, "", QW_URL, "问题"))
        self.assertEqual(QB.QbarWindow._activate.__qualname__, "QbarWindow._activate")
        self.assertEqual(self.copied(clip), ["问题"])
        self.assertNotIn(QW_URL, self.copied(clip))
        bar._run_builtin.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
