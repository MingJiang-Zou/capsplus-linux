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
import tempfile
import shutil
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
        # 三级来源都要喂**不存在的路径**，否则会读到真凭据（判据不许碰他真实的
        # `~/.dsh/.credentials.yaml` —— 用了注入路径才测得出"读不到就空"）。
        cfg = T.cloud_config("/tmp/definitely-missing.json",
                             credentials_path="/tmp/definitely-missing.yaml")
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


class TestDefaultBackendIsLocal(unittest.TestCase):
    """默认档必须是 `local`（2026-10-19 明江的口径："翻译最好用免费的"）。

    为什么这条要有判据：`auto` 的含义是"本机没起就静默切云"，而云那条是**按 token 计费**的
    LLM API（且那把 key 已 401）—— 默认档一旦回到 auto，用户就会在不知情的情况下花额度/报错。
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       (T.MODE_ENV, "CAPSPLUS_CLOUD_KEY", "CAPSPLUS_CLOUD_BASE_URL")}
        self.addCleanup(self._restore)
        for k in self._saved:
            os.environ.pop(k, None)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_mode_is_local(self):
        self.assertEqual(T.mode(), "local", "默认档不是 local 了 —— 又会被静默切到计费云")
        self.assertEqual(T.DEFAULT_MODE, "local")

    def test_explicit_auto_and_cloud_still_available(self):
        os.environ[T.MODE_ENV] = "auto"
        self.assertEqual(T.mode(), "auto")
        os.environ[T.MODE_ENV] = "cloud"
        self.assertEqual(T.mode(), "cloud")

    def test_broken_cloud_key_does_not_break_the_default(self):
        """默认档下：**云 key 失效（401 那种）也照样走本机且成功**。"""
        os.environ["CAPSPLUS_CLOUD_KEY"] = "fake-dead-key"
        os.environ["CAPSPLUS_CLOUD_BASE_URL"] = "https://example.invalid/v1"
        backend, why = T.resolve_backend(local_probe=lambda: True)
        self.assertEqual(backend, "local", f"默认档被云影响了：{why}")
        self.assertIn("本机", why)

    def test_no_cloud_credential_is_needed_by_default(self):
        """默认档**不读云凭据**也不该因为没凭据而失败。"""
        cloud = T.cloud_config(config_path="/nonexistent/x.json",
                               credentials_path="/nonexistent/creds.yaml")
        self.assertEqual(cloud["api_key"], "")
        backend, _why = T.resolve_backend(local_probe=lambda: True, cloud=cloud)
        self.assertEqual(backend, "local")

    def test_local_down_text_talks_about_starting_the_local_model(self):
        """本机没起时的文案：说"怎么起本机模型"，**不是**"自动切云"。"""
        _backend, why = T.resolve_backend(local_probe=lambda: False)
        self.assertIn("本机模型没起", why)
        self.assertIn("curl", why, "要给出判活命令")
        self.assertNotIn("自动切", why)
        self.assertNotIn("切到云", why.replace("想用云端就按面板上的『切到云』", ""),
                         "默认档不该把『切云』当解决办法推给用户")

    def test_cloud_mode_still_says_which_credential_source(self):
        os.environ[T.MODE_ENV] = "cloud"
        _backend, why = T.resolve_backend(
            local_probe=lambda: False,
            cloud={"base_url": "https://x.invalid/v1", "api_key": "", "model": "m",
                   "config_file": "/tmp/none.json", "source": ""})
        self.assertIn("云", why)


if __name__ == "__main__":
    unittest.main()


class TestCredentialSources(unittest.TestCase):
    """凭据来源**三级**：`CAPSPLUS_CLOUD_KEY` → `~/.bailian/config.json` → `~/.dsh/.credentials.yaml`。

    纪律（都进判据）：只读、**绝不打印**、绝不改写任何一个凭据文件；判据一律用**注入的 fixture 路径**
    （不碰明江真实的 `~/.dsh/.credentials.yaml`）；报错文案要可操作。
    """

    FAKE = "sk-FAKE-TEST-TOKEN-0123456789-DO-NOT-LEAK"

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-cred-"))
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.json_path = self.dir / "config.json"
        self.cred_path = self.dir / "credentials.yaml"
        self.json_path.write_text(json.dumps(
            {"active_config": "token-plan",
             "token-plan": {"base_url": "https://json.example.com", "api_key": "KEY-FROM-JSON"}}),
            encoding="utf-8")
        self.cred_path.write_text(
            "version: 1\nrefs:\n  AL_TOKEN_PLAN_API_KEY: %s\n  OTHER_REF: nope\n"
            % self.FAKE, encoding="utf-8")
        self._saved = os.environ.get("CAPSPLUS_CLOUD_KEY")
        self.addCleanup(self._restore)
        os.environ.pop("CAPSPLUS_CLOUD_KEY", None)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("CAPSPLUS_CLOUD_KEY", None)
        else:
            os.environ["CAPSPLUS_CLOUD_KEY"] = self._saved

    def test_env_beats_json_beats_yaml(self):
        os.environ["CAPSPLUS_CLOUD_KEY"] = "KEY-FROM-ENV"
        cfg = T.cloud_config(str(self.json_path), credentials_path=str(self.cred_path))
        self.assertEqual(cfg["api_key"], "KEY-FROM-ENV")
        self.assertIn("环境变量", cfg["source"])

        os.environ.pop("CAPSPLUS_CLOUD_KEY", None)
        cfg = T.cloud_config(str(self.json_path), credentials_path=str(self.cred_path))
        self.assertEqual(cfg["api_key"], "KEY-FROM-JSON")
        self.assertIn("bailian", cfg["source"])

    def test_falls_back_to_the_dsh_credentials_file(self):
        empty = self.dir / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        cfg = T.cloud_config(str(empty), credentials_path=str(self.cred_path))
        self.assertEqual(cfg["api_key"], self.FAKE)
        self.assertIn("credentials.yaml", cfg["source"])
        self.assertNotIn(self.FAKE, cfg["source"], "source 只能是标签，不能带值")

    def test_only_the_named_ref_is_read(self):
        self.assertEqual(T.read_dsh_credential(str(self.cred_path), ref="OTHER_REF"), "nope")
        self.assertEqual(T.read_dsh_credential(str(self.cred_path), ref="NOPE"), "")

    def test_missing_or_broken_file_is_empty(self):
        broken = self.dir / "broken.yaml"
        broken.write_text("这不是 yaml\n\t乱缩进:", encoding="utf-8")
        self.assertEqual(T.read_dsh_credential(str(broken)), "")
        self.assertEqual(T.read_dsh_credential(str(self.dir / "nope.yaml")), "")

    def test_credential_value_never_leaks_into_any_message(self):
        """**假凭据 fixture**：任何产出的文字里都不许出现它的值（含片段）。"""
        os.environ["CAPSPLUS_CLOUD_KEY"] = self.FAKE
        messages = []
        messages.append(T.describe())
        messages.append(T.cloud_config(str(self.json_path),
                                       credentials_path=str(self.cred_path))["source"])
        messages.append(str(T.resolve_backend(local_probe=lambda: False)))
        messages.append(str(T.resolve_backend(local_probe=lambda: True)))
        os.environ[T.MODE_ENV] = "cloud"
        self.addCleanup(lambda: os.environ.pop(T.MODE_ENV, None))
        messages.append(str(T.resolve_backend(local_probe=lambda: False)))
        # 真发一次云请求（端点故意不可达）——失败文案也必须不含凭据值
        leaked = T.translate_cloud("你好", cloud=T.cloud_config(
            str(self.json_path), credentials_path=str(self.cred_path)))
        messages.append(str(leaked))
        messages.append(str(T.translate_image(str(self.json_path), cloud=T.cloud_config(
            str(self.json_path), credentials_path=str(self.cred_path)))))
        blob = "\n".join(messages)
        self.assertNotIn(self.FAKE, blob, "凭据值漏到文案里了")
        for piece in (self.FAKE[:12], self.FAKE[-12:]):
            self.assertNotIn(piece, blob, f"凭据片段漏了：{piece}")
        self.assertNotIn("KEY-FROM-JSON", blob)

    def test_401_text_is_actionable(self):
        """401 要明确说"凭据失效 → 按 F2 切本地，或换一把 key"。"""
        cfg = {"base_url": "https://cloud.example.com/v1", "api_key": "k" * 20,
               "model": "m", "config_file": "/tmp/x.json", "source": "s"}
        ok, hint = T.cloud_hint_for_error(401, "Unauthorized", cfg)
        self.assertFalse(ok)
        self.assertIn("失效", hint)
        self.assertIn("本地", hint)
        self.assertIn("key", hint.lower())
        self.assertNotIn("k" * 20, hint)

    def test_cloud_config_is_read_only(self):
        """静态：凭据相关的代码**没有写操作**（只读是硬纪律）。"""
        src = (REPO / "capsplus" / "shot_translate.py").read_text(encoding="utf-8")
        for fn in ("def read_dsh_credential", "def cloud_config"):
            body = src[src.index(fn):]
            body = body[:body.index("\ndef ", 1)]
            for write_call in ("write_text", "open(", '"w"', "'w'", "mkdir", "unlink", "rename"):
                self.assertNotIn(write_call, body,
                                 f"{fn} 里出现了写操作 {write_call} —— 凭据文件只许读")


if __name__ == "__main__":
    unittest.main()
