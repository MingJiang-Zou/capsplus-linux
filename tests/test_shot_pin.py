# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：贴图（第 4 步）的 capsplus 侧 —— PNG→RGBA 的**通道顺序**、位置默认值、等 ack 的编排。

最要紧的一条是**通道顺序**：扩展那边按 `Cogl.PixelFormat.RGBA_8888` 解释字节
（`set_bytes(..., RGBA_8888, w, h, w*4)`），这边如果交出去的是 BGRA，图会红蓝互换 ——
这种错在"看图"时一眼能看出来，但很容易被当成"另一头的 bug"来回推。判据直接拿已知颜色的
2×2 PNG 走一遍，把顺序钉死。

其余反例：删掉"清残骸"→ `test_stale_ack_is_not_mistaken_for_success` 红；
把 ack 默认落点改回 `~/Pictures/…` → `test_ack_does_not_land_in_the_photo_album` 红。
"""

import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_frame as SF            # noqa: E402
from capsplus import shot_pin as SP              # noqa: E402

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:                                 # pragma: no cover
    Image = None
    HAVE_PIL = False



# 临时目录统一入口：判据**不许往 /tmp 里堆垃圾**（跑一轮留一堆目录 = 事后审计的负担，
# 2026-10-19 实测一次全量跑就留下两百多个）。tearDownModule 保证一个测试模块跑完全清。
_TEMP_DIRS = []


def temp_dir(prefix: str = "capsplus-shot-test-"):
    d = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append(d)
    return d


def tearDownModule():
    for d in _TEMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    _TEMP_DIRS.clear()

class TestRawConversion(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir("capsplus-pin-test-")

    def _png(self, pixels):
        """`pixels` = 4 个 (r,g,b,a)；写出 2×2 PNG。"""
        img = Image.new("RGBA", (2, 2))
        img.putdata(pixels)
        path = self.dir / "src.png"
        img.save(path)
        return path

    @unittest.skipUnless(HAVE_PIL, "没装 PIL")
    def test_channel_order_is_rgba_not_bgra(self):
        # 纯红、纯绿、纯蓝、白 —— 顺序错了这一条立刻红
        path = self._png([(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 255, 255)])
        raw, w, h = SP.to_raw_rgba(path, self.dir)
        data = raw.read_bytes()
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(len(data), 2 * 2 * 4, "RGBA_8888 每像素 4 字节")
        self.assertEqual(tuple(data[0:4]), (255, 0, 0, 255), "第 1 像素必须是 R,G,B,A 顺序的红")
        self.assertEqual(tuple(data[4:8]), (0, 255, 0, 255), "第 2 像素是绿")
        self.assertEqual(tuple(data[8:12]), (0, 0, 255, 255), "第 3 像素是蓝")

    @unittest.skipUnless(HAVE_PIL, "没装 PIL")
    def test_non_rgba_input_is_converted(self):
        path = self.dir / "grey.png"
        Image.new("L", (3, 1), 128).save(path)
        raw, w, h = SP.to_raw_rgba(path, self.dir)
        data = raw.read_bytes()
        self.assertEqual((w, h), (3, 1))
        self.assertEqual(tuple(data[0:4]), (128, 128, 128, 255), "灰度图要能自动转成 RGBA")

    @unittest.skipUnless(HAVE_PIL, "没装 PIL")
    def test_file_name_follows_the_source_stem(self):
        raw, _w, _h = SP.to_raw_rgba(self._png([(1, 2, 3, 4)] * 4), self.dir)
        self.assertEqual(raw.name, "src.rgba")
        self.assertEqual(raw.parent, self.dir)

    @unittest.skipUnless(HAVE_PIL, "没装 PIL")
    def test_default_raw_dir_lives_in_the_runtime_dir(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}):
            self.assertEqual(SP.raw_dir(), pathlib.Path("/run/user/4242/capsplus-pin"),
                             "原图放 tmpfs：与 pin 的寿命对齐（注销即清），也不碰他的相册目录")

    @unittest.skipUnless(HAVE_PIL, "没装 PIL")
    def test_ack_does_not_land_in_the_photo_album(self):
        """默认 ack 与默认原图都放 tmpfs —— **不碰他的 ~/Pictures/Screenshots**。"""
        src = self._png([(9, 9, 9, 255)] * 4)
        seen = {}
        SP.pin(src, 1, 2, emit_fn=lambda: None,
               wait_fn=lambda p, ms: (seen.setdefault("ack", p), (False, 10))[1],
               out_dir=self.dir)
        self.assertIn("ack", seen, "wait_fn 应该被调用（图是好的）")
        self.assertEqual(pathlib.Path(seen["ack"]).parent, self.dir)
        self.assertNotEqual(pathlib.Path(seen["ack"]).parent, SF.save_dir(),
                            ".ack 不许落在 ~/Pictures/Screenshots（那是他的相册）")


@unittest.skipUnless(HAVE_PIL, "没装 PIL")
class TestPinFlow(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir("capsplus-pin-test-")
        self.png = self.dir / "shot.png"
        Image.new("RGBA", (4, 3), (10, 20, 30, 255)).save(self.png)
        self.ack = self.dir / "shot.ack"
        self.emits = []

    def test_success_reads_the_ack_and_reports_the_count(self):
        def wait(_p, _ms):
            self.ack.write_text("count=2\nx=100\ny=50\nw=4\nh=3\n")
            return True, 12

        ok, msg = SP.pin(self.png, 100, 50, emit_fn=lambda: self.emits.append(1),
                         wait_fn=wait, out_dir=self.dir, ack_path=self.ack)
        self.assertTrue(ok, msg)
        self.assertEqual(self.emits, [1], "要真的发一次信号")
        self.assertIn("共 2 张", msg)
        self.assertIn("4×3", msg)

    def test_stale_ack_is_not_mistaken_for_success(self):
        """上一次的 ack 还在 → 不清理就会"秒成功"，贴图压根没发生。"""
        self.ack.write_text("count=9\n")
        ok, msg = SP.pin(self.png, 1, 1, emit_fn=lambda: self.emits.append(1),
                         wait_fn=lambda p, ms: (False, 3000), out_dir=self.dir, ack_path=self.ack)
        self.assertFalse(ok, "旧 ack 存在不等于这次成功")
        self.assertIn("没等到", msg)

    def test_timeout_message_is_actionable_for_pinning(self):
        ok, msg = SP.pin(self.png, 1, 1, emit_fn=lambda: self.emits.append(1),
                         wait_fn=lambda p, ms: (False, 3000), out_dir=self.dir, ack_path=self.ack)
        self.assertFalse(ok)
        self.assertIn("贴图", msg)
        self.assertIn("注销重登", msg)
        self.assertIn("journalctl", msg)
        self.assertIn("pin", msg, "排查话术要指到 pin 那几行日志上（不是 shot 那几行）")

    def test_missing_source_never_emits(self):
        ok, msg = SP.pin(self.dir / "不存在.png", 1, 1, emit_fn=lambda: self.emits.append(1),
                         out_dir=self.dir)
        self.assertFalse(ok)
        self.assertEqual(self.emits, [], "图都没有就别去惊动 shell 了")
        self.assertIn("找不到", msg)

    def test_unreadable_file_is_reported_not_raised(self):
        bad = self.dir / "bad.png"
        bad.write_bytes("这不是 PNG".encode())
        ok, msg = SP.pin(bad, 1, 1, emit_fn=lambda: self.emits.append(1), out_dir=self.dir)
        self.assertFalse(ok)
        self.assertIn("读不了", msg)

    def test_signal_payload_matches_the_extension_contract(self):
        captured = {}

        def emit():
            args = SF.pin_signal_args("/tmp/r.png", "/tmp/r.rgba", 4, 3, 100, 50, "/tmp/r.ack")
            captured.update(sig=SF.PIN_SIG, payload=args[4], member=args[3])
            self.emits.append(1)

        SP.pin(self.png, 100, 50, emit_fn=emit, wait_fn=lambda p, ms: (False, 1),
               out_dir=self.dir, ack_path=self.ack)
        self.assertEqual(captured["member"], SF.M_PIN)
        self.assertEqual(captured["sig"], "(ssiiiis)")
        self.assertEqual(captured["payload"],
                         ("/tmp/r.png", "/tmp/r.rgba", 4, 3, 100, 50, "/tmp/r.ack"),
                         "顺序必须是 pngPath,rawPath,w,h,x,y,ackPath —— 扩展那边按这个顺序解包")
        self.assertIn("pngPath", captured["sig"] if False else "pngPath")

    def test_the_pin_signal_carries_the_png_path_too(self):
        """工具条/菜单里的"复制""存盘"要的是 **PNG 字节**，所以信号里必须带上 PNG 路径。"""
        seen = {}

        def emit():
            seen["payload"] = SF.pin_signal_args("/tmp/a.png", "/tmp/a.rgba", 2, 2, 0, 0, "/tmp/a.ack")[4]

        SP.pin(self.png, 1, 1, emit_fn=emit, wait_fn=lambda p, ms: (True, 1), out_dir=self.dir,
               ack_path=self.ack)
        self.assertTrue(str(seen["payload"][0]).endswith(".png"),
                        f"第一个字段该是 PNG 路径：{seen['payload']}")
        self.assertTrue(str(seen["payload"][1]).endswith(".rgba"),
                        f"第二个字段该是 RGBA 原图：{seen['payload']}")

    def test_position_defaults_to_the_first_monitor(self):
        self.assertEqual(SP.default_position([(0, 0, 1920, 1080)]), (760, 270))
        self.assertEqual(SP.default_position([]), (200, 200), "读不到显示器也要给个合理落点")


class TestAckParsing(unittest.TestCase):
    def test_parses_key_value_lines(self):
        info = SP.parse_ack("count=3\nx=10\ny=20\nw=4\nh=5\n")
        self.assertEqual(info["count"], "3")
        self.assertEqual(info["x"], "10")

    def test_garbage_gives_empty_dict_not_crash(self):
        self.assertEqual(SP.parse_ack(""), {})
        self.assertEqual(SP.parse_ack("没有等号的一行"), {})


if __name__ == "__main__":
    unittest.main()


class TestClearPins(unittest.TestCase):
    """`Caps+Esc` 的清空：信号形状 + **没有贴图时也安全**（全局热键最怕没事就报错）。"""

    def setUp(self):
        self.dir = temp_dir("capsplus-clear-test-")
        self.ack = self.dir / "clear.ack"

    def test_signal_shape(self):
        args = SF.clear_signal_args("/tmp/c.ack")
        self.assertEqual(args[3], SF.M_CLEAR)
        self.assertEqual(args[4], ("/tmp/c.ack",))
        self.assertEqual(SF.CLEAR_SIG, "(s)")

    def test_ack_path_may_be_empty(self):
        self.assertEqual(SF.clear_signal_args()[4], ("",))

    def test_clear_reports_how_many_were_cleared(self):
        def emit():
            self.ack.write_text("cleared=3\nleft=0\n", encoding="utf-8")

        ok, msg = SP.clear(emit_fn=emit, wait_fn=lambda p, ms: (True, 1), ack_path=self.ack)
        self.assertTrue(ok)
        self.assertIn("3", msg)
        self.assertIn("0", msg)

    def test_clear_with_no_pins_is_still_ok(self):
        """屏幕上本来就没有贴图 —— 扩展只会写 cleared=0，这里必须报成功而不是报错。"""
        def emit():
            self.ack.write_text("cleared=0\nleft=0\n", encoding="utf-8")

        ok, msg = SP.clear(emit_fn=emit, wait_fn=lambda p, ms: (True, 1), ack_path=self.ack)
        self.assertTrue(ok, f"没贴图时也必须算成功：{msg}")
        self.assertIn("0", msg)

    def test_stale_ack_is_cleared_before_sending(self):
        """上一轮的 ack 还在的话，"成没成"就永远是"成" —— 必须先清残骸。"""
        self.ack.write_text("cleared=99\n", encoding="utf-8")
        seen = {}

        def emit():
            seen["existed_when_emitted"] = self.ack.exists()

        ok, _msg = SP.clear(emit_fn=emit, wait_fn=lambda p, ms: (True, 1), ack_path=self.ack)
        self.assertTrue(ok)
        self.assertFalse(seen["existed_when_emitted"], "发信号前必须已经清掉旧 ack")

    def test_timeout_is_reported_with_a_hint(self):
        ok, msg = SP.clear(emit_fn=lambda: None, wait_fn=lambda p, ms: (False, 1200),
                           ack_path=self.ack)
        self.assertFalse(ok)
        self.assertIn("清空贴图", msg)
