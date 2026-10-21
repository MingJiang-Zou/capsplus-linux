# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
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
from capsplus import services as SV                          # noqa: E402
from capsplus import settings_logic as SL                    # noqa: E402
from capsplus.mapping_overrides import Overrides, merge_overrides, render_overrides  # noqa: E402
from capsplus.render import render                           # noqa: E402
from capsplus.settings_logic import (                        # noqa: E402
    MACRO_EVENT_LIMIT, action_problem, binding_deltas, changed_bindings, copy_values,
    delete_override, macro_event_count, macro_warning, override_toml_hint, run_apply,
    snapshot_config, validate_key_name, validate_section, set_override,
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

    def test_changed_bindings_order_follows_the_rendered_text(self):
        """diff 的行序 = keyd 文本里的出现顺序（先 capl 后 capl+alt），不看 dict 的插入顺序。

        状态栏里同一次改动每次显示的顺序要一样，否则用户会以为界面在乱跳。
        """
        values = {"capl+alt": {"t": "up"}, "capl": {"e": "pageup"}}     # 故意把 alt 写在前面
        deltas = changed_bindings(render(), render(merge_overrides(Overrides(values=values))))
        self.assertEqual(deltas, ["[capl] e: up -> pageup", "[capl+alt] t: pageup -> up"])

    def test_override_with_the_default_value_produces_no_delta(self):
        """覆盖层里写了个和默认一模一样的值 = 没有变化（界面也会顺手把这种覆盖删掉）。"""
        values = {"capl+alt": {"b": "pagedown"}}     # b 的默认就是 pagedown
        self.assertEqual(changed_bindings(render(),
                                          render(merge_overrides(Overrides(values=values)))), [])

    def test_written_file_uses_quoted_section_names(self):
        """界面显示 `[capl+alt]`，**写盘必须**是 `["capl+alt"]`（+ 不是 TOML 裸键字符）。"""
        text = render_overrides({"capl+alt": {"b": "pagedown"}})
        self.assertIn('["capl+alt"]', text)
        self.assertNotIn("\n[capl+alt]\n", text)
        self.assertIn("必须加引号", override_toml_hint("capl+alt"))
        self.assertEqual(override_toml_hint("capl"), "", "两边写法一样的段名不值得提示")


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


# ---------------------------------------------------------------------------
# 服务页（2026-10-21）：表单值 ↔ 配置、校验、脱敏、连通性探测
#
# 这一层**刻意不 import gi**（判据要能离线跑），所以"界面输出里没有明文 key"
# 这条也能在这里判死 —— 不用起窗口。
# ---------------------------------------------------------------------------
class TestServiceForm(unittest.TestCase):
    """表单值 ↔ 配置的往返与校验。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "services.toml"
        self.svc = SV.default_services()
        self.svc.path = self.path
        # 给"翻译/cloud"配一把假凭据：后面判"界面输出里不许有它"
        self.secret = "sk-TEST-do-not-leak"
        self.svc.capability("translate").endpoints["cloud"].api_key = self.secret

    def tearDown(self):
        self._tmp.cleanup()

    # ── 往返 ────────────────────────────────────────────────────────────
    def test_form_values_round_trip_unchanged(self):
        """表单值 ↔ 配置：读出来再写回去，**逐字相同**（界面不许自己改用户的值）。"""
        cap = self.svc.capability("translate")
        index = 0
        values = SL.endpoint_form_values(cap.ordered_endpoints()[index])
        new_svc, problem = SL.set_endpoint(self.svc, "translate", index, values)
        self.assertEqual(problem, "")
        self.assertIsNotNone(new_svc)
        before = cap.ordered_endpoints()[index]
        after = new_svc.capability("translate").endpoints[before.name]
        for field in ("name", "kind", "style", "base_url", "model", "api_key_env",
                      "credentials_file", "credentials_key", "env_file", "env_key"):
            self.assertEqual(getattr(after, field), getattr(before, field), field)
        self.assertEqual(after.timeout_s, before.timeout_s)

    def test_set_endpoint_does_not_touch_the_original(self):
        """改一份配置不许就地改另一份（界面取消时不必回滚）。"""
        before = self.svc.capability("translate").endpoints["local"].base_url
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["local"])
        values["base_url"] = "http://changed/v1"
        new_svc, problem = SL.set_endpoint(self.svc, "translate", 0, values)
        self.assertEqual(problem, "")
        self.assertEqual(self.svc.capability("translate").endpoints["local"].base_url, before)
        self.assertEqual(new_svc.capability("translate").endpoints["local"].base_url,
                         "http://changed/v1")

    def test_rename_keeps_the_plain_key(self):
        """**改名时明文 key 必须跟着搬过去** —— 按位置找旧端点，不按名字。

        界面上看不见 key，所以"悄悄丢了"就等于静默删凭据（用户下次翻译就 401）。
        """
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["cloud"])
        values["name"] = "cloud2"
        new_svc, problem = SL.set_endpoint(self.svc, "translate", 1, values)
        self.assertEqual(problem, "")
        moved = new_svc.capability("translate").endpoints["cloud2"]
        self.assertEqual(moved.api_key, self.secret, "改名把明文 key 弄丢了")

    def test_blank_key_field_keeps_the_stored_key(self):
        """编辑条里那个输入框是空的（界面上看不见 key）—— 留空 = 保持原样，不许抹掉。"""
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["cloud"])
        values["api_key"] = ""
        new_svc, _problem = SL.set_endpoint(self.svc, "translate", 1, values)
        self.assertEqual(new_svc.capability("translate").endpoints["cloud"].api_key, self.secret)

    def test_clear_key_really_removes_it(self):
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["cloud"])
        values["clear_key"] = True
        new_svc, _problem = SL.set_endpoint(self.svc, "translate", 1, values)
        self.assertEqual(new_svc.capability("translate").endpoints["cloud"].api_key, "")

    # ── 校验 ────────────────────────────────────────────────────────────
    def test_empty_base_url_is_refused(self):
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["local"])
        values["base_url"] = ""
        problems = SL.endpoint_problems(values)
        self.assertTrue(any("地址不能为空" in p for p in problems), problems)

    def test_bad_base_url_is_refused(self):
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["local"])
        values["base_url"] = "127.0.0.1:12087"
        problems = SL.endpoint_problems(values)
        self.assertTrue(any("http://" in p for p in problems), problems)

    def test_incomplete_credential_source_is_refused(self):
        """凭据来源只填一半 = 读不出值（还容易被误当成"没配"）。"""
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["local"])
        values["credentials_file"] = "~/.dsh/.credentials.yaml"
        problems = SL.endpoint_problems(values)
        self.assertTrue(any("键名" in p for p in problems), problems)
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["local"])
        values["env_file"] = "~/.dsh/.env"
        problems = SL.endpoint_problems(values)
        self.assertTrue(any("键名" in p for p in problems), problems)

    def test_duplicate_name_is_refused(self):
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["local"])
        problems = SL.endpoint_problems(values, ["local", "cloud"])
        self.assertTrue(any("重复" in p for p in problems), problems)

    def test_bad_kind_or_style_is_refused(self):
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["local"])
        values["kind"] = "半本机"
        values["style"] = "smoke-signals"
        problems = SL.endpoint_problems(values)
        self.assertTrue(any("local" in p for p in problems), problems)
        self.assertTrue(any("openai" in p for p in problems), problems)

    def test_script_style_may_have_a_path_instead_of_a_url(self):
        """script 型的地址是脚本名（本地 OCR 就是 `shot_ocr_local.py`），不许逼它写 http。"""
        values = SL.endpoint_form_values(self.svc.capability("ocr").endpoints["local"])
        values["base_url"] = "shot_ocr_local.py"
        self.assertEqual(SL.endpoint_problems(values), [])

    def test_mode_must_be_auto_or_an_existing_endpoint(self):
        _new, problem = SL.set_capability_mode(self.svc, "translate", "nosuch")
        self.assertTrue(problem)
        _new, problem = SL.set_capability_mode(self.svc, "translate", "auto")
        self.assertEqual(problem, "")

    def test_translate_needs_a_target_language(self):
        values = SL.capability_form_values(self.svc.capability("translate"))
        values["target"] = ""
        problems = SL.capability_problems(values)
        self.assertTrue(any("目标语言" in p for p in problems), problems)

    def test_delete_endpoint_falls_back_to_auto(self):
        """删掉的正好是被指定的那个端点 → 用法退回 auto（不许指着一个不存在的端点）。"""
        svc, _p = SL.set_capability_mode(self.svc, "translate", "cloud")
        new_svc, problem = SL.delete_endpoint(svc, "translate", "cloud")
        self.assertEqual(problem, "")
        self.assertEqual(new_svc.capability("translate").mode, "auto")

    def test_add_endpoint_gets_a_free_name(self):
        new_svc, problem = SL.add_endpoint(self.svc, "translate")
        self.assertEqual(problem, "")
        names = [ep.name for ep in new_svc.capability("translate").ordered_endpoints()]
        self.assertEqual(len(names), len(set(names)), f"重名了：{names}")

    def test_broken_config_is_never_written(self):
        """有毛病就**一个字都不写**（文件不许被写坏）。"""
        broken = SL.copy_services(self.svc)
        broken.capability("translate").endpoints["local"].base_url = ""
        ok, text = SL.save_services_form(broken, self.path)
        self.assertFalse(ok)
        self.assertIn("没有写盘", text)
        self.assertFalse(self.path.exists(), "有毛病还写盘 = 判据失败")

    def test_good_config_is_written_and_read_back(self):
        ok, text = SL.save_services_form(self.svc, self.path)
        self.assertTrue(ok, text)
        self.assertTrue(self.path.exists())
        back = SV.load_services(self.path)
        self.assertEqual(back.error, "")

    # ── 脱敏：界面输出里不许有明文 key ──────────────────────────────────
    def test_form_values_never_contain_the_plain_key(self):
        """**表单值里根本没有明文 key 这一项**（界面回填时拿不到它）。"""
        values = SL.endpoint_form_values(self.svc.capability("translate").endpoints["cloud"])
        self.assertNotIn(self.secret, repr(values))
        self.assertNotIn("api_key", values)
        self.assertTrue(values["has_plain_key"])
        self.assertIn("已配置", values["key_state"])

    def test_credential_state_never_contains_the_value(self):
        state = SL.credential_state(self.svc.capability("translate").endpoints["cloud"])
        self.assertNotIn(self.secret, state)
        self.assertIn("已配置", state)

    def test_summary_never_contains_the_value(self):
        text = SV.format_describe(self.svc)
        self.assertNotIn(self.secret, text)

    def test_short_credential_source_is_short_and_says_nothing_secret(self):
        """表格那一列用**短版**（长句塞进列里只会被截断 —— 截断了等于没显示）。"""
        ep = self.svc.capability("translate").endpoints["cloud"]
        short = SL.credential_source_short(ep)
        self.assertEqual(short, "凭据文件")
        self.assertNotIn(self.secret, short)
        self.assertLess(len(short), 12, f"短版还是太长，列里放不下：{short}")
        # 长版（编辑条下方的状态行）才带路径
        long_text = SL.credential_state(ep)
        self.assertIn("凭据文件", long_text)
        self.assertNotIn(self.secret, long_text)

    # ── 连通性探测 ──────────────────────────────────────────────────────
    def test_probe_openai_reports_text_and_elapsed(self):
        def get(url, timeout):
            return 200, b'{"data": [{"id": "m1"}]}'

        def post(url, body, headers, timeout):
            return 200, b'{"choices": [{"message": {"content": "\u4f60\u597d"}}]}'

        ep = SV.Endpoint(name="e", kind="remote", style="openai",
                         base_url="http://x/v1", model="m1")
        result = SL.probe_endpoint(ep, get=get, post=post)
        self.assertTrue(result["ok"], result)
        self.assertIn("能出字", result["summary"])
        self.assertGreaterEqual(result["ms"], 0)

    def test_probe_openai_treats_empty_content_as_failure(self):
        """**答了 200 但 content 空 = 不许报"通了"**（推理模型的坑，2026-10-20 真机）。"""
        def get(url, timeout):
            return 200, b'{"data": []}'

        def post(url, body, headers, timeout):
            return 200, b'{"choices": [{"message": {"content": ""}}]}'

        ep = SV.Endpoint(name="e", kind="remote", style="openai", base_url="http://x/v1")
        result = SL.probe_endpoint(ep, get=get, post=post)
        self.assertFalse(result["ok"], result)
        self.assertIn("没出字", result["summary"])
        self.assertTrue(result["hint"], "失败了必须给下一步动作")

    def test_probe_openai_treats_a_foreign_shape_as_failure(self):
        """答的不是 OpenAI 形状（百度那类 REST 地址对任何路径都回 200）→ 不许报通了。"""
        def get(url, timeout):
            return 200, b'{"error_code": 123}'

        def post(url, body, headers, timeout):
            return 200, b'{"error_code": 123}'

        ep = SV.Endpoint(name="e", kind="remote", style="openai", base_url="http://x/v1")
        result = SL.probe_endpoint(ep, get=get, post=post)
        self.assertFalse(result["ok"], result)

    def test_probe_never_prints_the_credential(self):
        """服务端把 key 回显出来时，摘要里也不许有它。"""
        secret = "sk-TEST-echo"

        def get(url, timeout):
            return 200, b'{"data": []}'

        def post(url, body, headers, timeout):
            return 200, ('{"choices": [{"message": {"content": "%s"}}]}' % secret).encode()

        ep = SV.Endpoint(name="e", kind="remote", style="openai",
                         base_url="http://x/v1", api_key=secret)
        result = SL.probe_endpoint(ep, get=get, post=post)
        self.assertNotIn(secret, result["summary"], f"摘要里漏出了凭据：{result}")
        self.assertNotIn(secret, result["hint"])

    def test_probe_http_uses_health(self):
        def get(url, timeout):
            self.assertTrue(url.endswith("/health"), url)
            return 200, b'{"ok": true, "model_loaded": true}'

        ep = SV.Endpoint(name="e", kind="local", style="http",
                         base_url="http://127.0.0.1:18090")
        result = SL.probe_endpoint(ep, get=get)
        self.assertTrue(result["ok"], result)
        self.assertIn("model_loaded", result["summary"])

    def test_probe_http_failure_carries_a_next_step(self):
        def get(url, timeout):
            return 404, b""

        ep = SV.Endpoint(name="e", kind="local", style="http", base_url="http://127.0.0.1:18090")
        result = SL.probe_endpoint(ep, get=get)
        self.assertFalse(result["ok"])
        self.assertIn("404", result["summary"])
        self.assertTrue(result["hint"])


if __name__ == "__main__":
    unittest.main()
