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

import contextlib
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


def reasoning_reply(reasoning="想" * 40, finish="length"):
    """**真机上抓到的形状**（2026-10-20）：HTTP 200、`content` 空、思考全在 `reasoning_content`。

    原样搬进判据里（不是编的）：本机 llama.cpp `qwen3.8-27b` 的实际响应就是
    `choices[0].message.content = ""`、`reasoning_content` 有 134 个字、`finish_reason="length"`。
    """
    return json.dumps({"choices": [{"finish_reason": finish,
                                    "message": {"content": "", "reasoning_content": reasoning}}]}
                      ).encode("utf-8")


class _HttpError(Exception):
    """带状态码的假 HTTP 错误（urllib 的 `HTTPError.code` / requests 的 `.response.status_code`
    两种形状都覆盖到 —— 判据两种各用一次）。"""

    def __init__(self, code):
        super().__init__(f"HTTP Error {code}")
        self.code = code


class TestThinkingIsTurnedOff(unittest.TestCase):
    """① 关思考：**实测唯一有效的开关是 `chat_template_kwargs`**，判据直接抓请求体。

    2026-10-20 真机（用户报"翻译无效"）：本机 12087 上换成推理模型后，`content` **恒为空**、
    token 全花在 `reasoning_content` 里；`parse_completion` 读的是 `.content` ⇒ 翻译结果永远空白。
    实测数据：
      * 不关思考：`max_tokens=32` 全用光、`content=""`、`finish_reason=length`（3/3 复现）；
      * `chat_template_kwargs={"enable_thinking":false}`：`reasoning_content` 空、22 token 出完整译文、
        墙钟 0.43s（缓存命中）/0.78s（冷）——本机与阿里 token-plan **两个后端都认这个开关**；
      * 提示词里加 `/no_think` **不管用**（实测照样 32 token 全思考、content 仍空），所以本文件里
        不测它"有效"，只测"没把它当成办法"。
    """

    def test_local_request_carries_the_no_think_kwarg(self):
        seen = {}

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            seen["body"] = json.loads(data.decode())
            return reply("译文")

        result = T.translate_local("hello", post=post)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["body"].get("chat_template_kwargs"), {"enable_thinking": False},
                         "没发关思考开关 ⇒ 推理模型会把 token 全花在思考里、译文是空的")
        self.assertEqual(seen["body"].get("max_tokens"), T.TEXT_MAX_TOKENS,
                         "要给 max_tokens：不关思考时靠它兜住 runaway")

    def test_cloud_request_carries_it_too(self):
        seen = {}

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            seen["body"] = json.loads(data.decode())
            return reply("译文")

        T.translate_cloud("hello", cloud=CLOUD, post=post)
        self.assertEqual(seen["body"].get("chat_template_kwargs"), {"enable_thinking": False},
                         "云端 deepseek-v4.1-flash 也是推理模型（实测 reasoning_tokens 149→0）")

    def test_image_request_carries_it_too(self):
        seen = {}
        png = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-tr-")) / "x.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            seen["body"] = json.loads(data.decode())
            return reply("原文：a\n译文：b")

        T.translate_image(str(png), cloud=CLOUD, post=post)
        self.assertEqual(seen["body"].get("chat_template_kwargs"), {"enable_thinking": False})
        self.assertEqual(seen["body"].get("max_tokens"), T.IMAGE_MAX_TOKENS)

    def test_empty_content_becomes_an_explicit_reason_not_a_blank(self):
        """空 content 必须**带原因**（认出推理模型这种形状），不许静默空白。"""
        ans = T.translate_local("hello", post=lambda *a, **k: reasoning_reply())
        self.assertFalse(ans["ok"])
        self.assertIn("reasoning_content", ans["hint"], "要说清思考把 token 吃光了")
        self.assertIn("enable_thinking", ans["hint"], "要给可复制的修法")
        self.assertIn("/no_think", ans["hint"], "要顺带说清提示词那条路不管用（别让人白试）")
        self.assertEqual(ans["text"], "")

    def test_an_endpoint_that_rejects_the_kwarg_still_translates(self):
        """对面不认 `chat_template_kwargs`（400）⇒ 去掉它重试一次，而不是整个翻译废掉。"""
        calls = []

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            body = json.loads(data.decode())
            calls.append(bool(body.get("chat_template_kwargs")))
            if body.get("chat_template_kwargs"):
                raise _HttpError(400)
            return reply("译文")

        result = T.translate_local("hello", post=post)
        self.assertTrue(result["ok"], "400 应该退化成『去掉开关再试一次』")
        self.assertEqual(calls, [True, False], f"应当先带开关、被拒后不带：{calls}")
        self.assertIn("不认", result["note"], "退化了要说出来（别让人以为一直是关思考在跑）")

    def test_a_500_is_not_retried(self):
        """只有 400（"不认这个字段"）才重试；500 这种服务端错误照原样报。"""
        calls = []

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            calls.append(1)
            raise _HttpError(500)

        result = T.translate_local("hello", post=post)
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 1, "500 不该被重试（重试只会多花一次排队时间）")
        self.assertIn("500", result["hint"])


