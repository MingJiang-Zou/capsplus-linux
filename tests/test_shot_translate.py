# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：翻译（第二批 F2）—— 本机优先、云可直读图、失败话术可操作、凭据**不打印**。

四条口径（每条都有红法）：
  ① **本机优先**：`auto` 下本机端点活着就用本机（离线、不花他的钱）；
  ② **云端能直读图**：`translate_image()` 必须真的把图（data URL）发出去 —— 判据直接抓请求体；
  ③ **base_url 要补兼容路径**：`~/.bailian/config.json` 记的是站点根，直接拼 `/chat/completions`
     会 404（2026-10-19 实测）⇒ `normalize_base_url()` 补 `/compatible-mode/v1`；
  ④ **失败话术可操作**：带上下一步命令/端口/文件路径，不能只说"失败了"。

全程**不打真网**：post / get 都是注入的桩。
"""

import json
import os
import pathlib
import sys
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_translate as T       # noqa: E402

CLOUD = {"base_url": "https://example.invalid/compatible-mode/v1", "api_key": "k",
         "model": "deepseek-v4.1-flash", "config_file": "/tmp/none.json"}


def reply(text="译好了"):
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode("utf-8")


class TestBaseUrlNormalisation(unittest.TestCase):
    def test_site_root_gets_the_compatible_path(self):
        self.assertEqual(T.normalize_base_url("https://x.aliyuncs.com"),
                         "https://x.aliyuncs.com/compatible-mode/v1")

    def test_already_compatible_is_left_alone(self):
        url = "https://x.aliyuncs.com/compatible-mode/v1"
        self.assertEqual(T.normalize_base_url(url), url)

    def test_plain_v1_is_left_alone(self):
        self.assertEqual(T.normalize_base_url("http://127.0.0.1:12087/v1"),
                         "http://127.0.0.1:12087/v1")

    def test_trailing_slash_and_empty(self):
        self.assertEqual(T.normalize_base_url("https://x.com/"), "https://x.com/compatible-mode/v1")
        self.assertEqual(T.normalize_base_url(""), "")
        self.assertEqual(T.normalize_base_url(None), "")


class TestCloudConfig(unittest.TestCase):
    def test_env_overrides_file(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_CLOUD_KEY": "envkey",
                                          "CAPSPLUS_CLOUD_BASE_URL": "https://env.example",
                                          "CAPSPLUS_CLOUD_MODEL": "m1"}):
            cfg = T.cloud_config("/tmp/definitely-missing.json")
        self.assertEqual(cfg["api_key"], "envkey")
        self.assertEqual(cfg["base_url"], "https://env.example/compatible-mode/v1")
        self.assertEqual(cfg["model"], "m1")

    def test_reads_the_active_profile(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "active_config": "token-plan",
                "token-plan": {"api_key": "filekey", "base_url": "https://file.example"}}),
                encoding="utf-8")
            cfg = T.cloud_config(str(path))
        self.assertEqual(cfg["api_key"], "filekey")
        self.assertEqual(cfg["base_url"], "https://file.example/compatible-mode/v1")
        self.assertEqual(cfg["model"], T.DEFAULT_CLOUD_MODEL)

    def test_missing_file_is_empty_not_an_exception(self):
        cfg = T.cloud_config("/tmp/definitely-missing.json")
        self.assertEqual((cfg["api_key"], cfg["base_url"]), ("", ""))

    def test_local_model_default(self):
        self.assertEqual(T.local_model(), T.DEFAULT_LOCAL_MODEL)
        with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE_MODEL": "other"}):
            self.assertEqual(T.local_model(), "other")


class TestMessages(unittest.TestCase):
    def test_text_message_carries_the_target_language(self):
        messages = T.build_messages("hello")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("hello", messages[1]["content"])
        self.assertIn(T.target_language(), messages[1]["content"])

    def test_image_message_is_multimodal(self):
        messages = T.build_messages("", image_b64="QUJD")
        content = messages[1]["content"]
        self.assertIsInstance(content, list, "带图时 content 必须是数组（多模态格式）")
        kinds = [part["type"] for part in content]
        self.assertIn("image_url", kinds)
        url = [p for p in content if p["type"] == "image_url"][0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,QUJD"),
                        "图要以 data URL 形式内联（本地没有图床）")


class TestCompletionParsing(unittest.TestCase):
    def test_normal_payload(self):
        self.assertEqual(T.parse_completion(reply("你好")), (True, "你好"))

    def test_content_array_is_flattened(self):
        payload = {"choices": [{"message": {"content": [{"type": "text", "text": "a"},
                                                        {"type": "text", "text": "b"}]}}]}
        self.assertEqual(T.parse_completion(payload), (True, "ab"))

    def test_error_object(self):
        ok, why = T.parse_completion({"error": {"message": "bad key"}})
        self.assertFalse(ok)
        self.assertIn("bad key", why)

    def test_empty_choices(self):
        ok, why = T.parse_completion({"choices": []})
        self.assertFalse(ok)
        self.assertIn("choices", why)

    def test_non_json(self):
        ok, why = T.parse_completion("not json at all")
        self.assertFalse(ok)
        self.assertIn("JSON", why)


class TestBackendChoice(unittest.TestCase):
    def _resolve(self, mode, local, cloud):
        with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE": mode}):
            return T.resolve_backend(local_probe=lambda: local, cloud=cloud or
                                     {"base_url": "", "api_key": "", "config_file": "/tmp/none.json"})

    def test_auto_prefers_local(self):
        backend, why = self._resolve("auto", True, CLOUD)
        self.assertEqual(backend, T.BACKEND_LOCAL)
        self.assertIn(T.DEFAULT_LOCAL_MODEL, why)

    def test_auto_falls_back_to_cloud(self):
        backend, why = self._resolve("auto", False, CLOUD)
        self.assertEqual(backend, T.BACKEND_CLOUD)

    def test_auto_with_neither_gives_two_next_steps(self):
        backend, why = self._resolve("auto", False, None)
        self.assertIsNone(backend)
        self.assertIn("/models", why, "要说清怎么查本机模型起没起")
        self.assertIn("CAPSPLUS_CLOUD_KEY", why, "要说清云端凭据怎么给")

    def test_explicit_local_missing_is_refused(self):
        backend, why = self._resolve("local", False, CLOUD)
        self.assertIsNone(backend, "明确指定本机时不许偷偷上云（那要花他的钱）")
        self.assertIn("curl", why)

    def test_explicit_cloud_missing_is_refused(self):
        backend, why = self._resolve("cloud", True, None)
        self.assertIsNone(backend)
        self.assertIn("凭据", why)


class TestLocalTranslate(unittest.TestCase):
    def test_success(self):
        seen = {}

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            seen["url"] = url
            seen["body"] = json.loads(data.decode())
            seen["auth"] = headers.get("Authorization", "")
            return reply("你好世界")

        result = T.translate_local("Hello world", post=post)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "你好世界")
        self.assertEqual(result["src"], "Hello world")
        self.assertEqual(result["backend"], T.BACKEND_LOCAL)
        self.assertTrue(seen["url"].endswith("/chat/completions"))
        self.assertEqual(seen["body"]["model"], T.DEFAULT_LOCAL_MODEL)
        self.assertIn("Bearer", seen["auth"])
        self.assertIn("note", result)

    def test_network_error_is_actionable(self):
        def post(url, data, headers, timeout=T.TIMEOUT_S):
            raise OSError("connection refused")

        result = T.translate_local("hi", post=post)
        self.assertFalse(result["ok"])
        self.assertIn("本机模型", result["hint"])
        self.assertIn("/models", result["hint"], "要告诉他一键去看哪儿")

    def test_server_side_error_text(self):
        def post(url, data, headers, timeout=T.TIMEOUT_S):
            return json.dumps({"error": {"message": "out of memory"}}).encode()

        result = T.translate_local("hi", post=post)
        self.assertFalse(result["ok"])
        self.assertIn("out of memory", result["hint"])


class TestCloudTranslate(unittest.TestCase):
    def test_success_uses_the_cloud_model(self):
        seen = {}

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            seen["url"] = url
            seen["body"] = json.loads(data.decode())
            return reply("译文")

        result = T.translate_cloud("hello", cloud=CLOUD, post=post)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["body"]["model"], "deepseek-v4.1-flash")
        self.assertIn("云", result["note"])
        self.assertEqual(result["backend"], T.BACKEND_CLOUD)

    def test_missing_credentials_says_where(self):
        result = T.translate_cloud("hello", cloud={"base_url": "", "api_key": "",
                                                  "config_file": "/x/config.json"})
        self.assertFalse(result["ok"])
        self.assertIn("/x/config.json", result["hint"])

    def test_http_error_is_actionable(self):
        def post(url, data, headers, timeout=T.TIMEOUT_S):
            raise OSError("HTTP Error 401: Unauthorized")

        result = T.translate_cloud("hello", cloud=CLOUD, post=post)
        self.assertFalse(result["ok"])
        self.assertIn("401", result["hint"])
        self.assertIn("key", result["hint"])


class TestCloudReadsTheImageDirectly(unittest.TestCase):
    """云端多模态的卖点：**跳过 OCR 直读图**。判据直接抓请求体里有没有那张图。"""

    def test_image_is_sent_as_a_data_url(self):
        png = pathlib.Path("/tmp/capsplus-translate-test.png")
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 64)
        seen = {}

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            seen["body"] = json.loads(data.decode())
            return reply("原文：A\n译文：甲")

        result = T.translate_image(str(png), cloud=CLOUD, post=post)
        self.assertTrue(result["ok"])
        content = seen["body"]["messages"][1]["content"]
        self.assertIsInstance(content, list)
        image = [p for p in content if p["type"] == "image_url"][0]
        self.assertIn("data:image/png;base64,", image["image_url"]["url"])
        self.assertIn("跳过 OCR", result["note"])

    def test_cloud_without_key_refuses(self):
        result = T.translate_image("/tmp/whatever.png",
                                   cloud={"base_url": "", "api_key": "",
                                          "config_file": "/x/config.json"})
        self.assertFalse(result["ok"])
        self.assertIn("凭据", result["hint"])

    def test_unreadable_file_is_reported(self):
        result = T.translate_image("/tmp/definitely-missing.png", cloud=CLOUD,
                                   post=lambda *a, **k: reply())
        self.assertFalse(result["ok"])
        self.assertIn("读不了", result["hint"])


class TestLocalProbes(unittest.TestCase):
    def test_reachable_with_data(self):
        get = lambda url, timeout=5: json.dumps({"data": [{"id": "qwen3.8-27b"}]}).encode()  # noqa: E731
        self.assertTrue(T.local_reachable(get=get))

    def test_unreachable_when_empty_or_error(self):
        self.assertFalse(T.local_reachable(get=lambda url, timeout=5: b"{}"))
        def boom(url, timeout=5):
            raise OSError("refused")
        self.assertFalse(T.local_reachable(get=boom))

    def test_vision_flag_is_read_honestly(self):
        payload = {"data": [{"id": T.DEFAULT_LOCAL_MODEL, "modalities": {"vision": False}}]}
        get = lambda url, timeout=5: json.dumps(payload).encode()  # noqa: E731
        self.assertFalse(T.local_vision(get=get),
                         "本机模型实测只吃文本 —— 本地链路必须先 OCR，判据别把这条写反")

    def test_vision_true_when_the_model_says_so(self):
        payload = {"data": [{"id": T.DEFAULT_LOCAL_MODEL, "modalities": {"vision": True}}]}
        get = lambda url, timeout=5: json.dumps(payload).encode()  # noqa: E731
        self.assertTrue(T.local_vision(get=get))


class TestTranslateEntrypoint(unittest.TestCase):
    def test_empty_text_is_refused(self):
        result = T.translate("   ", T.BACKEND_LOCAL)
        self.assertFalse(result["ok"])
        self.assertIn("原文", result["hint"])

    def test_unknown_backend(self):
        result = T.translate("hi", "astral")
        self.assertFalse(result["ok"])
        self.assertIn("astral", result["hint"])

    def test_auto_passes_the_resolved_note(self):
        result = T.translate("hi", None, local_probe=lambda: True,
                             post=lambda *a, **k: reply("译"))
        self.assertTrue(result["ok"])
        self.assertIn(T.DEFAULT_LOCAL_MODEL, result["note"])


if __name__ == "__main__":
    unittest.main()