"""设置窗口纯逻辑层（`capsplus/settings_logic.py`）的测试。

四条要害：
  ① 宏事件计数必须和 keyd 实测边界一致（数错 = 界面黄字骗人）；
  ② 仓库默认映射**自身**不许超宏上限（这条一改错，界面上每个用户都会看到黄字）；
  ③ 覆盖层增删返回新表、非法输入一律当场拦下（界面红字用的就是这些返回值）；
  ④ 渲染 diff 不许被头部注释里的 `[capl]` / `[capl+alt]` 字串骗到（踩过）。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import apply as cap_apply                      # noqa: E402
from capsplus import mapping_data as MD                      # noqa: E402
from capsplus.mapping_overrides import render_overrides      # noqa: E402
from capsplus.render import render                           # noqa: E402
from capsplus.settings_logic import (                        # noqa: E402
    MACRO_EVENT_LIMIT, action_problem, binding_deltas, changed_bindings, changed_lines,
    copy_values, delete_override, macro_event_count, macro_warning, override_count,
    override_toml_hint, run_apply, snapshot_config, validate_key_name, validate_section,
    set_override,
)


class TestMacroEvents(unittest.TestCase):
    def test_plain_key_is_one_event(self):
        self.assertEqual(macro_event_count("up"), 0)          # 非 macro 不算
        self.assertEqual(macro_event_count("macro(up)"), 1)
        self.assertEqual(macro_event_count("macro(up up up)"), 3)

    def test_modifier_counts_as_its_own_event(self):
        """按下 Shift 再按下方向键 = 2 个事件，这就是 7 行而不是 10 行的原因。"""
        self.assertEqual(macro_event_count("macro(S-up)"), 2)
        self.assertEqual(macro_event_count("macro(S-C-left)"), 3)
        self.assertEqual(macro_event_count("macro(S-up S-up S-up S-up S-up S-up S-up)"), 14)

    def test_over_limit_warns_with_numbers(self):
        text = macro_warning("macro(" + " ".join(["up"] * 16) + ")")
        self.assertIn("16", text)
        self.assertIn("静默", text)
        self.assertIn(str(MACRO_EVENT_LIMIT), text)

    def test_within_limit_has_no_warning(self):
        self.assertEqual(macro_warning("macro(" + " ".join(["up"] * 15) + ")"), "")
        self.assertEqual(macro_warning("up"), "")

    def test_shipped_defaults_never_exceed_the_limit(self):
        """仓库默认表必须自己先干净（超了就是真 bug：keyd 会整条丢掉）。"""
        for section, rows in MD.SECTIONS:
            for entry in rows:
                if isinstance(entry, str):
                    continue
                key, action = entry
                count = macro_event_count(action)
                self.assertLessEqual(count, MACRO_EVENT_LIMIT,
                                     f"[{section}] {key} = {action} → {count} 个事件")


class TestValidation(unittest.TestCase):
    def test_allowed_sections_pass(self):
        self.assertEqual(validate_section("capl"), "")
        self.assertEqual(validate_section("capl+alt"), "")

    def test_main_section_is_refused(self):
        """[main] 是 overload 声明，界面绕不过去（覆盖层 ALLOWED_SECTIONS 也不收）。"""
        problem = validate_section("main")
        self.assertIn("main", problem)
        self.assertIn("overload", problem)

    def test_unknown_section_is_refused(self):
        self.assertTrue(validate_section("nosuchlayer"))
        self.assertTrue(validate_section(""))

    def test_key_name_rules(self):
        known = {"up", "e", "backspace"}
        self.assertEqual(validate_key_name("e", known), "")
        self.assertIn("空", validate_key_name("", known))
        self.assertIn("空格", validate_key_name("a b", known))
        self.assertIn("不认识的键名", validate_key_name("zz", known))
        self.assertEqual(validate_key_name("zz", None), "")   # 没键名表 = 不校验

    def test_action_problem_reports_the_first_thing_wrong(self):
        self.assertEqual(action_problem("up", "e", "capl", {"up", "e"}), "")
        self.assertIn("空格", action_problem("up down", "e", "capl", {"up", "e"}))
        self.assertIn("不认识的键名", action_problem("up", "zz", "capl", {"up"}))
        self.assertIn("只允许改", action_problem("up", "e", "main", {"up", "e"}))
        self.assertIn("不能为空", action_problem("", "e", "capl", {"e"}))


class TestOverrideEditing(unittest.TestCase):
    def test_set_returns_new_table_and_leaves_original_untouched(self):
        before = {"capl": {"e": "up"}}
        after = set_override(before, "capl", "d", "down")
        self.assertEqual(before, {"capl": {"e": "up"}}, "原表不许被改（界面取消时不用回滚）")
        self.assertEqual(after, {"capl": {"e": "up", "d": "down"}})

    def test_set_replaces_existing_override(self):
        after = set_override({"capl": {"e": "up"}}, "capl", "e", "pageup")
        self.assertEqual(after["capl"]["e"], "pageup")

    def test_set_strips_whitespace(self):
        after = set_override({}, "capl", " e ", " up ")
        self.assertEqual(after, {"capl": {"e": "up"}})

    def test_set_rejects_illegal_action(self):
        with self.assertRaises(ValueError) as caught:
            set_override({}, "capl", "e", "macro(up")
        self.assertIn("括号", str(caught.exception))

    def test_set_rejects_bad_section(self):
        with self.assertRaises(ValueError):
            set_override({}, "main", "capslock", "up")

    def test_delete_restores_default(self):
        after = delete_override({"capl": {"e": "pageup"}}, "capl", "e")
        self.assertEqual(after, {})                # 空段直接没了 = 全用仓库默认

    def test_delete_keeps_other_keys(self):
        after = delete_override({"capl": {"e": "pageup", "t": "up"},
                                 "capl+alt": {"b": "pagedown"}}, "capl", "e")
        self.assertEqual(after, {"capl": {"t": "up"}, "capl+alt": {"b": "pagedown"}})

    def test_delete_unknown_is_noop(self):
        self.assertEqual(delete_override({"capl": {"e": "up"}}, "capl", "zz"),
                         {"capl": {"e": "up"}})

    def test_copy_values_is_deep(self):
        original = {"capl": {"e": "up"}}
        clone = copy_values(original)
        clone["capl"]["e"] = "down"
        self.assertEqual(original["capl"]["e"], "up")

    def test_count_and_changed_lines_order(self):
        values = {"capl+alt": {"b": "pagedown"}, "capl": {"e": "pageup", "t": "up"}}
        self.assertEqual(override_count(values), 3)
        # 顺序 = ALLOWED_SECTIONS 的顺序，状态栏文案才不会每次跳动
        self.assertEqual(changed_lines(values),
                         ["[capl] e -> pageup", "[capl] t -> up", "[capl+alt] b -> pagedown"])

    def test_written_file_uses_quoted_section_names(self):
        """界面显示 `[capl+alt]`，**写盘必须**是 `["capl+alt"]`（+ 不是 TOML 裸键字符）。"""
        text = render_overrides({"capl+alt": {"b": "pagedown"}})
        self.assertIn('["capl+alt"]', text)
        self.assertNotIn("\n[capl+alt]\n", text)
        self.assertIn("带 +", override_toml_hint("capl+alt"))


# ---------------------------------------------------------------------------
# 渲染 diff（不许被注释里的段名字串骗到）
# ---------------------------------------------------------------------------
BEFORE = """# 语义: 按住 Caps = [capl] 层; 再加 Alt = [capl+alt] 增强层
# 注意: macro(...) 行尾不许写注释
[ids]
*

