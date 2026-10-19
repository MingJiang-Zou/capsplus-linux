# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：贴图的**删除路径 / 样式表 / 模态焦点**（2026-10-19 P0）。

明江原话：「贴出来的图 删都删不掉」；随后又纠正：「点击了贴图或者拖动了贴图，焦点就在这个上面了，
然后按 esc 就可以取消贴图」。所以这里钉三件事：

  1. **样式表存在且不许有裸 class**：`stylesheet.css` 以前**根本不存在**，而 extension.js 一直在用
     `capsplus-pin` / `capsplus-pin-close` / `capsplus-pin-box` —— 没有样式表 = `×` 是裸 `St.Button`
     （没尺寸、没背景、没 hover），人根本看不出那儿能点。判据：样式表必须在，且**用到几个 class
     就得定义几个**（反例：删掉任意一条规则 → 红）。
  2. **至少三条能被发现的关闭路径**：hover 工具条的 `×`、右键菜单的「关闭这张」、双击、拿到焦点
     后的 `Esc`、以及 `Caps+Esc` 的全局兜底（`ClearPins` 信号 + `--clear-pins` 子命令）。
     反例：摘掉右键那条 → 红；摘掉 `ClearPins` 订阅或子命令 → 红。
  3. **pushModal / popModal 必须配对**（这条是**灾难级**）：没配对 = shell 卡在模态态，
     明江的键盘进不了任何应用。判据：全文件只允许各出现一次、必须在同一对函数里、且所有销毁路径
     都经过 `destroyPin`。反例：删掉 `Main.popModal(...)` → 红。

