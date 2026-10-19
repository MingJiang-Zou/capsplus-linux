# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：GNOME 扩展里新增的抓帧/贴图那两段 —— 形状必须与 Python 侧、与这台机的 GJS 实测一致。

为什么需要静态判据（2026-10-19）：扩展跑在 shell 进程里，**改错了不会有任何报错**——
现象只是"截图没反应"。而重新加载扩展要明江注销重登（Wayland 没有 Alt+F2 r），
所以每一次部署都该是"已经用机器核对过形状"的，不能拿他的会话来试错。

这里钉的每一条都对应一个实测过的事实（不是想象的规矩）：
* `Shell.Screenshot.screenshot_area(x, y, w, h, stream[, callback])` —— 签名从本机
  `/usr/lib/gnome-shell/Shell-18.typelib` dump 出来（x,y,w,h:int32 + Gio.OutputStream + callback）；
  闪屏是 shell 自己的 JS 动画（`ui/screenshot.js` 的 `Flashspot`，500ms opacity 255→0），
  所以"不碰那段动画 + 不加 flash 参数"就是明江要的"绝不闪"；
* 不能 `export_object_path` / `register_object` / `return_value`（本机 GJS 里全不可用）；
* `signal_subscribe` 只能 7 实参、回调在最后（仓里 raise-browser 的既有实测）；
* 参数要从 tuple variant 里 `deepUnpack()` 出来（本机实测 `unpack()` 对元组返回 5 个空对象）；
* 产物必须先写 `<out>.part` 再 rename —— 否则调用方会读到半张图。
"""

import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_frame as SF          # noqa: E402

EXT = REPO / "extensions" / "raise-browser@capsplus.example" / "extension.js"


def strip_comments(text: str) -> str:
    """去掉注释再查标识符：注释里提到 `Flashspot` 是**解释**，不是实现。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def call_args(text: str, callee: str) -> list:
    """列出 `callee(...)` 每个调用的**顶层实参个数**（靠括号配对，够用且不引第三方解析器）。"""
    out = []
    for m in re.finditer(re.escape(callee) + r"\(", text):
        i = m.end()
        depth, args, commas = 1, 1, 0
        while i < len(text) and depth:
            ch = text[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    break
            elif ch == "," and depth == 1:
                commas += 1
            i += 1
        args += commas
        if text[m.end():i].strip() == "":
            args = 0
        out.append(args)
    return out


class TestExtensionShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = EXT.read_text(encoding="utf-8")
        cls.code = strip_comments(cls.raw)
        cls.code_nospace = re.sub(r"\s+", "", cls.code)

    def test_extension_still_exports_a_default_class(self):
        self.assertIn("export default class", self.code,
                      "写成模块级 export function enable 会被 shell 静默不加载（2026-10-16 踩过）")

    def test_capture_members_match_the_python_constants(self):
        for member in (SF.M_CAPTURE, SF.M_PIN):
            self.assertIn(f"'{member}'", self.code,
                          f"扩展里的成员名必须与 shot_frame.{member} 逐字一致，"
                          "否则发出去没人订，现象就是「截图没反应」")

    def test_subscribes_with_seven_arguments_callback_last(self):
        counts = call_args(self.code, "signal_subscribe")
        self.assertTrue(all(c == 7 for c in counts),
                        f"本机实测 signal_subscribe 要 7 个实参、回调在最后，实际：{counts}")
        # 第一批的四个入口都必须在位（第 4 步把 M_PIN 加进来了，这条判据跟着一起长了）
        for var in ("M_RAISE", "M_PROBE", "M_CAPTURE", "M_PIN"):
            self.assertIn(f"signal_subscribe(null,IFACE,{var}", self.code_nospace,
                          f"{var} 没真的挂上订阅 ⇒ 发信号没人接，现象就是「截图没反应」")

    def test_never_tries_to_export_a_dbus_object_or_return_a_value(self):
        for banned in ("export_object_path", "register_object", "return_value", "return_error_literal"):
            self.assertNotIn(banned, self.code,
                             f"{banned} 在本机 GJS 里不可用（实测：undefined / 参数形态对不上），"
                             "用了就是静默失败")

    def test_screenshot_area_is_called_with_the_dumped_signature(self):
        counts = call_args(self.code, "screenshot_area")
        self.assertTrue(counts, "抓帧必须真的调 screenshot_area（删掉它 → 本判据红）")
        self.assertTrue(set(counts) <= {5, 6},
                        f"screenshot_area 只接受 (x,y,w,h,stream) 或再加一个回调，实际：{counts}"
                        "；多塞一个布尔'flash'就会与 C 签名错位 —— 而 shell 的闪屏正是那个参数触发的")
        self.assertIn(5, counts, "shell 自己用的就是 5 实参（promisify 后的 Promise 形态）")

    def test_no_flash_animation_anywhere(self):
        for banned in ("Flashspot", "ease(", "Lightbox", "duration:"):
            self.assertNotIn(banned, self.code,
                             f"{banned} 是 shell 那套截图 UI 的闪屏动画写法；我们的抓帧不许带上它"
                             "（明江换掉 Snipaste 就是因为闪）")

    def test_one_screenshot_instance_is_reused(self):
        self.assertEqual(len(re.findall(r"new Shell\.Screenshot\(", self.code)), 1,
                         "每次抓帧 new 一个实例是没必要的开销（shell 自己也只是 new 一个反复用）")

    def test_params_come_from_deep_unpack_not_unpack(self):
        self.assertIn("deepUnpack", self.code)
        self.assertNotRegex(self.code, r"[^p]\.unpack\(\)",
                            "本机实测：tuple variant 的 unpack() 返回 5 个空对象，只有 deepUnpack() 对")

    def test_writes_a_part_file_then_renames(self):
        self.assertIn(".part`", self.code,
                      "必须先写 .part 再 rename：调用方靠「文件出现」判断成功，"
                      "直接写目标文件会让他读到半张图")
        self.assertIn("part.replace(", self.code)
        self.assertNotIn("out.replace(", self.code, "不许直接往目标文件里写")
        self.assertIn("part.move(out", self.code)

    def test_failure_leaves_no_artifact(self):
        self.assertIn("part.delete(null)", self.code,
                      "失败必须把 .part 删掉：留着就会被当成「抓到了」（半张图比没图更坏）")

    def test_pin_uses_add_top_chrome_for_real_always_on_top(self):
        """贴图"真置顶"的唯一实现方式：`addTopChrome` —— shell 自己的 layout.js 注释写死了
        `addChrome` 会放到 top_window_group 下面，那不算置顶。"""
        self.assertIn("addTopChrome", self.code)
        self.assertNotIn("addChrome(", self.code, "addChrome 在 top_window_group 之下，不算真置顶")
        self.assertNotIn("set_keep_above", self.code,
                         "Wayland 下 set_keep_above 什么都不做（X11 时代的 hint）")

    def test_pin_image_bytes_are_rgba_8888_with_the_matching_stride(self):
        self.assertIn("Cogl.PixelFormat.RGBA_8888", self.code)
        self.assertIn("GLib.Bytes.new(raw)", self.code)
        self.assertIn("width * 4", self.code,
                      "RGBA_8888 每像素 4 字节：stride 算错就是花屏/错位")
        self.assertIn("set_content(", self.code)

    def test_pin_does_not_steal_keyboard_focus(self):
        self.assertIn("can_focus: false", self.code,
                      "× 按钮不许拿键盘焦点（明江在干活，贴图不能把输入抢走）")
        self.assertNotIn("grab_key_focus", self.code)
        self.assertNotIn("navigate_focus", self.code)

    def test_pin_cleans_up_after_itself(self):
        self.assertIn("destroyPin", self.code)
        self.assertIn("removeChrome", self.code, "关掉时要 removeChrome，不然 actor 会留在 uiGroup 上")
        self.assertIn("destroyAllPins();", self.code,
                      "扩展停用时必须把自己建的 actor 全部清掉（否则泄漏）")

    def test_pin_writes_an_ack_file(self):
        """成功要写 ack —— 这条链路没有回复通道，capsplus 只能等文件。

        查两件事：① 真的往 ackPath 里写；② 这行**受 `if (ackPath)` 管**而不是被某个恒假条件挡掉
        （第一版只查 ①，把条件改成 `if (false)` 时判据照样绿 —— 那也是有牙没牙的分界）。
        """
        # 只看 `pinImage()` 的函数体：`clearPins()` 里也有一处写 ack，
        # 拿整份文件找下标会把两处混在一起（2026-10-19 加了 ClearPins 之后这里真红过一次）。
        body = _function_body(self.code, "function pinImage(")
        self.assertIn("GLib.file_set_contents(ackPath,", body)
        self.assertIn("if (ackPath)", body,
                      "ack 写入被别的条件挡着（比如恒假），capsplus 就会一直等到超时")
        self.assertLess(body.index("addTopChrome(actor)"),
                        body.index("GLib.file_set_contents(ackPath,"),
                        "顺序必须是「先真挂上去、再写 ack」：反了会报成功但屏幕上什么都没有")

    def test_pin_journal_line_is_parsed_by_python(self):
        sample = "[raise-browser] pin result=ok path=/tmp/r.rgba at=100,50 w=4 h=3 left=1 ms=12"
        rec = SF.parse_pin_log(sample)[0]
        self.assertEqual((rec["result"], rec["left"]), ("ok", "1"))
        self.assertEqual(SF.parse_shot_log(sample), [],
                         "pin 的行不该被当成 shot 的行（判活会看错）")

    def test_journal_line_format_is_the_one_python_parses(self):
        for marker in ("shot result=", "pin result=", "path=", "ms=", "left="):
            self.assertIn(marker, self.code)
        sample = f"[raise-browser] shot result=ok path=/tmp/x.png rect=0,0,8,8 scale=1 ms=5"
        rec = SF.parse_shot_log(sample)[0]
        self.assertEqual((rec["result"], rec["path"]), ("ok", "/tmp/x.png"),
                         "扩展写的行必须能被 shot_frame.parse_shot_log 解出来（跨语言契约）")
        self.assertIn("ready capture=", self.code,
                      "ready 行要报 capture/pin 订阅状态：改完扩展后判断「是不是新代码」就靠它")

    def test_no_deprecated_two_arg_timeout_add(self):
        """本机 GJS 的 `GLib.timeout_add` 是**原始 C 绑定**：至少 3 实参，2 实参直接抛错。"""
        for m in re.finditer(r"timeout_add\(([^)]*)\)", self.code):
            inner = m.group(1)
            self.assertGreaterEqual(inner.count(",") + 1, 3,
                                    f"timeout_add 必须写 3 实参 (priority, interval, cb)，"
                                    f"实际：timeout_add({inner})")

    def test_metadata_uuid_is_unchanged(self):
        import json
        meta = json.loads((EXT.parent / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["uuid"], SF.EXT_UUID,
                         "绝不许新建 uuid：GNOME 只在启动时扫描扩展目录，新 uuid 要明江注销重登")


class TestGuardHasTeeth(unittest.TestCase):
    """反例具型：把实现删掉，上面的判据必须能抓到（判据抓不住东西 = 没有判据）。"""

    SAMPLE = """export default class X {
    _on() {
        Gio.DBus.session.signal_subscribe(null, IFACE, M_CAPTURE, null, null, null, cb);
        shot.screenshot_area(x, y, w, h, stream);
    }
}
"""

    def test_arity_checker_really_counts(self):
        self.assertEqual(call_args(self.SAMPLE, "signal_subscribe"), [7])
        self.assertEqual(call_args(self.SAMPLE, "screenshot_area"), [5])
        self.assertEqual(call_args("f(a, g(b, c), d)", "f"), [3],
                         "嵌套调用里的逗号不许算进来")

    def test_missing_subscribe_is_detected(self):
        broken = self.SAMPLE.replace("signal_subscribe", "// 删了")
        self.assertEqual(call_args(broken, "signal_subscribe"), [])

    def test_wrong_arity_is_detected(self):
        broken = self.SAMPLE.replace("x, y, w, h, stream", "x, y, w, h, stream, true")
        self.assertEqual(call_args(broken, "screenshot_area"), [6],
                         "多一个 flash 参数就是这条判据要抓的形态（它正是闪屏的开关）")


if __name__ == "__main__":
    unittest.main()

def _function_body(code: str, header: str) -> str:
    """从 `header` 那行开始，按花括号配平截出这个函数的**函数体**。

    为什么要它：同一份文件里有多处写 ack / 多处 connect，判据必须只看目标函数那一段
    —— 否则"新增一个函数"就会把老判据带红（或者更糟：老判据被新代码蒙对）。
    """
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