[main]
capslock = overload(capl, capslock)

[capl]
# ---- 光标移动 ----
e = up
t = macro(up up up up up up up up up up)

[capl+alt]
t = pageup
"""

AFTER = """# 语义: 按住 Caps = [capl] 层; 再加 Alt = [capl+alt] 增强层
[ids]
*

[main]
capslock = overload(capl, capslock)

[capl]
# ---- 光标移动 ----
e = pageup
t = macro(up up up up up up up up up up)
zz = S-up

[capl+alt]
t = S-pageup
"""


class TestRenderDiff(unittest.TestCase):
    def test_only_real_changes_are_listed(self):
        self.assertEqual(changed_bindings(BEFORE, AFTER),
                         ["[capl] e: up -> pageup", "[capl] zz: （新增） -> S-up",
                          "[capl+alt] t: pageup -> S-pageup"])

    def test_section_header_comments_are_ignored(self):
        """头部注释里也写着 `[capl]` / `[capl+alt]`，按字串 split 会切错段（这条锁住）。"""
        self.assertEqual(changed_bindings(BEFORE, BEFORE), [])
        table = binding_deltas(BEFORE, BEFORE)
        self.assertEqual(table, [])

    def test_removed_binding_is_reported(self):
        lines = changed_bindings(AFTER, BEFORE)
        self.assertIn("[capl] zz: S-up -> （删掉）", lines)

    def test_real_render_pair_has_the_override(self):
        """真的拿 render() 与"默认+覆盖"渲染对比，diff 必须落在 [capl] 的 e 上。"""
        from capsplus.mapping_overrides import merge_overrides, Overrides
        merged = merge_overrides(Overrides(values={"capl": {"e": "pageup"}}))
        deltas = changed_bindings(render(), render(merged))
        self.assertIn("[capl] e: up -> pageup", deltas)
        self.assertFalse(any(d.startswith("[capl+alt]") for d in deltas),
                         f"增强层没被改过，diff 里不该出现它：{deltas}")
        self.assertFalse(any(d.startswith("[main]") or d.startswith("[ids]") for d in deltas))


class TestRunApply(unittest.TestCase):
    """`run_apply` 必须把任何异常都收成文案 —— 按钮不许把窗口带崩。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.overrides = self.dir / "mappings.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_dry_run_accepts_good_override(self):
        self.overrides.write_text('[capl]\ne = "pageup"\n', encoding="utf-8")
        with mock.patch("capsplus.mapping_overrides.OVERRIDES_PATH", self.overrides):
            ok, text = run_apply(dry_run=True)
        self.assertTrue(ok, text)
        self.assertIn("dry-run", text)

    def test_dry_run_refuses_illegal_override(self):
        self.overrides.write_text('[capl]\ne = "up down"\n', encoding="utf-8")
        with mock.patch("capsplus.mapping_overrides.OVERRIDES_PATH", self.overrides):
            ok, text = run_apply(dry_run=True)
        self.assertFalse(ok)
        self.assertIn("覆盖层有问题", text)
        self.assertIn("空格", text)

    def test_apply_error_text_is_passed_through(self):
        """keyd 报错时的文案里有"回滚到哪个备份"，界面必须原样显出来，不许吞。"""
        with mock.patch("capsplus.apply.apply",
                        side_effect=cap_apply.ApplyError("keyd 报错:\n  已回滚到上一版备份 /x/y")):
            ok, text = run_apply()
        self.assertFalse(ok)
        self.assertIn("应用失败", text)
        self.assertIn("已回滚到上一版备份", text)

    def test_unexpected_exception_does_not_escape(self):
        with mock.patch("capsplus.apply.apply", side_effect=ZeroDivisionError("oops")):
            ok, text = run_apply()
        self.assertFalse(ok)
        self.assertIn("ZeroDivisionError", text)

    def test_successful_apply_mentions_reload(self):
        result = cap_apply.ApplyResult(reloaded=True, log="line1\nloaded ok",
                                       backup=Path("/backups/capslock-plus.conf.20261015"))
        with mock.patch("capsplus.apply.apply", return_value=result):
            ok, text = run_apply()
        self.assertTrue(ok)
        self.assertIn("已应用并 reload", text)
        self.assertIn("capslock-plus.conf.20261015", text)
        self.assertIn("loaded ok", text)


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.source = self.dir / "qbar.toml"
        self.backup = self.dir / "state" / "config-backup"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_source_returns_none(self):
        self.assertIsNone(snapshot_config(self.source, self.backup))

    def test_original_is_kept_with_timestamp_dir(self):
        self.source.write_text("# 原件\n", encoding="utf-8")
        target = snapshot_config(self.source, self.backup)
        self.assertIsNotNone(target)
        self.assertEqual(target.read_text(encoding="utf-8"), "# 原件\n")
        self.assertTrue(target.parent.is_dir())

    def test_keeping_the_newest_only(self):
        """超出的旧副本 mv 进焚化炉（不许 rm）。"""
        for name in ("20260101-000000-000000", "20260102-000000-000000",
                     "20260103-000000-000000"):
            (self.backup / name).mkdir(parents=True)
        self.source.write_text("new\n", encoding="utf-8")
        grave = self.dir / "attic"
        with mock.patch("capsplus.settings_logic.cap_apply.attic_dir", return_value=grave):
            snapshot_config(self.source, self.backup, keep=2)
        left = sorted(p.name for p in self.backup.iterdir() if p.is_dir())
        self.assertEqual(len([n for n in left if n.startswith("2026")]), 2, left)
        moved = sorted(p.name for p in grave.iterdir())
        self.assertIn("20260101-000000-000000", moved)


if __name__ == "__main__":
    unittest.main()