class TestTimeoutsAreMeasured(unittest.TestCase):
    """① 超时：实测定的数字 + **超时与"端点不在"必须分开说**（排队不等于端点死了）。"""

    def test_default_timeout_is_roomier_than_the_measured_queueing(self):
        # 实测：服务端自己算一次翻译 0.39s，可被另一个会话排到 **63.7s** 墙钟 ⇒ 60s 会误报
        self.assertGreaterEqual(T.TIMEOUT_S, 120, "默认超时要比实测排队的 63.7s 留足余量")
        self.assertEqual(T.request_timeout(), T.TIMEOUT_S)

    def test_env_can_raise_it(self):
        os.environ[T.TIMEOUT_ENV] = "7"
        self.addCleanup(lambda: os.environ.pop(T.TIMEOUT_ENV, None))
        self.assertEqual(T.request_timeout(), 7)
        os.environ[T.TIMEOUT_ENV] = "乱写"
        self.assertEqual(T.request_timeout(), T.TIMEOUT_S, "写坏了要退回默认，不是变成 0")
        os.environ[T.TIMEOUT_ENV] = "0"
        self.assertEqual(T.request_timeout(), T.TIMEOUT_S)

    def test_timeout_says_queueing_not_dead(self):
        def post(url, data, headers, timeout=T.TIMEOUT_S):
            self.assertEqual(timeout, T.request_timeout(), "传下去的必须是生效的那个超时")
            raise TimeoutError("timed out")

        result = T.translate_local("hello", post=post)
        self.assertFalse(result["ok"])
        self.assertIn("超时", result["hint"])
        self.assertIn("排队", result["hint"], "要说清本机是串行的、可能只是被别人占着")
        self.assertNotIn("没应答", result["hint"], "别把排队说成端点死了")


class TestTranslateStatusProbe(unittest.TestCase):
    """① `--translate-status` 要**做一次真往返**并按结果分类（原来只说"端点活着"）。"""

    def _probe(self, payload_or_exc):
        if isinstance(payload_or_exc, Exception):
            def post(*_a, **_k):
                raise payload_or_exc
        else:
            def post(*_a, **_k):
                return payload_or_exc
        return post

    def test_alive_and_producing_text(self):
        out = T.probe_local(post=self._probe(reply("你好")))
        self.assertEqual(out["state"], T.PROBE_STATE_OK)
        self.assertIn("你好", out["text"])

    def test_alive_but_content_empty_is_a_flagged_state(self):
        """这正是真机上的形状：HTTP 200、能连上、**出不了字** —— 不许报"活着"。"""
        out = T.probe_local(post=self._probe(reasoning_reply()))
        self.assertEqual(out["state"], T.PROBE_STATE_EMPTY)
        self.assertIn("reasoning_content", out["detail"])

    def test_timeout_state(self):
        out = T.probe_local(post=self._probe(TimeoutError("slow")))
        self.assertEqual(out["state"], T.PROBE_STATE_TIMEOUT)
        self.assertLessEqual(T.PROBE_TIMEOUT_S, 10, "探活要秒回，不许拖着")

    def test_down_state(self):
        out = T.probe_local(post=self._probe(OSError("connection refused")))
        self.assertEqual(out["state"], T.PROBE_STATE_DOWN)

    def test_status_text_reports_all_four(self):
        ok = T.describe(post=self._probe(reply("你好")))
        self.assertIn("活着且能出字", ok)
        empty = T.describe(post=self._probe(reasoning_reply()))
        self.assertIn("活着但出不了字", empty)
        self.assertIn("超时", T.describe(post=self._probe(TimeoutError("x"))))
        self.assertIn("端点不在", T.describe(post=self._probe(OSError("refused"))))
        # 关掉探活那条路只报端点通不通（离线/判据用）
        self.assertIn("端点", T.describe(probe=False))