另外补一条**语法**判据（`node --check`）：扩展语法错 = 静默不加载，只能靠现象反推
（2026-10-16 就这么白让明江注销重登过一次）。
"""

import pathlib
import re
import shutil
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_frame as SF        # noqa: E402

EXT_DIR = REPO / "extensions" / "raise-browser@capsplus.example"
EXT = EXT_DIR / "extension.js"
CSS = EXT_DIR / "stylesheet.css"

CODE = EXT.read_text(encoding="utf-8")


def code_only(code: str) -> str:
    """去掉整行注释后的正文 —— 注释里就写着我实测到的 `Main.pushModal(...)` 形态，
    拿全文数出现次数会把注释也算成"又一处调用"（这正是这条判据要避免的误报）。"""
    return "\n".join(l for l in code.splitlines()
                      if not l.strip().startswith(("//", "*", "/*")))
STYLE = CSS.read_text(encoding="utf-8") if CSS.exists() else ""


def css_rules(style: str) -> dict:
    """把样式表拆成 `{选择器元组: 规则体}`。

    为什么要真拆：光用 `in` 找字符串会被**复合选择器**骗过去 ——
    删掉独立规则 `.capsplus-pin-box { … }` 之后，`.capsplus-pin-box.capsplus-pin-active { … }`
    还含那个子串，于是"边框/阴影还在不在"的判据照样绿（2026-10-19 反例具型 AB 实测漏过）。
    """
    out = {}
    body = re.sub(r"/\*.*?\*/", "", style, flags=re.S)          # 去掉块注释
    for chunk in body.split("}"):
        if "{" not in chunk:
            continue
        selectors, _, decls = chunk.partition("{")
        decls = decls.strip()
        if not decls:
            continue
        key = tuple(s.strip() for s in selectors.split(",") if s.strip())
        out[key] = decls
    return out


def rule_for(style: str, selector: str) -> str:
    """取**独立规则**（选择器就是它自己）的规则体；没有就空串。"""
    return css_rules(style).get((selector,), "")


def function_body(code: str, header: str) -> str:
    """按花括号配平截出某个函数的函数体（同 `test_shot_extension` 里那个助手，口径一致）。"""
    start = code.index(header)
    brace = code.index("{", start)
    depth = 0
    for idx in range(brace, len(code)):
        if code[idx] == "{":
            depth += 1
        elif code[idx] == "}":
            depth -= 1
            if depth == 0:
                return code[start:idx + 1]
    raise AssertionError(f"{header} 的花括号没配平")


class TestStylesheetExists(unittest.TestCase):
    """基础件：扩展目录下有 `stylesheet.css`，且**用到的 class 都有规则**。"""

    def test_the_file_exists(self):
        self.assertTrue(CSS.exists(),
                        "扩展目录里没有 stylesheet.css ⇒ extension.js 里那些 style_class 全都不生效")

    def test_the_three_original_classes_are_covered(self):
        for cls in ("capsplus-pin", "capsplus-pin-box", "capsplus-pin-close"):
            self.assertTrue(rule_for(STYLE, f".{cls}"),
                            f"样式表里缺**独立**规则 .{cls}（靠复合选择器凑不算）")

    def test_pin_gets_a_border_and_shadow(self):
        block = rule_for(STYLE, ".capsplus-pin-box")
        self.assertIn("border", block, "贴图要有一圈边框，才看得出是「贴」上去的")
        self.assertIn("box-shadow", block, "要投影：不然和桌面壁纸糊在一起")

    def test_close_affordance_is_visible_and_has_a_hover_state(self):
        rules = css_rules(STYLE)
        self.assertIn("min-width", rule_for(STYLE, ".capsplus-pin-tool"),
                      "工具按钮要有尺寸，否则点不着")
        self.assertTrue(any("capsplus-pin-tool" in " ".join(sel) and "hover" in " ".join(sel)
                            for sel in rules),
                        "关闭/工具按钮要有 hover 反馈，否则人看不出能点")

    def test_focused_pin_looks_selected(self):
        self.assertIn("capsplus-pin-active", STYLE,
                      "拿到键盘焦点时要有视觉反馈（明江得知道「键现在归它」）")

    def test_no_naked_style_class_in_extension_js(self):
        """extension.js 里用到的每个 `capsplus-*` class 都必须在样式表里有规则。

        反例就是当时那个 bug：class 写了、样式表没有 ⇒ 一切都照旧（裸控件）。
        """
        used = set(re.findall(r"style_class:\s*'([^']+)'", CODE))
        used |= set(re.findall(r"add_style_class_name\('([^']+)'\)", CODE))
        used = {c for c in used if c.startswith("capsplus-")}
        self.assertTrue(used, "没扫到任何 capsplus-* class？那这条判据失效了")
        missing = sorted(c for c in used if f".{c}" not in STYLE)
        self.assertEqual(missing, [], f"这些 class 在 extension.js 里用了但样式表没定义：{missing}")


class TestClosePaths(unittest.TestCase):
    """至少三条**能被发现**的关闭路径，且全局兜底那两条（信号 + 子命令）都在。"""

    PATHS = {
        "hover 工具条的 ×": "['×', () => destroyPin(pin)]",
        "右键菜单的关闭": "'关闭这张（Esc）', () => destroyPin(pin)",
        "双击关闭": "PIN_DOUBLE_CLICK_MS",
        "拿到焦点后按 Esc": "Clutter.KEY_Escape",
    }

    def test_at_least_three_paths_exist(self):
        found = [name for name, needle in self.PATHS.items() if needle in CODE]
        self.assertGreaterEqual(len(found), 3, f"关闭路径只有 {found} —— 明江要的是「删得掉」")

    def test_each_path_actually_destroys(self):
        for name, needle in self.PATHS.items():
            with self.subTest(path=name):
                self.assertIn(needle, CODE, f"{name} 这条不见了")
        # 落点要真的有四处：工具栏的 ×、右键菜单、双击、Esc（ClearPins 走 destroyAllPins）
        body = code_only(CODE)
        self.assertGreaterEqual(body.count("destroyPin(pin)"), 4,
                                "四条路（工具条 × / 右键菜单 / 双击 / Esc）都要落到 destroyPin(pin)")
        in_pin = function_body(CODE, "function pinImage(")
        self.assertGreaterEqual(in_pin.count("destroyPin(pin)"), 2,
                                "pinImage 里双击与 Esc 两条路都要落到 destroyPin(pin)")

    def test_esc_only_works_with_focus(self):
        """Esc 只在**拿到模态焦点**时到得了 —— 没有焦点时不误伤（口径见 lead 的更正）。"""
        body = function_body(CODE, "function grabPinFocus(")
        self.assertIn("Main.pushModal(", body)
        self.assertIn("key-press-event", CODE)
        self.assertIn("if (event.get_key_symbol() !== Clutter.KEY_Escape)", CODE,
                      "只处理 Esc，别的键不许吞")

    def test_global_fallback_signal_is_subscribed(self):
        self.assertIn("const M_CLEAR = 'ClearPins';", CODE)
        self.assertIn("signal_subscribe(null, IFACE, M_CLEAR", CODE,
                      "`Caps+Esc` 的全局兜底靠 ClearPins 信号，扩展必须订它")

    def test_global_fallback_is_safe_with_no_pins(self):
        body = function_body(CODE, "function clearPins(")
        self.assertIn("destroyAllPins();", body)
        self.assertIn("cleared=${before}", body)
        self.assertNotIn("throw", body, "没贴图时不许抛（全局热键最怕没事就报错）")

    def test_cli_subcommand_exists_and_is_wired(self):
        from capsplus import shot_capture as SC
        args = SC.parse_args(["--clear-pins"])
        self.assertTrue(args.clear_pins)
        self.assertIn("command_clear_pins", SC.__dict__ or dir(SC))
        self.assertEqual(SF.M_CLEAR, "ClearPins")
        self.assertEqual(SF.CLEAR_SIG, "(s)")

    def test_clear_path_destroys_every_pin(self):
        body = function_body(CODE, "function destroyAllPins(")
        self.assertIn("destroyPin(pin)", body)
        self.assertIn("_pins.slice()", body, "遍历副本：destroy 里会改 _pins，直接遍历会漏")


class TestModalFocusIsPaired(unittest.TestCase):
    """灾难级防线：pushModal 与 popModal 必须**一一配对**（不配对 = 键盘进不了任何应用）。"""

    def test_push_and_pop_each_appear_once(self):
        body = code_only(CODE)
        self.assertEqual(body.count("Main.pushModal("), 1,
                         "pushModal 只允许有一处（多一处就多一份配对风险）")
        self.assertEqual(body.count("Main.popModal("), 1,
                         "popModal 只允许有一处")

    def test_they_live_in_the_two_matched_helpers(self):
        self.assertIn("Main.pushModal(", function_body(CODE, "function grabPinFocus("))
        self.assertIn("Main.popModal(", function_body(CODE, "function releasePinFocus("))

    def test_pop_is_guarded_against_double_pop(self):
        body = function_body(CODE, "function releasePinFocus(")
        self.assertIn("if (!pin.grab)", body, "没有 grab 就直接返回（popModal 对未知 grab 会 throw）")
        self.assertIn("pin.grab = null;", body, "先清引用，异常也不至于二次 pop")
        self.assertIn("pin.grab = Main.pushModal(", function_body(CODE, "function grabPinFocus("),
                      "grab 必须存下来 —— popModal 收的就是这个返回值")

    def test_every_destroy_path_releases_focus(self):
        body = function_body(CODE, "function destroyPin(")
        self.assertIn("releasePinFocus(pin)", body,
                      "destroyPin 是所有销毁路径的唯一出口，必须在这里摘焦点")

    def test_disable_destroys_all_pins(self):
        body = function_body(CODE, "    disable() {")
        self.assertIn("destroyAllPins()", body,
                      "扩展停用时若还挂着模态焦点，shell 就卡在模态态了")

    def test_focus_is_released_when_clicking_outside(self):
        self.assertIn("isOutsidePin", CODE,
                      "模态抓的是键盘+鼠标；点到窗外要主动交还焦点，不然下一个窗口像「点不动」")
        self.assertIn("releasePinFocus(pin);", function_body(CODE, "function pinImage("))

    def test_only_one_pin_holds_the_focus(self):
        body = function_body(CODE, "function grabPinFocus(")
        self.assertIn("releasePinFocus(other)", body, "换一张贴图时要把上一张的焦点交还")


class TestDuplicateSubscriptionSafety(unittest.TestCase):
    """热重载会留下**摘不掉的旧订阅**（这版 GJS 没有 signal_remove）⇒ 每个请求跑两遍。

    2026-10-19 真机实测：重载之后每次抓帧都是两行 `started` + 两行 `fail`，明江的截图直接不能用
    （两条 handler 抢同一个 `.part`：后一条触发 shell 的"同一时刻只允许一个截图操作"，
    它的失败清理又删掉 `.part`，前一条 rename 时报"没有那个文件"）。
    所以判据钉两件事：**跨实例去重**（用 shell 的 `global`，不是模块变量）+ `.part` 名字唯一。
    """

    def test_registry_lives_on_global_not_module_state(self):
        body = function_body(CODE, "function duplicateRequest(")
        self.assertIn("global.__capsplusSeen", body,
                      "模块级变量每个实例一份，挡不住重复订阅 —— 必须挂在 shell 的 global 上")

    def test_all_three_handlers_dedupe(self):
        for kind in ("'capture'", "'pin'", "'clear'"):
            with self.subTest(kind=kind):
                self.assertIn(f"duplicateRequest({kind}", CODE, f"{kind} 这条没去重")

    def test_capture_part_file_is_unique_per_call(self):
        body = function_body(CODE, "function captureArea(")
        self.assertIn(".part`", body)
        self.assertNotIn("${outPath}.part`", body,
                         "`.part` 名字要带每次调用唯一的后缀：不然重复订阅会互相删文件")

    def test_dedupe_skips_are_logged(self):
        self.assertIn("dup request=", CODE, "跳过了要留痕，不然排查时看不见")


class TestExtensionSyntax(unittest.TestCase):
    """语法错 = 静默不加载（2026-10-16 白让明江注销重登过一次）。"""

    @unittest.skipUnless(shutil.which("node"), "没有 node：这条语法判据跳过")
    def test_parses_as_an_es_module(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp) / "extension.mjs"
            copy.write_text(CODE, encoding="utf-8")
            proc = subprocess.run([shutil.which("node"), "--check", str(copy)],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"extension.js 语法不过：{proc.stderr.strip()[:400]}")

    def test_still_has_the_default_export(self):
        self.assertIn("export default class RaiseBrowserExtension", CODE,
                      "缺 `export default class` 时 shell 会静默不加载（2026-10-16 的坑）")

    def test_install_script_deploys_the_stylesheet(self):
        """安装脚本必须把 stylesheet.css 一起复制过去。

        扩展目录是**复制**不是软链（`tools/install-extension.sh` 里 `cp`）：脚本漏了样式表，
        仓里改得再对，shell 那边也还是裸控件 ——「改了但没生效」比没改更糟。
        """
        script = (REPO / "tools" / "install-extension.sh").read_text(encoding="utf-8")
        self.assertIn("stylesheet.css", script, "安装脚本没带样式表")

    def test_deployed_copy_is_in_sync_when_it_exists(self):
        """如果这台机上已经部署过，就顺手核一下和仓内是否同一份（漂了就得重新部署）。"""
        dst = pathlib.Path.home() / ".local/share/gnome-shell/extensions/raise-browser@capsplus.example"
        if not dst.exists():
            self.skipTest("这台机没部署过扩展")
        for name in ("extension.js", "stylesheet.css"):
            src_file = EXT_DIR / name
            dst_file = dst / name
            if not dst_file.exists():
                self.skipTest(f"部署目录里还没有 {name}（还没重新部署）")
            with self.subTest(file=name):
                self.assertEqual(src_file.read_text(encoding="utf-8"),
                                 dst_file.read_text(encoding="utf-8"),
                                 f"{name} 部署的那份和仓内的不一致 —— 重新跑 tools/install-extension.sh")

    def test_ready_marker_mentions_the_new_pieces(self):
        """增量标记：改完扩展后看 journal 有没有这行，就知道 shell 里跑的是不是新代码。"""
        self.assertIn("clear=${_subClear ? 'on' : 'off'}", CODE)
        self.assertIn("toolbar+menu+modal=on", CODE)


if __name__ == "__main__":
    unittest.main()