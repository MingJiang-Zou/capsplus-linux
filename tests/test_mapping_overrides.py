"""用户覆盖层（`~/.config/capsplus/mappings.toml`）测试：合并、校验、逐字节等价。

三条要害：
  ① 没有覆盖层时渲染结果与 `render()` **逐字节相同** —— 不许破坏既有的
     `test_binding_sets_are_identical` 验收（本文件 test_no_overrides_is_byte_identical 锁住）；
  ② 覆盖层里写了未知键 -> 该条不生效 + 报错（宁可少改，也不要写出坏配置）；
  ③ 覆盖值非法（空 / 带空格 / 括号没闭合）-> 同样拦下。
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import mapping_data as MD                 # noqa: E402
from capsplus.apply import render_with_overrides        # noqa: E402
from capsplus.mapping_overrides import (                # noqa: E402
    OverrideError, load_and_merge, load_known_keys, load_overrides,
    merge_overrides, render_overrides, save_overrides, validate_action,
)
from capsplus.render import binding_rows, normalized_bindings, render  # noqa: E402

# 校验用的"键名表"直接取 keyd 自己的键名清单（`keyd.rvaiya list-keys`）——
# 测试里手写一份键名容易写错一个，把"覆盖层被正确接受"这条判据变成假红。
KNOWN = load_known_keys() or {
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "enter", "backspace", "delete", "space", "tab", "esc", "e", "t", "c", "x", "v",
    "semicolon", "dot", "comma", "slash"}


class OverrideTempMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "mappings.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, body: str) -> Path:
        self.path.write_text(body, encoding="utf-8")
        return self.path

    def load(self):
        return load_overrides(self.path, KNOWN)

    def merged(self):
        return load_and_merge(self.path, KNOWN)


class TestMerge(OverrideTempMixin):
    def test_no_overrides_is_byte_identical(self):
        """没有覆盖层 = 与仓库默认渲染逐字节相同（既有验收不许被破坏）。"""
        data, ov = self.merged()          # 文件不存在
        self.assertTrue(ov.empty)
        self.assertEqual(render(data), render())

    def test_empty_file_is_byte_identical(self):
        self.write("# 只有注释\n")
        data, ov = self.merged()
        self.assertTrue(ov.empty)
        self.assertEqual(render(data), render())

    def test_override_replaces_action_but_keeps_position(self):
        self.write('[capl]\ne = "pageup"\n')
        data, ov = self.merged()
        rows = binding_rows("capl", data)
        self.assertEqual(rows[0], ("e", "pageup", "光标 上/下/左/右", "覆盖"))
        # 位置不动：e 仍然是 [capl] 段的第一条；条数也不变
        self.assertEqual(len(rows), len(binding_rows("capl")))
        self.assertEqual(rows[1][0], "d")

    def test_untouched_keys_keep_repo_default(self):
        self.write('[capl]\ne = "pageup"\n')
        data, _ = self.merged()
        rows = dict((k, (a, s)) for k, a, _n, s in binding_rows("capl", data))
        self.assertEqual(rows["d"], ("down", "默认"))

    def test_new_key_is_appended_to_section(self):
        self.write('[capl]\nz = "pageup"\n')
        data, _ = self.merged()
        rows = binding_rows("capl", data)
        self.assertEqual(rows[-1][0], "z")
        self.assertEqual(rows[-1][1], "pageup")
        self.assertIn(("capl", "z", "pageup"), normalized_bindings(render(data)))

    def test_alt_layer_override(self):
        # 注意：带 + 的段名在 TOML 里必须加引号，裸写 [capl+alt] 是语法错误
        self.write('["capl+alt"]\nt = "up"\n')
        data, _ = self.merged()
        alt = dict((k, a) for k, a, _n, _s in binding_rows("capl+alt", data))
        self.assertEqual(alt["t"], "up")
        capl = dict((k, a) for k, a, _n, _s in binding_rows("capl", data))
        self.assertEqual(capl["t"], "macro(up up up up up up up up up up)")   # 基础层不受影响

    def test_composite_layer_still_after_member_layer(self):
        self.write('[capl]\ne = "pageup"\n["capl+alt"]\nt = "up"\n')
        text = render(self.merged()[0])
        self.assertLess(text.index("[capl]"), text.index("[capl+alt]"))

    def test_overrides_make_rendered_diff(self):
        """有覆盖时 render 输出必须真的变（否则"覆盖生效"是假的）。"""
        self.write('[capl]\ne = "pageup"\n')
        before = render()
        after = render(self.merged()[0])
        self.assertNotEqual(before, after)
        # [capl+alt] 里也绑了 e（默认 macro(up up up)），所以只看 [capl] 那一段。
        # 注意不能用 split("[capl+alt]")：渲染头注释里也写着这个字串，会切错位置。
        capl_lines, inside = [], False
        for line in after.splitlines():
            if line.startswith("["):
                inside = line.strip() == "[capl]"
                continue
            if inside:
                capl_lines.append(line)
        diff = [l for l in capl_lines if l.startswith("e = ")]
        self.assertEqual(diff, ["e = pageup"])


class TestBadInput(OverrideTempMixin):
    def test_unknown_key_in_override_is_rejected(self):
        """默认里没有的键是允许的（追加），但**段名**不认识要拦下。"""
        self.write('[nosuchlayer]\ne = "up"\n')
        ov = self.load()
        self.assertTrue(ov.errors)
        self.assertIn("nosuchlayer", ov.errors[0])
        self.assertTrue(ov.empty)

    def test_illegal_value_with_space_is_rejected(self):
        self.write('[capl]\ne = "up down"\n')
        ov = self.load()
        self.assertTrue(ov.errors)
        self.assertIn("空格", ov.errors[0])
        self.assertTrue(ov.empty)

    def test_empty_value_is_rejected(self):
        self.write('[capl]\ne = ""\n')
        self.assertTrue(self.load().errors)

    def test_unclosed_macro_is_rejected(self):
        self.write('[capl]\ne = "macro(up up"\n')
        ov = self.load()
        self.assertTrue(ov.errors)
        self.assertIn("括号", ov.errors[0])

    def test_unknown_keyname_reports(self):
        self.write('[capl]\ne = "nosuchkey"\n')
        ov = self.load()
        self.assertTrue(ov.errors)
        self.assertIn("不认识的键名", ov.errors[0])

    def test_non_string_value_is_rejected(self):
        self.write('[capl]\ne = 3\n')
        self.assertTrue(self.load().errors)

    def test_broken_toml_does_not_raise(self):
        self.write('[capl\ne = "up"\n')
        ov = self.load()
        self.assertTrue(ov.error)
        self.assertTrue(ov.empty)
        data = merge_overrides(ov)              # 坏文件 = 当没有覆盖层
        self.assertEqual(render(data), render())

    def test_render_with_overrides_refuses_bad_file(self):
        """apply 路径：覆盖层有问题必须**拒绝执行**，不能写出半份配置。"""
        import capsplus.mapping_overrides as MO
        saved = MO.OVERRIDES_PATH
        MO.OVERRIDES_PATH = self.path
        try:
            self.write('[capl]\ne = "up down"\n')
            with self.assertRaises(OverrideError):
                render_with_overrides()
            self.write('[capl]\ne = "pageup"\n')
            self.assertIn("e = pageup", render_with_overrides())
        finally:
            MO.OVERRIDES_PATH = saved

    def test_validate_action_accepts_normal_values(self):
        for action in ("up", "S-up", "C-c", "home", "macro(up up)", "pageup",
                       "command(/bin/true)", "overload(capl, capslock)"):
            self.assertIsNone(validate_action(action, KNOWN), action)

    def test_main_section_is_not_overridable(self):
        self.write('[main]\ncapslock = "up"\n')
        ov = self.load()
        self.assertTrue(ov.errors)


class TestWrite(OverrideTempMixin):
    def test_render_and_reload_roundtrip(self):
        values = {"capl": {"e": "pageup", "t": "pagedown"}}
        text = render_overrides(values)
        self.assertIn("[capl]", text)
        self.assertIn('e = "pageup"', text)
        self.path.write_text(text, encoding="utf-8")
        ov = self.load()
        self.assertEqual(ov.count, 2)
        self.assertEqual(ov.get("capl", "e"), "pageup")
        data, _ = self.merged()
        rendered = render(data)
        self.assertIn("e = pageup\n", rendered)
        self.assertIn("t = pagedown\n", rendered)

    def test_save_is_atomic_and_reparses(self):
        save_overrides({"capl": {"e": "end"}}, self.path)
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())
        self.assertEqual(self.load().get("capl", "e"), "end")

    def test_composite_section_roundtrips(self):
        """[capl+alt] 不是合法 TOML 裸键：写出来必须带引号，且能被读回来（否则增强层覆盖是假的）。"""
        text = render_overrides({"capl+alt": {"t": "up"}})
        self.assertIn('["capl+alt"]', text)
        self.path.write_text(text, encoding="utf-8")
        self.assertEqual(self.load().get("capl+alt", "t"), "up")

    def test_bare_composite_section_gives_hint(self):
        """用户手写裸 [capl+alt] 时，错误信息要点破该加引号（只说"TOML 语法错误"等于没说）。"""
        self.write('[capl+alt]\nt = "up"\n')
        ov = self.load()
        self.assertIn("引号", ov.error)

    def test_empty_overrides_renders_header_only(self):
        save_overrides({}, self.path)
        self.assertTrue(self.load().empty)

    def test_defaults_untouched_by_module_defaults(self):
        """合并是**只读**的：不许把用户覆盖写回 mapping_data 的常量。"""
        before = dict((k, a) for k, a, _n, _s in binding_rows("capl"))
        self.write('[capl]\ne = "pageup"\n')
        self.merged()
        after = dict((k, a) for k, a, _n, _s in binding_rows("capl"))
        self.assertEqual(before, after)
        self.assertEqual(MD.CAPL[1], ("e", "up"))


class TestSyntaxGuard(OverrideTempMixin):
    def test_macro_override_has_no_trailing_comment(self):
        """覆盖成 macro 也不能带行尾注释（keyd 会吃掉整行剩下的内容）。"""
        self.write('[capl]\nt = "macro(up up up)"\n')
        text = render(self.merged()[0])
        for line in text.splitlines():
            if line.strip().startswith("t = macro("):
                self.assertTrue(line.strip().endswith(")"))
                break
        else:
            self.fail("覆盖后的 t 行没渲染出来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