class TestCloudCredentialFailover(unittest.TestCase):
    """① 云端兜底要**真的能用**：第一把 401（`~/.bailian/config.json` 实测就是坏的）要自动换下一把。"""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-tr-failover-"))
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.json_path = self.dir / "config.json"
        self.cred_path = self.dir / "credentials.yaml"
        self.json_path.write_text(json.dumps(
            {"active_config": "token-plan",
             "token-plan": {"base_url": "https://json.example.com", "api_key": "KEY-FROM-JSON"}}),
            encoding="utf-8")
        self.cred_path.write_text("version: 1\nrefs:\n  AL_TOKEN_PLAN_API_KEY: KEY-FROM-YAML\n",
                                  encoding="utf-8")
        self.addCleanup(lambda: os.environ.pop("CAPSPLUS_CLOUD_KEY", None))
        os.environ.pop("CAPSPLUS_CLOUD_KEY", None)
        self.json_cfg = T.cloud_config(str(self.json_path), credentials_path=str(self.cred_path))

    def test_candidates_are_listed_in_priority_order_without_values(self):
        cands = T.cloud_candidates(str(self.json_path), credentials_path=str(self.cred_path))
        self.assertEqual([c["api_key"] for c in cands], ["KEY-FROM-JSON", "KEY-FROM-YAML"])
        self.assertIn("bailian", cands[0]["source"])
        self.assertIn("credentials.yaml", cands[1]["source"])
        for cand in cands:
            self.assertNotIn("KEY-FROM", cand["source"], "source 只能是标签，不能带值")

    def test_a_401_from_the_first_credential_falls_through_to_the_next(self):
        used = []

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            key = headers.get("Authorization", "").replace("Bearer ", "")
            used.append(key)
            if key == "KEY-FROM-JSON":
                raise _HttpError(401)                    # 实测就是这把 401
            return reply("译文")

        orig = T.cloud_candidates
        with mock.patch.object(T, "cloud_candidates",
                               lambda *a, **k: orig(str(self.json_path),
                                                    credentials_path=str(self.cred_path))):
            result = T.translate_cloud("hello", post=post)
        self.assertTrue(result["ok"], "第二把是好的，就该成功 —— 别把 401 当『有凭据』")
        self.assertEqual(used, ["KEY-FROM-JSON", "KEY-FROM-YAML"])
        self.assertIn("401", result["note"], "换过凭据要说出来（不然没人知道第一把坏了）")

    def test_all_credentials_401_reports_every_source_it_tried(self):
        def post(*_a, **_k):
            raise _HttpError(401)

        orig = T.cloud_candidates
        with mock.patch.object(T, "cloud_candidates",
                               lambda *a, **k: orig(str(self.json_path),
                                                    credentials_path=str(self.cred_path))):
            result = T.translate_cloud("hello", post=post)
        self.assertFalse(result["ok"])
        self.assertIn("bailian", result["hint"])
        self.assertIn("credentials.yaml", result["hint"])
        self.assertIn("本地", result["hint"], "全坏要给下一步：切本机模型")
        self.assertNotIn("KEY-FROM", result["hint"], "文案里不许出现凭据值")

    def test_explicit_cloud_still_means_one_attempt(self):
        """显式传 cloud（判据/调用方指定）时不许偷偷换成别的凭据。"""
        calls = []

        def post(*_a, **_k):
            calls.append(1)
            raise _HttpError(401)

        result = T.translate_cloud("hello", cloud=CLOUD, post=post)
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 1)


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
    """默认档必须是 `local`（2026-10-19 用户的口径："翻译最好用免费的"）。

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
    （不碰用户真实的 `~/.dsh/.credentials.yaml`）；报错文案要可操作。
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
        # `probe=False`：这条判据查的是"值有没有漏出去"，不该真去连网络/真模型
        #（`--translate-status` 默认会真跑一次小往返，那条另有判据 `TestTranslateStatusProbe`）。
        messages.append(T.describe(probe=False))
        # 真走一遍"带探活"的分支（`--translate-status` 的默认形状），但**用桩把网络封住**
        messages.append(T.describe(post=lambda *a, **k: json.dumps(
            {"choices": [{"finish_reason": "length",
                          "message": {"content": None, "reasoning_content": "想" * 40}}]}).encode()))
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


# ══════════════════════════════════════════════════════════════════════════
# 2026-10-21：服务配置可配（`services.toml`）—— 端点从配置来，优先级 环境变量 > 配置 > 默认
# ══════════════════════════════════════════════════════════════════════════
# 为什么老判据一条没改：这一轮**没有改任何既有行为** ——
#   · 没有配置文件时，端点/档位/凭据候选顺序与 2026-10-21 之前**逐字一致**（见下面的
#     `TestWithoutAConfigFileNothingChanged`）；
#   · 有配置文件时，老的环境变量（`CAPSPLUS_TRANSLATE` / `CAPSPLUS_TRANSLATE_URL` /
#     `CAPSPLUS_TRANSLATE_MODEL` / `CAPSPLUS_CLOUD_*`）**仍然认，而且优先级更高**。
# 所以老判据（含 `TestBackendChoice` / `TestDefaultBackendIsLocal` / `TestCloudCredentialFailover`）
# 必须**一条都不红** —— 它们红了就说明"加配置层"把老用法弄坏了。

CONFIG_ALL = """
[translate]
mode = "auto"

