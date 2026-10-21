# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""设置页的键位全表：默认声明过的绑定 + **空位**（数字键 / F1-F12 / m、z 之类）。

起因（用户原话）：「capsplus 这个软件我希望对应的快捷键的组合都添加上去，可以在设置页面里面设置」
「快捷键我希望同样可以组合数字按键 以及 F1-F12 数字键」。查下来：设置页列出的键
**就是** `mapping_data.SECTIONS` 声明的键（`render.binding_rows` 与 `layer_rows` 都从它派生，
差集为空）—— 所以"有些快捷键没有"不是界面漏渲染，而是**表里根本没声明那些键位**：
参考实现 `lib/lib_keysSet.ahk` 的 keySchemeInit_capslox() 声明了 capl 64 个、capl+alt 52 个，
本仓默认只绑了 31 / 25（差额 33 / 27 个键位，见 `mapping_data.EDITABLE_KEYS` 的注释）。

判据：
  ① 设置页列出的键集 == 参考实现声明的键位全表（`MD.EDITABLE_KEYS`）；
  ② 数字键 0-9 与 F1-F12 在 [capl] 里**可配**：走设置页那条链（校验 → 覆盖层 → 合并 → 渲染）
     真的生成绑定，且只多出这一条；
  ③ 空位**不渲染**：默认渲染里没有它们，既有绑定一条都不许动（顺序 + 字面值都要一字不差）；
  ④ 表里每个键名都是 keyd 自己认得的（有 keyd 的机器才跑，没有就 skip）。

