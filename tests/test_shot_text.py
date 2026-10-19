# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：F2/F4 的**接线层**（`capsplus/shot_text.py`）。

它要保证四件事：
  ① 窗里的画面（带标注的 surface）能落成 PNG 交给后端 —— OCR 看的是"当前这张图"，不是原图；
  ② 后端偏好（auto / local / cloud）翻得动，而且**下一次按 F2/F4 立刻生效**；
  ③ **云能直读图**（跳过 OCR），本机不行 —— `can_read_images()` 必须如实回答；
  ④ 与标注窗的接口就是那三个方法名，接错了这里就红。

全程不打真网、不起真模型：三个 backend 函数都是注入的桩。
"""

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_text as ST           # noqa: E402
from capsplus.shot_panel import BACKEND_CLOUD, BACKEND_LOCAL   # noqa: E402

try:
    import cairo
    HAVE_CAIRO = True
except Exception:
    cairo = None
    HAVE_CAIRO = False


def _surface(w=40, h=20):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    cr.set_source_rgb(0.9, 0.2, 0.2)
    cr.paint()
    return surface


class TestProviders(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-text-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.seen = {}

    def _providers(self, **over):
        def ocr_fn(path, backend):
            self.seen["ocr"] = (path, backend)
            return {"ok": True, "text": "识别到", "backend": backend or "local", "ms": 1}

        def translate_fn(text, backend):
            self.seen["translate"] = (text, backend)
            return {"ok": True, "src": text, "text": "译", "backend": backend or "local", "ms": 2}

        def translate_image_fn(path):
            self.seen["image"] = path
            return {"ok": True, "src": "原文", "text": "译", "backend": "cloud", "ms": 3}

        return ST.TextProviders(ocr_fn=ocr_fn, translate_fn=translate_fn,
                                translate_image_fn=translate_image_fn, out_dir=self.dir,
                                **over)

    @unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要真写一张 PNG")
    def test_surface_is_written_as_a_png(self):
        providers = self._providers()
        path = providers.png_of(_surface(), "ocr")
        self.assertTrue(pathlib.Path(path).exists())
        self.assertTrue(path.endswith(".png"))
        self.assertEqual(providers.saved, [path], "落过的盘要记着（排查与判据都用它）")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n", "要是真 PNG，后端才读得动")

    @unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要造一张图")
    def test_on_ocr_hands_the_png_and_the_preference(self):
        providers = self._providers()
        providers.ocr_backend = BACKEND_LOCAL
        result = providers.on_ocr(_surface())
        self.assertTrue(result["ok"])
        path, backend = self.seen["ocr"]
        self.assertEqual(backend, BACKEND_LOCAL)
        self.assertTrue(path.startswith(str(self.dir)))

    def test_on_translate_passes_text_and_preference(self):
        providers = self._providers()
        providers.translate_backend = BACKEND_CLOUD
        providers.on_translate("hello")
        self.assertEqual(self.seen["translate"], ("hello", BACKEND_CLOUD))

    @unittest.skipUnless(HAVE_CAIRO, "没有 pycairo：这条要造一张图")
    def test_on_translate_image_sends_the_frame(self):
        providers = self._providers()
        providers.on_translate_image(_surface())
        self.assertTrue(self.seen["image"].endswith(".png"))

    def test_only_cloud_can_read_images(self):
        providers = self._providers()
        self.assertFalse(providers.can_read_images(), "偏好是 auto 时不能承诺直读图")
        providers.translate_backend = BACKEND_LOCAL
        self.assertFalse(providers.can_read_images(), "本机模型实测只吃文本")
        providers.translate_backend = BACKEND_CLOUD
        self.assertTrue(providers.can_read_images())

    def test_switch_flips_the_preference(self):
        providers = self._providers()
        self.assertEqual(providers.switch_translate(BACKEND_LOCAL), BACKEND_CLOUD)
        self.assertEqual(providers.translate_backend, BACKEND_CLOUD)
        self.assertEqual(providers.switch_translate(BACKEND_CLOUD), BACKEND_LOCAL)
        self.assertEqual(providers.switch_translate(""), BACKEND_CLOUD, "没偏好时首切云")

    def test_switch_ocr_is_independent(self):
        providers = self._providers()
        providers.switch_ocr(BACKEND_LOCAL)
        self.assertEqual(providers.ocr_backend, BACKEND_CLOUD)
        self.assertIsNone(providers.translate_backend, "两个偏好互不干扰")

    def test_status_is_readable(self):
        text = self._providers().status()
        self.assertIn("auto", text)
        self.assertEqual(len(text.splitlines()), 2)

    def test_method_names_match_the_window_contract(self):
        """标注窗要的就是这三个名字（接错了这条就红）。"""
        providers = self._providers()
        for name in ("on_ocr", "on_translate", "on_translate_image", "switch_translate"):
            self.assertTrue(callable(getattr(providers, name, None)), f"缺 {name}")

    def test_default_backends_are_the_real_modules(self):
        """默认必须指向真实现（判据里注入桩，但生产代码不许指向桩）。"""
        from capsplus import shot_ocr, shot_translate
        providers = ST.TextProviders()
        self.assertIs(providers.ocr_fn, shot_ocr.ocr)
        self.assertIs(providers.translate_fn, shot_translate.translate)
        self.assertIs(providers.translate_image_fn, shot_translate.translate_image)

    def test_runtime_dir_matches_the_ocr_module(self):
        from capsplus import shot_ocr
        self.assertEqual(ST.runtime_dir(), shot_ocr.runtime_dir())


class TestAnnotatorIntegration(unittest.TestCase):
    """接线层与标注窗的边界：`annotate(..., providers=…)` 要把三个回调都接上。"""

    def test_callbacks_are_wired_from_providers(self):
        import inspect
        from capsplus import shot_annotate
        source = inspect.getsource(shot_annotate.annotate)
        for needle in ("providers.on_ocr", "providers.on_translate",
                       "providers.on_translate_image", "providers.switch_translate"):
            self.assertIn(needle, source, f"annotate() 没把 {needle} 接上")

    def test_region_flow_creates_the_providers(self):
        import inspect
        from capsplus import shot_capture
        source = inspect.getsource(shot_capture.command_region)
        self.assertIn("TextProviders()", source, "Region 流程要自己建接线层（不然 F2/F4 没后端）")
        self.assertIn("providers", source)


if __name__ == "__main__":
    unittest.main()