[translate.local]
kind = "local"
style = "openai"
base_url = "http://127.0.0.1:12087/v1"
model = "qwen3.8-27b"

[translate.cloud]
kind = "remote"
style = "openai"
base_url = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
model = "deepseek-v4.1-flash"

[translate.nas]
kind = "remote"
style = "openai"
base_url = "http://192.0.2.10:8080/v1"
model = "qwen3-32b"
api_key_env = "NAS_TRANSLATE_KEY"
"""

CONFIG_NAS_FIRST = """
[translate]
mode = "auto"

[translate.nas]
kind = "remote"
style = "openai"
base_url = "http://192.0.2.10:8080/v1"
model = "qwen3-32b"
api_key_env = "NAS_TRANSLATE_KEY"

[translate.local]
kind = "local"
style = "openai"
base_url = "http://127.0.0.1:12087/v1"
model = "qwen3.8-27b"
"""

CONFIG_LOCAL_ONLY = """
[translate.local]
kind = "local"
style = "openai"
base_url = "http://192.0.2.10:8080/v1"
model = "qwen3-32b"
"""


@contextlib.contextmanager
def services_file(text: str):
    """把服务配置指到一段临时 TOML。

    为什么必须注入路径：判据**不许碰用户真配的那份** `~/.config/capsplus/services.toml`
    —— 一碰，它就会随用户的设置漂移（今天绿、明天红），而且判据还会读到真凭据。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "services.toml"
        path.write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, {"CAPSPLUS_SERVICES_FILE": str(path)}):
            yield path


def setUpModule():
    """**没配置**的状态是这一批老判据的前提 —— 先把真配置挡在外面。"""
    os.environ["CAPSPLUS_SERVICES_FILE"] = "/tmp/capsplus-no-such-services.toml"


def tearDownModule():
    os.environ.pop("CAPSPLUS_SERVICES_FILE", None)


