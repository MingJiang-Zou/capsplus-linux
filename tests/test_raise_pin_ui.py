# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：贴图的**删除路径 / 样式表 / 模态焦点**（2026-10-19 P0）。

用户原话：「贴出来的图 删都删不掉」；随后又纠正：「点击了贴图或者拖动了贴图，焦点就在这个上面了，
然后按 esc 就可以取消贴图」。所以这里钉三件事：

  1. **样式表存在且不许有裸 class**：`stylesheet.css` 以前**根本不存在**，而 extension.js 一直在用
     `capsplus-pin` / `capsplus-pin-close` / `capsplus-pin-box` —— 没有样式表 = `×` 是裸 `St.Button`
     （没尺寸、没背景、没 hover），人根本看不出那儿能点。判据：样式表必须在，且**用到几个 class
     就得定义几个**（反例：删掉任意一条规则 → 红）。
  2. **至少三条能被发现的关闭路径**：hover 工具条的 `×`、右键菜单的「关闭这张」、双击、拿到焦点
     后的 `Esc`、以及 `Caps+Esc` 的全局兜底（`ClearPins` 信号 + `--clear-pins` 子命令）。
     反例：摘掉右键那条 → 红；摘掉 `ClearPins` 订阅或子命令 → 红。
  3. **pushModal / popModal 必须配对**（这条是**灾难级**）：没配对 = shell 卡在模态态，
     用户的键盘进不了任何应用。判据：全文件只允许各出现一次、必须在同一对函数里、且所有销毁路径
     都经过 `destroyPin`。反例：删掉 `Main.popModal(...)` → 红。

另外补一条**语法**判据（`node --check`）：扩展语法错 = 静默不加载，只能靠现象反推
（2026-10-16 就这么白让用户注销重登过一次）。
"""


import json
import pathlib
import re
import shutil
import subprocess
import tempfile
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
                      "拿到键盘焦点时要有视觉反馈（用户得知道「键现在归它」）")

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
        self.assertGreaterEqual(len(found), 3, f"关闭路径只有 {found} —— 用户要的是「删得掉」")

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

    2026-10-19 真机实测：重载之后每次抓帧都是两行 `started` + 两行 `fail`，用户的截图直接不能用
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
    """语法错 = 静默不加载（2026-10-16 白让用户注销重登过一次）。"""

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

# node 侧驱动：把抠出来的真代码跑一遍，走"进入贴图 → 离开贴图 → 进工具条 → 点按钮"的序列
DRIVER = """
__BODY__

let state = {visible: false, deadline: 0};
const step = (event, now) => {
    const r = hoverPlan(state, event, now);
    state = r.state;
    return r.action;
};
const log = [];
log.push(['pin-enter', step('pin-enter', 0), state.visible]);
log.push(['pin-leave', step('pin-leave', 10), state.visible]);
log.push(['tick-before-deadline', step('tick', 200), state.visible]);
log.push(['bar-enter', step('bar-enter', 240), state.visible]);
log.push(['tick-after-cancel', step('tick', 3000), state.visible]);
log.push(['bar-leave', step('bar-leave', 3100), state.visible]);
log.push(['tick-early', step('tick', 3200), state.visible]);
log.push(['tick-expired', step('tick', 3400), state.visible]);
const geo = toolbarGeometry(400, 300, 106, 34, 200, 100, 1920, 1080);
const geoBottom = toolbarGeometry(400, 300, 106, 34, 200, 380, 1920, 700);   // 贴图贴近下沿
const geoEdge = toolbarGeometry(400, 300, 106, 34, 1800, 100, 1920, 1080);   // 贴右边界
const buttons = toolbarButtons(geo, 34, 3);
const hit = hitButton(buttons, geo.x + 2, geo.y + 10);
const hitClose = hitButton(buttons, geo.x + 2 * 34 + 2, geo.y + 10);
const miss = hitButton(buttons, geo.x - 50, geo.y - 50);
console.log('SUMMARY ' + JSON.stringify({log, geo, geoBottom, geoEdge, hit, hitClose, miss,
                                        grace: HOVER_GRACE_MS, barW: 106, barH: 34}));
