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
            args = SF.pin_signal_args("/tmp/r.rgba", 4, 3, 100, 50, "/tmp/r.ack")
            captured.update(sig=SF.PIN_SIG, payload=args[4], member=args[3])
            self.emits.append(1)

        SP.pin(self.png, 100, 50, emit_fn=emit, wait_fn=lambda p, ms: (False, 1),
               out_dir=self.dir, ack_path=self.ack)
        self.assertEqual(captured["member"], SF.M_PIN)
        self.assertEqual(captured["sig"], "(siiiis)")
        self.assertEqual(captured["payload"], ("/tmp/r.rgba", 4, 3, 100, 50, "/tmp/r.ack"),
                         "顺序必须是 rawPath,w,h,x,y,ackPath —— 扩展那边按这个顺序解包")

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