class TestEndpointsComeFromTheConfig(unittest.TestCase):
    """① 端点从 `services.toml` 来（可配远程），且**默认行为不变**。"""

    def test_the_config_lists_the_endpoints_in_written_order(self):
        with services_file(CONFIG_ALL):
            eps = T.translate_endpoints()
        self.assertEqual([e.name for e in eps], ["local", "cloud", "nas"],
                         "端点清单 = 配置里写的那几个，顺序就是书写顺序")
        self.assertEqual(eps[2].base_url, "http://192.0.2.10:8080/v1")
        self.assertEqual(eps[2].kind, "remote")

    def test_local_url_and_model_follow_the_config(self):
        with services_file(CONFIG_LOCAL_ONLY):
            self.assertEqual(T.local_url(), "http://192.0.2.10:8080/v1")
            self.assertEqual(T.local_model(), "qwen3-32b")

    def test_a_named_endpoint_can_be_selected_by_name(self):
        """自建远程端点：用**端点名**就能选它（`CAPSPLUS_TRANSLATE=nas`）。"""
        with services_file(CONFIG_ALL):
            with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE": "nas",
                                              "NAS_TRANSLATE_KEY": "k"}):
                self.assertEqual(T.mode(), "nas")
                backend, why = T.resolve_backend(local_probe=lambda: True)
        self.assertEqual(backend, "nas")
        self.assertIn("nas", why)

    def test_an_unknown_endpoint_name_is_refused_with_the_usable_ones(self):
        with services_file(CONFIG_ALL):
            with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE": "nope"}):
                backend, why = T.resolve_backend(local_probe=lambda: True)
        self.assertIsNone(backend, "配置里没有的端点不许瞎猜")
        self.assertIn("nas", why, "要说清现在能用哪些")

    def test_the_configured_endpoint_is_actually_called(self):
        """端点不只是"列出来"—— 请求必须真的发到配置里那个地址。"""
        seen = {}

        def post(url, data, headers, timeout=T.TIMEOUT_S):
            seen["url"] = url
            seen["auth"] = headers.get("Authorization", "")
            return reply("译文")

        with services_file(CONFIG_ALL):
            with mock.patch.dict(os.environ, {"NAS_TRANSLATE_KEY": "secret-key"}):
                ep = T.endpoint_by_name("nas")
                result = T.translate_endpoint("hello", ep, post=post)
        self.assertTrue(result["ok"], result.get("hint"))
        self.assertTrue(seen["url"].startswith("http://192.0.2.10:8080/v1/"),
                        f"请求没发到配置的地址：{seen['url']}")
        self.assertIn("secret-key", seen["auth"], "凭据要按配置给的来源用")


class TestAutoFollowsTheWrittenOrder(unittest.TestCase):
    """② `auto` = 按**端点书写顺序**挨个试（配置里 local 在前就 local 优先）。"""

    def test_the_first_written_endpoint_wins_even_if_the_later_one_works(self):
        """配置里 nas 写在 local 前面 ⇒ auto 先用 nas（哪怕本机探得到）。"""
        with services_file(CONFIG_NAS_FIRST):
            with mock.patch.dict(os.environ, {"NAS_TRANSLATE_KEY": "k"}):
                backend, _why = T.resolve_backend(local_probe=lambda: True)
        self.assertEqual(backend, "nas", "auto 不认书写顺序 = 用户写的顺序就没意义了")

    def test_local_first_means_local_first(self):
        with services_file(CONFIG_ALL):
            backend, _why = T.resolve_backend(local_probe=lambda: True)
        self.assertEqual(backend, "local")

    def test_an_unusable_first_endpoint_falls_through_to_the_next(self):
        """第一个端点探不到 ⇒ 接着试下一个（这才是 auto 的意义）。"""
        with services_file(CONFIG_ALL):
            with mock.patch.dict(os.environ, {"NAS_TRANSLATE_KEY": "k"}):
                backend, _why = T.resolve_backend(
                    local_probe=lambda: False,
                    cloud={"base_url": "https://x.invalid/v1", "api_key": "k",
                           "model": "m", "config_file": "/tmp/none.json"})
        self.assertEqual(backend, "cloud")