"""


def strip_comments(js: str) -> str:
    """去掉 JS 注释再做文本断言 —— 注释里常写着"为什么不用 xxx"，按文本扫会误伤（踩过两次）。"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


def _extract_functions(source: str, names) -> str:
    """从 extension.js 里抠出几个函数的**源码**（拿到 node 里去真跑）。

    为什么抠源码跑而不是在 Python 里重写一遍：重写就是"第二份真相" —— extension.js 改了、
    判据里的副本没改，判据会绿着骗人。抠出来跑 = 判的就是真代码。
    """
    out = []
    for name in names:
        m = re.search(r"^(?:const|function|let)\s+" + name + r"\b", source, re.M)
        if not m:
            raise AssertionError("extension.js 里找不到 " + name)
        start = m.start()
        if source[start:].startswith(("const", "let")):
            out.append(source[start:source.index(";", source.index("=", start)) + 1])
            continue
        depth, i = 0, source.index("{", start)
        for j in range(i, len(source)):
            if source[j] == "{":
                depth += 1
            elif source[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append(source[start:j + 1])
                    break
    return "\n\n".join(out)


def _run_node(script_body: str) -> subprocess.CompletedProcess:
    """把一段 JS 写成临时文件、用 node 真跑一遍。

    ⚠ **跑完要清掉那个临时目录**（2026-10-20 发现的：原来 `mkdtemp` 之后没人收，
    `/tmp/capsplus-extjs-*` 一天堆了 **141 个**、每个 282 字节 —— 机器上"散件"就是这么攒出来的）。
    清场用 `shutil.rmtree`，不是"删除"，但同样属于中间产物：**跑完即清**是这一层的义务
    （中间的 JS 源码没有必要留；要复算的人 poke 一下 `_node_source()` 就有了）。
    """
    import tempfile
    directory = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-extjs-"))
    try:
        path = directory / "probe.mjs"
        path.write_text(script_body, encoding="utf-8")
        return subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


HOVER_FNS = ["HOVER_GRACE_MS", "TOOLBAR_GAP", "toolbarGeometry", "toolbarButtons",
             "hitButton", "hoverPlan"]


class TestNodeProbeCleansUpAfterItself(unittest.TestCase):
    """`_run_node` 跑完必须把临时目录收掉（2026-10-20：一天攒了 141 个 `/tmp/capsplus-extjs-*`）。"""

    def test_temp_dir_is_removed_after_the_run(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["path"] = pathlib.Path(argv[1])
            self.assertTrue(seen["path"].exists(), "跑之前那个 JS 文件得真写出来了")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        real = subprocess.run
        subprocess.run = fake_run
        try:
            _run_node("console.log(1)")
        finally:
            subprocess.run = real
        self.assertFalse(seen["path"].exists(), f"临时文件还在：{seen['path']}")
        self.assertFalse(seen["path"].parent.exists(), f"临时目录还在：{seen['path'].parent}")

    def test_temp_dir_is_removed_even_when_node_fails(self):
        """node 自己炸了（超时）也要把临时目录收掉 —— 只写在 happy path 上等于没收。"""
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        before = set(pathlib.Path(tempfile.gettempdir()).glob("capsplus-extjs-*"))
        real = subprocess.run
        subprocess.run = fake_run
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_node("while(1){}")
        finally:
            subprocess.run = real
        after = set(pathlib.Path(tempfile.gettempdir()).glob("capsplus-extjs-*"))
        self.assertEqual(after - before, set(), f"超时那次把临时目录留下了：{after - before}")


class TestHoverToolbarIsReachable(unittest.TestCase):
    """**hover 工具条点不到**（2026-10-19 真机）：鼠标移上去闪一下就消失、点不着。

    真机序列：进入贴图 → 离开贴图 → 进入工具条 → 点复制。判据把 extension.js 里的
    `hoverPlan` / `toolbarGeometry` / `hitButton` **抠出来在 node 里真跑**，断言：
      ① 整条序列里工具条**一直是 visible**（离开贴图那一步不许立刻 hide）；
      ② 点击**真的落到按钮上**（命中测试找到 copy / ×）；
      ③ 工具条在贴图**内部**（否则鼠标必须离开 actor 才够得着 ⇒ 永远"闪现抓不住"）。
    反例：① 去掉宽限（立即 hide）→ 必红；② 工具条挪回贴图外面 → 必红。
    """

    def setUp(self):
        self.src = (REPO / "extensions" / "raise-browser@capsplus.example" /
                    "extension.js").read_text(encoding="utf-8")

    def _summary(self):
        body = _extract_functions(self.src, HOVER_FNS)
        proc = _run_node(DRIVER.replace("__BODY__", body))
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("SUMMARY ")][0]
        return json.loads(line[len("SUMMARY "):])

    def test_sequence_keeps_the_toolbar_visible_and_clicks_land(self):
        data = self._summary()
        visible_throughout = {"pin-enter", "pin-leave", "tick-before-deadline", "bar-enter",
                              "tick-after-cancel", "bar-leave", "tick-early"}
        for name, _action, visible in data["log"]:
            if name in visible_throughout:
                self.assertTrue(visible, "「" + name + "」之后工具条被藏了 —— 鼠标到不了按钮")
            if name == "tick-expired":
                self.assertFalse(visible, "宽限到期之后该藏")
        self.assertGreaterEqual(data["grace"], 200, "宽限期要 200–300ms")
        self.assertLessEqual(data["grace"], 300)
        self.assertEqual(data["hit"], 0, "点不到复制按钮")
        self.assertEqual(data["hitClose"], 2, "× 要一击即中")
        self.assertEqual(data["miss"], -1, "没点到就该返回 -1（别误触发）")

    def test_toolbar_never_overlaps_the_pin(self):
        """**工具条与贴图矩形零重叠**（用户 2026-10-20：摆里面"会挡住贴图部分内容"）。

        坐标断言（不是"看起来没压"）：工具条矩形与贴图矩形必须有**空隙**、不许相交；
        位置默认在下方，下面放不下翻上方，贴到屏幕边缘时还得留在 stage 里。
        """
        data = self._summary()
        geo, bar_w, bar_h = data["geo"], data["barW"], data["barH"]
        pin_w, pin_h = 400, 300

        def overlaps(g, w=bar_w, h=bar_h):
            return g["x"] < pin_w and g["x"] + w > 0 and g["y"] < pin_h and g["y"] + h > 0
        self.assertFalse(overlaps(geo), f"工具条压在贴图上了：{geo}")
        self.assertFalse(geo["overlaps"], "函数自己报的 overlaps 也该是 false")
        self.assertGreaterEqual(geo["y"], pin_h + 1, "默认该在下方（留空隙）")
        self.assertLess(data["geoBottom"]["y"], 0, "底下放不下时该翻到上方")
        self.assertFalse(overlaps(data["geoBottom"]), "翻到上方后也不许压住贴图")
        self.assertGreaterEqual(data["geoEdge"]["x"] + bar_w, 0, "越界要拉回可见范围")
        self.assertLessEqual(200 + data["geoEdge"]["x"] + bar_w, 1920, "整条工具条要在屏幕内")
        # 接线也要用它的结果（摆回内部 = 反例）
        bar = self.src[self.src.index("function buildPinToolbar"):]
        bar = bar[:bar.index("\n}")]
        self.assertIn("bar.set_position(geo.x, geo.y)", strip_comments(bar),
                      "工具条没按 toolbarGeometry 的结果摆")

    def test_leave_does_not_hide_immediately_in_the_source(self):
        """源码层面再钉一次：贴图的 leave-event 里**不许**直接 `toolbar.hide()`。"""
        block = self.src[self.src.index("actor.connect('leave-event'"):]
        block = block[:block.index("});") + 3]
        code = strip_comments(block)
        self.assertIn("HOVER_GRACE_MS", code, "leave 时该起算宽限")
        # `toolbar.hide()` 只允许出现在**宽限到期之后**（时间回调里），不许在 leave 当场就调
        self.assertLess(code.index("HOVER_GRACE_MS"), code.index("toolbar.hide()"),
                        "leave-event 里当场就 hide 了 —— 宽限被绕过")
        head = code[:code.index("HOVER_GRACE_MS")]
        self.assertNotIn("toolbar.hide()", head, "宽限之前就 hide 了")


class TestPinsAreHiddenDuringCapture(unittest.TestCase):
    """**遮罩压在贴图之上**（用户 2026-10-19 口径）：抓帧期间贴图必须真的藏起来。

    为什么不做"开关"（他嫌复杂）：贴图在 shell 图层、遮罩是 GTK 窗口，物理上抬不过去；
    等价做法只有"抓帧前隐藏 → 抓完恢复"。副作用：**新截图不会把旧贴图拍进去**（也是他最初的诉求）。
    """

    def setUp(self):
        self.src = (REPO / "extensions" / "raise-browser@capsplus.example" /
                    "extension.js").read_text(encoding="utf-8")

    def _area(self):
        area = self.src[self.src.index("function captureArea"):]
        return area[:area.index("/* ------------------------------------------------------")]

    def test_visibility_plan_moves_only_visible_pins(self):
        body = _extract_functions(self.src, ["pinVisibilityPlan"])
        proc = _run_node(body + "\nconsole.log('PLAN ' + JSON.stringify(pinVisibilityPlan([true, false, true])));\n")
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        plan = json.loads([ln for ln in proc.stdout.splitlines()
                           if ln.startswith("PLAN ")][0][5:])
        self.assertEqual(plan["hide"], [0, 2], "只动当时可见的那些")
        self.assertEqual(plan["restore"], [0, 2], "恢复的集合与隐藏的集合一致")
        self.assertNotIn(1, plan["restore"], "本来就不可见的贴图不许被 show 出来")

    def test_hide_happens_before_the_capture_call(self):
        """顺序是判据：**先 hide 再抓帧**（否则旧贴图会被拍进新截图）。"""
        head = self.src[self.src.index("function captureArea"):]
        head = head[:head.index("callArea(shooter()")]
        self.assertIn("hidePinsForCapture()", head, "抓帧前没有隐藏贴图")
        self.assertLess(head.index("hidePinsForCapture()"), head.index("part.replace"),
                        "隐藏要在开流/抓帧之前")

    def test_every_exit_path_restores(self):
        """所有出口都要恢复：成功 / 抓帧失败 / 关流失败 / 改名失败 / 开流失败 + 超时兜底。"""
        area = self._area()
        exits = area.count("shot result=")
        self.assertGreaterEqual(area.count("restore()"), exits,
                                f"出口 {exits} 个、恢复只有 {area.count('restore()')} 次"
                                " —— 有一条路会把贴图留在隐藏态")
        self.assertRegex(area, r"timeout_add_seconds\(\s*(GLib\.PRIORITY_DEFAULT\s*,\s*)?30",
                         "没有超时兜底：回调不来就永久隐身")

    def test_hiding_is_real_not_transparent(self):
        """**必须真 hide**：冻结是合成后抓的，`set_opacity(0)` 一样会被拍进去。"""
        body = strip_comments(_extract_functions(
            self.src, ["hidePinsForCapture", "restorePinsAfterCapture"]))
        self.assertIn(".hide()", body)
        self.assertIn(".show()", body)
        self.assertNotIn("set_opacity", body, "用透明度冒充隐藏了 —— 抓帧照样拍得到")

    def test_disable_clears_hover_timers(self):
        self.assertIn("restoreAllPins()", self.src, "停用时没收拾 hover 定时器")
        disable = self.src[self.src.index("    disable() {"):]
        disable = disable[:disable.index("\n    }")]
        self.assertIn("restoreAllPins()", disable, "disable() 里没调 restoreAllPins()")