反例具型在同一文件里（`TestCriterionHasTeeth`）：把新增键从表里删掉，①必须红 ——
判据函数 `page_key_problems()` 就是①和反例共用的那一份。
"""

import os
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "settings"))

from capsplus import mapping_data as MD                                # noqa: E402
from capsplus import mapping_overrides as MO                           # noqa: E402
from capsplus import settings_logic as SL                              # noqa: E402
from capsplus.render import binding_rows, editable_keys, normalized_bindings, render  # noqa: E402

# 参考文件（早期手工复刻的 v1 配置）；可用环境变量指向别处，没有就跳过用到它的那条
REF_CONF = Path(os.environ.get(
    "CAPSPLUS_REF_CONF",
    Path.home() / ".dsh/refs/capslock-v3.3.0/keyd-capslock-plus.conf"))

# 用户点名要的两组：数字键 0-9 与 F1-F12（写法以 keyd 自己的键名表为准：`1`..`9` `0`、`f1`..`f12`）
DIGITS = list("1234567890")
FKEYS = [f"f{i}" for i in range(1, 13)]
LETTERS = list("abcdefghijklmnopqrstuvwxyz")


# ---------------------------------------------------------------------------
# 判据本体（①与反例具型共用同一份，别写两遍）
# ---------------------------------------------------------------------------
def page_key_problems(section: str, page_keys, matrix: dict) -> list[str]:
    """设置页列出的键集 vs 参考实现声明的键位全表 —— 空列表 = 一致。"""
    want, got = set(matrix.get(section, ())), set(page_keys)
    out = []
    if want - got:
        out.append(f"[{section}] 参考实现声明了、设置页里却没有: {sorted(want - got)}")
    if got - want:
        out.append(f"[{section}] 设置页里有、参考实现没声明: {sorted(got - want)}")
    return out


def declared_keys(section: str) -> set[str]:
    """内置表（`mapping_data.SECTIONS`）**声明过的**键 —— 也就是设置页原来列出的那些。"""
    return {e[0] for e in dict(MD.SECTIONS)[section] if not isinstance(e, str)}


def ordered_bindings(text: str):
    """归一化后的绑定**序列**（保序）：比集合相等更强 —— 顺序动了也算变。"""
    return sorted(normalized_bindings(text))


# ---------------------------------------------------------------------------
# 独立口径：直接数上游源码里的键位声明（不是拿本仓常量跟本仓常量比）
# ---------------------------------------------------------------------------
# 为什么非要这一份（2026-10-19 反例具型抓到的）：只断言"设置页 == EDITABLE_KEYS"是**自证式**的 ——
# 把某个键从 EDITABLE_KEYS 里删掉，设置页也跟着少了那一个键，两边一起缩，那条断言照样绿。
# 上面那几条"必须覆盖数字/F 键 / 26 个字母"的断言能红，但真正独立的对账物只有上游源码本身。
REF_SCHEME = Path(os.environ.get(
    "CAPSPLUS_REF_SCHEME",
    Path.home() / ".dsh/refs/capslock-v3.3.0/lib_keysSet.ahk"))

# AHK 的键名 -> keyd 的键名。**一字之差不许靠"看着像"**：多字母的名字必须显式列出来 ——
# 第一版只列了改名的那几个，靠 `fullmatch("[a-z]")` 兜底，结果 backspace/minus/enter 这些
# 多字母名字被**静默跳过**（20 个键位凭空消失），正好被下面那条等式判据红了出来（2026-10-19）。
AHK_TO_KEYD = {
    "backquote": "grave", "quote": "apostrophe", "leftSquareBracket": "leftbrace",
    "rightSquareBracket": "rightbrace", "ralt": "rightalt",
    "minus": "minus", "equal": "equal", "backspace": "backspace", "tab": "tab",
    "backslash": "backslash", "semicolon": "semicolon", "enter": "enter",
    "comma": "comma", "dot": "dot", "slash": "slash", "space": "space",
}

# 上游 keySchemeInit_capslox() 的声明数（2026-10-19 数出来的）；解析器一旦漏读就该红，
# 别让"解析坏了"伪装成"设置页没问题"
UPSTREAM_COUNTS = {"capl": 64, "capl+alt": 52}


def upstream_key_matrix(text: str) -> dict[str, set[str]]:
    """上游 `keySchemeInit_capslox()` 声明的键位：`{层: 键集}`。

    只认 `keyset.caps_*` / `keyset.caps_lalt_*`；`caps_win_*`（Caps+Win 那层）本仓没支持，
    显式跳过并在返回值里不带它 —— 别把它当成"我们有"。`press_caps` 是短按动作、不是键位。
    """
    start = text.index("keySchemeInit_capslox(){")
    end = text.index("keySchemeInit_capslockPlus(){")
    out: dict[str, set[str]] = {"capl": set(), "capl+alt": set()}
    for line in text[start:end].splitlines():
        found = re.search(r"keyset\.caps_([A-Za-z0-9_]+)\s*:", line)
        if not found:
            continue
        suffix = found.group(1)
        if suffix == "press_caps" or suffix.startswith("win_") or suffix.startswith("lalt_win"):
            continue
        layer = "capl+alt" if suffix.startswith("lalt_") else "capl"
        if suffix.startswith("lalt_"):
            suffix = suffix[len("lalt_"):]
        if suffix in AHK_TO_KEYD:
            out[layer].add(AHK_TO_KEYD[suffix])
        elif re.fullmatch(r"[a-z]", suffix) or re.fullmatch(r"[0-9]", suffix) \
                or re.fullmatch(r"f([1-9]|1[0-2])", suffix):
            out[layer].add(suffix)
        # 其余（如 lalt_wheelUp、鼠标那些）不是键位，跳过
    return out


class TestPageListsTheWholeKeyMatrix(unittest.TestCase):
    """① 设置页列出的键集 == 参考实现声明的键位全表。"""

    def test_every_allowed_section_has_a_matrix(self):
        """矩阵与覆盖层允许改的段必须一一对应（漏一层就是"那一层又没得配"）。"""
        self.assertEqual(sorted(MD.EDITABLE_KEYS), sorted(MO.ALLOWED_SECTIONS))

    def test_page_keys_equal_the_matrix(self):
        for section in MO.ALLOWED_SECTIONS:
            page = [row[0] for row in binding_rows(section)]
            self.assertEqual(len(page), len(set(page)), f"[{section}] 设置页有重复的键")
            self.assertEqual(page_key_problems(section, page, MD.EDITABLE_KEYS), [],
                             f"[{section}] 设置页列出的键 ≠ 参考实现的键位全表")

    def test_page_keys_cover_declared_plus_digits_and_fkeys(self):
        """用户要的那句：设置页 ⊇ 内置表声明的键 ∪ 数字键 ∪ F1-F12。"""
        page = {row[0] for row in binding_rows("capl")}
        self.assertEqual(declared_keys("capl") - page, set(), "默认绑定的键反而不在设置页里")
        self.assertEqual(set(DIGITS) - page, set(), "数字键没进设置页")
        self.assertEqual(set(FKEYS) - page, set(), "F1-F12 没进设置页")

    def test_all_26_letters_are_configurable(self):
        """26 个字母一个不少（原来 m、z 既没绑定也没处配）。"""
        for section in MO.ALLOWED_SECTIONS:
            page = {row[0] for row in binding_rows(section)}
            self.assertEqual(set(LETTERS) - page, set(), f"[{section}] 有字母配不了")

    def test_fkeys_only_in_the_base_layer(self):
        """F 键只属于基础层：参考实现的 caps_lalt_* 里没有 F 键（别凭空给增强层加）。"""
        self.assertEqual(set(FKEYS) - set(MD.EDITABLE_KEYS["capl"]), set())
        self.assertEqual(set(FKEYS) & set(MD.EDITABLE_KEYS["capl+alt"]), set())

    def test_matrix_is_a_superset_of_every_declared_binding(self):
        """将来往表里加绑定，键位矩阵必须跟着有 —— 否则新键在设置页里又是"没有"。"""
        for section in MO.ALLOWED_SECTIONS:
            self.assertEqual(declared_keys(section) - set(MD.EDITABLE_KEYS[section]), set())

    def test_layer_rows_agrees_with_the_page(self):
        """`layer_rows()`（"能改的全表"）与设置页的行集一致 —— 两份导出不许漂。"""
        for section in MO.ALLOWED_SECTIONS:
            rows = {key for sec, key in MO.layer_rows() if sec == section}
            self.assertEqual(rows, {row[0] for row in binding_rows(section)})
            self.assertEqual(editable_keys(section), MD.EDITABLE_KEYS[section])


class TestPageMatchesTheUpstreamScheme(unittest.TestCase):
    """①的独立口径：设置页列出的键集 == **上游源码里**声明的键位（不是本仓常量的自我循环）。"""

    @unittest.skipUnless(REF_SCHEME.exists(), f"没有上游源码：{REF_SCHEME}")
    def test_page_keys_equal_the_upstream_declaration(self):
        upstream = upstream_key_matrix(REF_SCHEME.read_text(encoding="utf-8"))
        # 解析器自己也得防呆：解析坏了（比如路径变了、函数改名）不能变成"空集照样绿"
        for section, keys in upstream.items():
            self.assertEqual(len(keys), UPSTREAM_COUNTS[section],
                             f"[{section}] 从上游解析出 {len(keys)} 个键位，"
                             f"应该是 {UPSTREAM_COUNTS[section]} —— 解析器漏读或上游改了")
        for section in MO.ALLOWED_SECTIONS:
            page = {row[0] for row in binding_rows(section)}
            self.assertEqual(page, upstream[section],
                             f"[{section}] 设置页与上游声明不一致："
                             f"少了 {sorted(upstream[section] - page)}，多了 {sorted(page - upstream[section])}")

    @unittest.skipUnless(REF_SCHEME.exists(), f"没有上游源码：{REF_SCHEME}")
    def test_upstream_declares_digits_and_fkeys_for_the_base_layer(self):
        """上游确实声明了数字键与 F1-F12（这是用户要的那两组的出处，不是我们自己想的）。"""
        upstream = upstream_key_matrix(REF_SCHEME.read_text(encoding="utf-8"))
        self.assertEqual(set(DIGITS) - upstream["capl"], set())
        self.assertEqual(set(FKEYS) - upstream["capl"], set())


class TestEmptyRowsAreEmpty(unittest.TestCase):
    """空位行的三件事：动作空、说明是"空位"、来源算默认。"""

    def test_empty_rows_carry_no_action(self):
        for section in MO.ALLOWED_SECTIONS:
            for key, action, note, source in binding_rows(section):
                if key in declared_keys(section):
                    continue
                self.assertEqual(action, "", f"[{section}] {key} 是空位，动作该是空串")
                self.assertEqual(note, MD.EMPTY_KEY_NOTE, f"[{section}] {key} 没写清是空位")
                self.assertEqual(source, "默认", f"[{section}] {key} 从没被改过，来源该是默认")

    def test_declared_rows_still_carry_their_action(self):
        """既有绑定的行不许被"空位化"：动作、来源照旧。"""
        rows = dict((k, (a, s)) for k, a, _n, s in binding_rows("capl"))
        self.assertEqual(rows["e"], ("up", "默认"))
        self.assertEqual(rows["enter"][0], "macro(end enter)")


class TestDigitsAndFkeysAreConfigurable(unittest.TestCase):
    """② 走设置页那条链：校验 → 覆盖层 → 合并 → 渲染，真的生成 capl+数字 / capl+F 键。"""

    CASES = (("1", "C-c"), ("0", "C-v"), ("7", "pageup"), ("f1", "S-C-home"),
             ("f5", "pageup"), ("f12", "noop"))

    def merged_for(self, section: str, key: str, action: str):
        values = SL.set_override({}, section, key, action, MO.load_known_keys())
        return MO.merge_overrides(MO.Overrides(values=values))

    def test_each_new_key_becomes_a_real_binding_in_the_base_layer(self):
        base = normalized_bindings(render())
        for key, action in self.CASES:
            with self.subTest(key=key):
                text = render(self.merged_for("capl", key, action))
                added = normalized_bindings(text) - base
                self.assertEqual(added, {("capl", key, action)},
                                 f"capl+{key} 没有变成唯一的新绑定：{sorted(added)}")
                removed = base - normalized_bindings(text)
                self.assertEqual(removed, set(), f"新增 capl+{key} 把既有绑定弄丢了：{removed}")

    def test_new_binding_lands_at_the_end_of_the_section(self):
        """位置可解释：覆盖层新加的键追加在该段段尾（与 merge_overrides 的规则一致）。"""
        lines = render(self.merged_for("capl", "1", "C-c")).splitlines()
        # 段头必须按**整行**找：头部注释里也写着 [capl] / [capl+alt] 这两个字串（踩过）
        inner = lines[lines.index("[capl]") + 1:lines.index("[capl+alt]")]
        block = [ln.strip() for ln in inner
                 if ln.strip() and not ln.strip().startswith("#")]
        self.assertEqual(block[-1], "1 = C-c")

    def test_the_page_marks_it_as_overridden(self):
        """配完之后设置页那一行要认得出是"我改的"（来源变「覆盖」，且还在表里）。"""
        for key, action in (("1", "C-c"), ("f12", "noop")):
            rows = dict((k, (a, s)) for k, a, _n, s in
                        binding_rows("capl", self.merged_for("capl", key, action)))
            self.assertEqual(rows[key], (action, "覆盖"), f"[capl] {key} 没标成覆盖")
            self.assertEqual(len(rows), len(binding_rows("capl")),
                             "新加一条不该改变设置页的行数")


class TestEmptyRowsDoNotTouchTheRender(unittest.TestCase):
    """③ 空位不渲染，既有绑定一条都不许动（顺序 + 字面值）。"""

    def test_no_empty_key_is_rendered_by_default(self):
        matrix = {key for section in MO.ALLOWED_SECTIONS
                  for key in MD.EDITABLE_KEYS[section]}
        declared = {e[0] for _s, rows in MD.SECTIONS
                    for e in rows if not isinstance(e, str)}
        rendered = {key for _s, key, _a in normalized_bindings(render())}
        leaked = (rendered & matrix) - declared
        self.assertEqual(leaked, set(), f"空位被渲染出来了（默认行为会跟着变）：{leaked}")

    def test_empty_position_declares_nothing_in_the_sections(self):
        """空位不许出现在 SECTIONS 里 —— 那等于改了默认绑定。"""
        for section, rows in MD.SECTIONS:
            for key in MD.EDITABLE_KEYS[section]:
                if key in declared_keys(section):
                    continue
                self.assertNotIn(key, [e[0] for e in rows if not isinstance(e, str)],
                                 f"[{section}] {key} 是空位，不该被声明成默认绑定")

    @unittest.skipUnless(REF_CONF.exists(), f"没有参考配置：{REF_CONF}")
    def test_existing_bindings_are_byte_for_byte_the_same(self):
        """与参考配置逐条对齐（比集合相等更严：顺序动了也算变）。"""
        ref = ordered_bindings(REF_CONF.read_text(encoding="utf-8"))
        got = ordered_bindings(render())
        self.assertEqual(got, ref, "既有绑定的渲染结果变了（顺序或字面值）")

    def test_binding_count_is_unchanged(self):
        """条数也钉一下：58 = capl 31 + capl+alt 25 + main 1 + ids 1。

        顺带纠正一处老注释：`tests/test_render.py` 里写的"57 条（capl 30 …）"是错的 ——
        2026-10-19 用 `normalized_bindings(render())` 数出来是 **58**（capl **31**）。
        判据只钉总数，不钉注释里的数字。
        """
        self.assertEqual(len(normalized_bindings(render())), 58)

    def test_digits_and_fkeys_are_absent_until_configured(self):
        """数字/F 键默认**没有绑定**：按住 Caps 按它们，keyd 的层是放行（不会吞键）。"""
        rendered = normalized_bindings(render())
        for key in DIGITS + FKEYS:
            self.assertEqual([b for b in rendered if b[1] == key], [],
                             f"默认渲染里不该有 {key} 的绑定（有就是默认行为被改了）")


class TestKeyNamesAreReal(unittest.TestCase):
    """④ 表里的键名必须是 keyd 认得的（`list-keys`），别把 leftsquare 这种自造名写进去。"""

    def test_every_matrix_key_is_a_valid_keyd_key(self):
        known = MO.load_known_keys()
        if not known:
            self.skipTest("这台机器上没有 keyd（或取不到 list-keys），键名校验跳过")
        for section in MO.ALLOWED_SECTIONS:
            unknown = [k for k in MD.EDITABLE_KEYS[section] if k not in known]
            self.assertEqual(unknown, [], f"[{section}] 这些键名 keyd 不认：{unknown}")

    def test_digits_and_fkeys_are_valid_key_names(self):
        known = MO.load_known_keys()
        if not known:
            self.skipTest("这台机器上没有 keyd（或取不到 list-keys），键名校验跳过")
        for key in DIGITS + FKEYS:
            self.assertIn(key, known, f"keyd 的键名表里没有 {key}")


class TestCriterionHasTeeth(unittest.TestCase):
    """反例具型：把新增的键从表里删掉，判据①必须红（判据抓不住东西 = 没有判据）。"""

    def page_keys(self, section: str) -> list[str]:
        return [row[0] for row in binding_rows(section)]

    def test_removing_a_new_key_makes_the_criterion_red(self):
        for poison in ("1", "0", "f1", "f12", "m", "z"):
            with self.subTest(key=poison):
                crippled = dict(MD.EDITABLE_KEYS)
                crippled["capl"] = [k for k in MD.EDITABLE_KEYS["capl"] if k != poison]
                problems = page_key_problems("capl", self.page_keys("capl"), crippled)
                self.assertEqual(len(problems), 1, f"删掉 {poison} 没被抓到：{problems}")
                self.assertIn(repr(poison), problems[0],
                              f"报红文案里没点名 {poison}：{problems[0]}")

    def test_intact_matrix_is_green(self):
        """同一条判据对完好的表必须绿 —— 否则上面的"红"说明不了任何事。"""
        for section in MO.ALLOWED_SECTIONS:
            self.assertEqual(page_key_problems(section, self.page_keys(section),
                                               MD.EDITABLE_KEYS), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)