class TestEnvironmentBeatsTheConfigFile(unittest.TestCase):
    """③ **环境变量 > 配置文件 > 内置默认**（仓库口径，三条链路统一）。"""

    def test_the_url_env_var_beats_the_config(self):
        with services_file(CONFIG_LOCAL_ONLY):
            with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE_URL":
                                               "http://127.0.0.1:19999/v1"}):
                self.assertEqual(T.local_url(), "http://127.0.0.1:19999/v1")

    def test_the_model_env_var_beats_the_config(self):
        with services_file(CONFIG_LOCAL_ONLY):
            with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE_MODEL": "other-model"}):
                self.assertEqual(T.local_model(), "other-model")

    def test_the_mode_env_var_beats_the_config(self):
        with services_file(CONFIG_ALL):
            with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE": "cloud"}):
                self.assertEqual(T.mode(), "cloud")

    def test_the_cloud_env_vars_still_win_over_the_config(self):
        """云端那几个环境变量（现场救火用）不许因为加了配置层就失效。"""
        with services_file(CONFIG_ALL):
            with mock.patch.dict(os.environ, {"CAPSPLUS_CLOUD_BASE_URL":
                                               "https://env.example/v1",
                                               "CAPSPLUS_CLOUD_MODEL": "env-model",
                                               "CAPSPLUS_CLOUD_KEY": "env-key"}):
                cfg = T.cloud_config("/tmp/definitely-missing.json",
                                     credentials_path="/tmp/definitely-missing.yaml")
        self.assertEqual(cfg["base_url"], "https://env.example/v1")
        self.assertEqual(cfg["model"], "env-model")
        self.assertEqual(cfg["api_key"], "env-key")


class TestConfiguredCredentialsKeepTheOldOrderAsFallback(unittest.TestCase):
    """④ 凭据：配置里显式写的排**第一**，老的三级顺序（env → bailian → dsh）留作兜底。"""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-svc-cred-"))
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.json_path = self.dir / "config.json"
        self.cred_path = self.dir / "credentials.yaml"
        self.json_path.write_text(json.dumps(
            {"active_config": "token-plan",
             "token-plan": {"base_url": "https://json.example.com", "api_key": "KEY-FROM-JSON"}}),
            encoding="utf-8")
        self.cred_path.write_text("version: 1\nrefs:\n  AL_TOKEN_PLAN_API_KEY: KEY-FROM-YAML\n",
                                  encoding="utf-8")
        self.addCleanup(lambda: os.environ.pop("CAPSPLUS_CLOUD_KEY", None))
        os.environ.pop("CAPSPLUS_CLOUD_KEY", None)

    def test_the_configured_source_comes_first_and_the_old_order_stays_behind(self):
        config = """
[translate.cloud]
credentials_file = "%s"
credentials_key = "AL_TOKEN_PLAN_API_KEY"
""" % self.cred_path
        with services_file(config):
            cands = T.cloud_candidates(str(self.json_path),
                                        credentials_path=str(self.cred_path))
        keys = [c["api_key"] for c in cands]
        self.assertEqual(keys[0], "KEY-FROM-YAML", "配置里写的来源要排第一")
        self.assertIn("KEY-FROM-JSON", keys, "老的三级顺序要留在后面当兜底")
        self.assertIn("KEY-FROM-YAML", keys)

    def test_without_a_config_the_old_order_is_untouched(self):
        """**没有配置文件时，老的顺序一字不变**（这是"不许把老用法弄坏"的底线）。"""
        cands = T.cloud_candidates(str(self.json_path), credentials_path=str(self.cred_path))
        self.assertEqual([c["api_key"] for c in cands], ["KEY-FROM-JSON", "KEY-FROM-YAML"])
        self.assertIn("bailian", cands[0]["source"])
        self.assertIn("credentials.yaml", cands[1]["source"])

    def test_the_configured_base_url_is_used_and_not_normalised(self):
        """配置里那个地址是**用户自己写的** —— 不许被"补兼容路径"改掉（自建端点会打错地方）。"""
        config = """
[translate.cloud]
base_url = "http://192.0.2.10:8080/v1"
"""
        with services_file(config):
            cfg = T.cloud_config(str(self.json_path),
                                 credentials_path=str(self.cred_path))
        self.assertEqual(cfg["base_url"], "http://192.0.2.10:8080/v1")


class TestWithoutAConfigFileNothingChanged(unittest.TestCase):
    """⑤ **没配置时行为逐字不变**（模块头 ②：内置默认 = 与 2026-10-21 之前一致）。"""

    def test_defaults_are_the_old_hardcoded_ones(self):
        self.assertEqual(T.local_url(), T.DEFAULT_LOCAL_URL)
        self.assertEqual(T.local_model(), T.DEFAULT_LOCAL_MODEL)
        self.assertEqual(T.mode(), "local")

    def test_the_default_endpoints_are_the_old_ones(self):
        eps = {e.name: e for e in T.translate_endpoints()}
        self.assertEqual(eps["local"].base_url, "http://127.0.0.1:12087/v1")
        self.assertEqual(eps["local"].model, "qwen3.8-27b")
        self.assertEqual(eps["cloud"].base_url,
                         "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")

    def test_a_config_without_a_translate_section_keeps_the_local_default(self):
        """**关键一条**：配置文件里没有 `[translate]` 时，默认档**不许**变成 `auto`。

        为什么：`services.default_services()` 给的默认是 `auto`，而本模块的默认是 `local`
        （2026-10-19 用户定的口径："翻译最好用免费的"，默认档绝不许静默切到计费云）。
        库分不清"用户写了 auto"和"没写"，所以这里按**用户显式写了没有**来判。
        """
        with services_file("[asr]\nlang = \"zh\"\n"):
            self.assertEqual(T.mode(), "local",
                             "默认档被配置文件带成 auto 了 ⇒ 会静默切到计费云")


class TestCounterExamples(unittest.TestCase):
    """反例具型：每条判据都得能红。**判据抓不住东西 = 没有判据。**"""

    def test_counterexample_config_beating_the_env_var_is_caught(self):
        """反例 ①：把优先级反过来（配置压过环境变量）⇒ 判据必须红。"""
        with services_file(CONFIG_ALL):
            with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE": "cloud"}):
                good = T.mode()
                with mock.patch.object(T, "_pick", lambda e, c, d: c or e or d):
                    bad = T.mode()
        self.assertEqual(good, "cloud", "判据本体：环境变量必须压过配置文件")
        self.assertNotEqual(bad, "cloud",
                            "优先级写反了这条判据抓不住 —— 它就是摆设")

    def test_counterexample_auto_ignoring_the_order_is_caught(self):
        """反例 ②：让 `auto` 忽略端点顺序（老规矩"local 优先"）⇒ 判据必须红。"""
        with services_file(CONFIG_NAS_FIRST):
            with mock.patch.dict(os.environ, {"NAS_TRANSLATE_KEY": "k"}):
                good, _why = T.resolve_backend(local_probe=lambda: True)
                original = T.translate_endpoints
                with mock.patch.object(T, "translate_endpoints",
                                       lambda cfg=None: list(reversed(original(cfg)))):
                    bad, _why2 = T.resolve_backend(local_probe=lambda: True)
        self.assertEqual(good, "nas", "判据本体：auto 按端点书写顺序")
        self.assertNotEqual(bad, "nas", "auto 忽略顺序这条判据抓不住 —— 它就是摆设")

    def test_counterexample_the_old_env_var_dying_is_caught(self):
        """反例 ③：让老的环境变量失效（`CAPSPLUS_TRANSLATE` 不再被认）⇒ 判据必须红。"""
        with services_file(CONFIG_ALL):
            with mock.patch.dict(os.environ, {"CAPSPLUS_TRANSLATE": "cloud"}):
                good = T.mode()
                with mock.patch.object(T, "_known_mode",
                                       lambda value, cfg=None: value.lower() == "auto"):
                    bad = T.mode()
        self.assertEqual(good, "cloud", "判据本体：老的环境变量必须仍然生效")
        self.assertNotEqual(bad, "cloud",
                            "老环境变量失效这条判据抓不住 —— 它就是摆设")


if __name__ == "__main__":
    unittest.main()
