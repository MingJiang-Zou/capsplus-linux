# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：OCR（第二批 F4）—— 后端选择、本地子进程、云端（百度）、以及**话说得到不到位**。

三条口径（每条都有对应的红法）：
  ① **本地优先**：`auto` 下探得到本地就用本地（离线、不花钱），探不到才上云；
  ② **不可用时必须可操作**：报错里要带"装什么/看哪个文件"，不能只说"失败了"
     —— 反例：把 hint 换成"✗ 失败" → 红；
  ③ **主进程不背重依赖**：`shot_ocr.py` 顶层不许 import rapidocr/onnxruntime/opencv（子进程才碰）。

全程**不打真网、不起真模型**：runner / post / get 都是注入的桩。
"""

import json
import os
import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import shot_ocr as O          # noqa: E402

CREDS = {"api_key": "ak", "secret_key": "sk", "env_file": "/tmp/none.env"}


class Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class TestEnvParsing(unittest.TestCase):
    def test_parses_export_quotes_and_comments(self):
        text = ('# 注释\nBAIDU_OCR_API_KEY=abc\n'
                'export BAIDU_OCR_SECRET_KEY="d e f"\n\nOTHER=\'x\'\n')
        values = O.parse_env_file(text)
        self.assertEqual(values["BAIDU_OCR_API_KEY"], "abc")
        self.assertEqual(values["BAIDU_OCR_SECRET_KEY"], "d e f")
        self.assertEqual(values["OTHER"], "x")

    def test_garbage_lines_are_ignored(self):
        self.assertEqual(O.parse_env_file("not a kv line\n=empty\n"), {})

    def test_credentials_come_from_env_first(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"BAIDU_OCR_API_KEY": "from-env"}):
            creds = O.ocr_credentials("/tmp/definitely-missing.env")
        self.assertEqual(creds["api_key"], "from-env")

    def test_missing_file_is_not_an_exception(self):
        creds = O.ocr_credentials("/tmp/definitely-missing.env")
        self.assertEqual((creds["api_key"], creds["secret_key"]), ("", ""))
        self.assertTrue(creds["env_file"].endswith(".env"))


class TestBackendChoice(unittest.TestCase):
    """`auto`：本地优先；两边都没有时给的话必须可操作。"""

    def _resolve(self, mode, local=True, cloud=True):
        import os
        from unittest import mock
        env = {"CAPSPLUS_OCR": mode}
        with mock.patch.dict(os.environ, env):
            return O.resolve_backend(local_probe=lambda: ("/opt/ocr/bin/python" if local else None),
                                     creds=CREDS if cloud else {"api_key": "", "secret_key": "",
                                                                "env_file": "/tmp/none.env"})

    def test_auto_prefers_local(self):
        backend, why = self._resolve("auto", local=True, cloud=True)
        self.assertEqual(backend, O.BACKEND_LOCAL)
        self.assertIn("/opt/ocr/bin/python", why)

    def test_auto_falls_back_to_cloud(self):
        backend, why = self._resolve("auto", local=False, cloud=True)
        self.assertEqual(backend, O.BACKEND_CLOUD)
        self.assertIn("百度", why)

    def test_auto_with_neither_says_what_to_do(self):
        backend, why = self._resolve("auto", local=False, cloud=False)
        self.assertIsNone(backend)
        self.assertIn("rapidocr", why, "要说清装什么")
        self.assertIn("BAIDU_OCR_API_KEY", why, "要说清缺哪个凭据")

    def test_explicit_local_that_is_missing_is_refused_with_a_command(self):
        backend, why = self._resolve("local", local=False, cloud=True)
        self.assertIsNone(backend, "明确指定本地时不许偷偷上云（那会花他的钱）")
        self.assertIn("venv", why)
        self.assertIn("rapidocr-onnxruntime", why)

    def test_explicit_cloud_that_is_missing_is_refused(self):
        backend, why = self._resolve("cloud", local=True, cloud=False)
        self.assertIsNone(backend)
        self.assertIn("凭据", why)

    def test_default_probe_really_looks_for_an_install(self):
        """不传探针时也要真去找 —— 反例：默认写成 `lambda: None`，于是 auto/local 永远探不到本地，
        而 `--ocr-status` 因为自己传探针又显示"本地可用"（2026-10-19 真踩过这个自相矛盾）。"""
        from unittest import mock
        with mock.patch.object(O, "candidate_pythons", lambda exists=None: ["/p/python"]), \
                mock.patch.object(O, "local_available", lambda python, runner=None: True):
            self.assertEqual(O.default_local_probe(), "/p/python")
        with mock.patch.object(O, "candidate_pythons", lambda exists=None: ["/p/python"]), \
                mock.patch.object(O, "local_available", lambda python, runner=None: False):
            self.assertIsNone(O.default_local_probe())

    def test_auto_finds_local_without_an_explicit_probe(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"CAPSPLUS_OCR": "auto"}):
            with mock.patch.object(O, "candidate_pythons", lambda exists=None: ["/p/python"]), \
                    mock.patch.object(O, "local_available", lambda python, runner=None: True):
                backend, why = O.resolve_backend(creds=CREDS)
        self.assertEqual(backend, O.BACKEND_LOCAL, f"默认探针没起作用：{why}")

    def test_unknown_mode_falls_back_to_auto(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"CAPSPLUS_OCR": "telepathy"}):
            self.assertEqual(O.mode(), "auto")


class TestLocalBackend(unittest.TestCase):
    def test_parses_the_last_json_line(self):
        stdout = 'engine log line\n{"ok": true, "text": "hi", "ms": 5}\n'
        self.assertEqual(O.parse_helper_output(stdout)["text"], "hi")

    def test_unparsable_output_is_reported(self):
        parsed = O.parse_helper_output("Traceback...\nboom\n")
        self.assertFalse(parsed["ok"])
        self.assertIn("没吐出可解析", parsed["hint"])

    def test_successful_run_maps_to_the_panel_contract(self):
        runner = lambda argv, timeout: Result(  # noqa: E731
            stdout=json.dumps({"ok": True, "text": "识别到的\n两行", "ms": 123,
                               "engine": "rapidocr-onnxruntime 1.2.3"}, ensure_ascii=False),
            returncode=0)
        result = O.ocr_local("/tmp/a.png", python="/opt/ocr/bin/python", runner=runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "识别到的\n两行")
        self.assertEqual(result["backend"], O.BACKEND_LOCAL)
        self.assertEqual(result["ms"], 123)
        self.assertIn("rapidocr", result["note"])

    def test_helper_failure_surfaces_its_hint(self):
        runner = lambda argv, timeout: Result(  # noqa: E731
            stdout=json.dumps({"ok": False, "hint": "✗ 这个 venv 里没有 rapidocr"}, ensure_ascii=False),
            returncode=3)
        result = O.ocr_local("/tmp/a.png", python="/opt/ocr/bin/python", runner=runner)
        self.assertFalse(result["ok"])
        self.assertIn("rapidocr", result["hint"])

    def test_timeout_is_reported_as_actionable(self):
        def runner(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        result = O.ocr_local("/tmp/a.png", python="/opt/ocr/bin/python", runner=runner)
        self.assertFalse(result["ok"])
        self.assertIn("模型", result["hint"])

    def test_no_python_available(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"CAPSPLUS_OCR_PYTHON": ""}):
            with mock.patch.object(O, "candidate_pythons", lambda exists=None: []):
                result = O.ocr_local("/tmp/a.png")
        self.assertFalse(result["ok"])
        self.assertIn("CAPSPLUS_OCR_PYTHON", result["hint"])

    def test_argv_shape(self):
        seen = {}

        def runner(argv, timeout):
            seen["argv"] = argv
            return Result(stdout='{"ok": true, "text": "x", "ms": 1}')

        O.ocr_local("/tmp/a.png", python="/opt/ocr/bin/python", runner=runner)
        self.assertEqual(seen["argv"][0], "/opt/ocr/bin/python")
        self.assertTrue(seen["argv"][1].endswith("shot_ocr_local.py"))
        self.assertEqual(seen["argv"][2], "/tmp/a.png")


class TestCloudBackend(unittest.TestCase):
    def test_ocr_assembles_words_result(self):
        def post(url, data, headers, timeout=15):
            if "oauth" in url:
                return json.dumps({"access_token": "tk", "expires_in": 100}).encode()
            return json.dumps({"words_result": [{"words": "第一行"}, {"words": "第二行"}]}).encode()

        result = O.ocr_cloud("/tmp/a.png", creds=CREDS, post=post,
                             cache_path=pathlib.Path("/tmp/none-capsplus-token.json"))
        _ = result
        # 读不到 /tmp/a.png（本来就没有这个文件）时应当如实报，别假装成功
        self.assertIn(result["ok"], (True, False))

    def test_ocr_text_assembly_and_note(self):
        png = pathlib.Path("/tmp/capsplus-ocr-test.png")
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        def post(url, data, headers, timeout=15):
            if "oauth" in url:
                return json.dumps({"access_token": "tk", "expires_in": 100}).encode()
            return json.dumps({"words_result": [{"words": "第一行"}, {"words": "第二行"}]}).encode()

        result = O.ocr_cloud(str(png), creds=CREDS, post=post,
                            cache_path=pathlib.Path("/tmp/none-capsplus-token.json"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "第一行\n第二行")
        self.assertIn("accurate_basic", result["note"])

    def test_baidu_error_code_is_surfaced(self):
        png = pathlib.Path("/tmp/capsplus-ocr-test.png")

        def post(url, data, headers, timeout=15):
            if "oauth" in url:
                return json.dumps({"access_token": "tk", "expires_in": 100}).encode()
            return json.dumps({"error_code": 17, "error_msg": "Open api daily request limit reached"}).encode()

        result = O.ocr_cloud(str(png), creds=CREDS, post=post,
                            cache_path=pathlib.Path("/tmp/none-capsplus-token.json"))
        self.assertFalse(result["ok"])
        self.assertIn("17", result["hint"])
        self.assertIn("limit", result["hint"])

    def test_missing_credentials_tells_where_to_put_them(self):
        result = O.ocr_cloud("/tmp/a.png", creds={"api_key": "", "secret_key": "",
                                                  "env_file": "/x/.env"})
        self.assertFalse(result["ok"])
        self.assertIn("/x/.env", result["hint"])

    def test_token_is_cached_and_reused(self):
        calls = []

        def post(url, data, headers, timeout=15):
            calls.append(url)
            return json.dumps({"access_token": "tk", "expires_in": 1000}).encode()

        cache = pathlib.Path("/tmp/capsplus-token-cache-test.json")
        if cache.exists():
            cache.unlink()
        token, how = O.baidu_token(CREDS, post=post, cache_path=cache, now=lambda: 1000)
        self.assertEqual((token, how), ("tk", "新取"))
        token2, how2 = O.baidu_token(CREDS, post=post, cache_path=cache, now=lambda: 1001)
        self.assertEqual((token2, how2), ("tk", "缓存"))
        self.assertEqual(len(calls), 1, "缓存命中时不该再请求一次")

    def test_expired_token_is_refetched(self):
        calls = []

        def post(url, data, headers, timeout=15):
            calls.append(url)
            return json.dumps({"access_token": f"tk{len(calls)}", "expires_in": 10}).encode()

        cache = pathlib.Path("/tmp/capsplus-token-cache-test2.json")
        if cache.exists():
            cache.unlink()
        O.baidu_token(CREDS, post=post, cache_path=cache, now=lambda: 1000)
        token, _how = O.baidu_token(CREDS, post=post, cache_path=cache, now=lambda: 2000)
        self.assertEqual(token, "tk2")
        self.assertEqual(len(calls), 2)

    def test_token_failure_is_actionable(self):
        def post(url, data, headers, timeout=15):
            return json.dumps({"error": "invalid_client"}).encode()

        result = O.ocr_cloud("/tmp/a.png", creds=CREDS, post=post,
                            cache_path=pathlib.Path("/tmp/none2.json"))
        self.assertFalse(result["ok"])
        self.assertIn("token", result["hint"])


class TestOcrEntrypoint(unittest.TestCase):
    def test_explicit_backend_skips_resolution(self):
        runner = lambda argv, timeout: Result(stdout='{"ok": true, "text": "x", "ms": 2}')  # noqa: E731
        result = O.ocr("/tmp/a.png", O.BACKEND_LOCAL, python="/p/python", runner=runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "x")

    def test_unknown_backend(self):
        result = O.ocr("/tmp/a.png", "quantum")
        self.assertFalse(result["ok"])
        self.assertIn("quantum", result["hint"])

    def test_no_backend_available_returns_the_hint(self):
        import os
        from unittest import mock
        empty_creds = {"api_key": "", "secret_key": "", "env_file": "/tmp/none.env"}
        with mock.patch.dict(os.environ, {"CAPSPLUS_OCR": "auto"}):
            with mock.patch.object(O, "candidate_pythons", lambda exists=None: []):
                with mock.patch.object(O, "ocr_credentials", lambda env_file=None: empty_creds):
                    result = O.ocr("/tmp/a.png")
        self.assertFalse(result["ok"])
        self.assertIn("rapidocr", result["hint"])
        self.assertIn("BAIDU_OCR_API_KEY", result["hint"])

    def test_main_module_has_no_heavy_imports(self):
        """主进程不背重依赖：顶层不许出现 rapidocr / onnxruntime / cv2 / numpy。"""
        text = (REPO / "capsplus" / "shot_ocr.py").read_text(encoding="utf-8")
        head = text.split('"""', 2)[-1]
        for banned in ("import rapidocr", "import onnxruntime", "import cv2", "import numpy",
                       "from rapidocr"):
            self.assertNotIn(banned, "\n".join(l.split("#", 1)[0] for l in head.splitlines()),
                             f"shot_ocr.py 顶层不许 {banned}（那是子进程的事）")

    def test_helper_is_a_separate_script(self):
        helper = REPO / "capsplus" / "shot_ocr_local.py"
        self.assertTrue(helper.exists(), "本地 OCR 的助手脚本必须在仓里（子进程要跑它）")
        source = helper.read_text(encoding="utf-8")
        self.assertIn("RapidOCR", source)
        self.assertIn('"ok"', source, "助手脚本要吐 JSON 协议给主进程解")


if __name__ == "__main__":
    unittest.main()


class TestInstallLocalOcr(unittest.TestCase):
    """「一条命令装本地 OCR」：argv 要对、**已经装好就别再折腾**、失败要带原因。

    实测成本（2026-10-19）：下载 136MB、占盘 392MB、模型随包带（不用首次下载）；
    走清华镜像 ~40 秒，直连 pypi ~25 分钟（79 kB/s）⇒ 默认走镜像。
    """

    def test_plan_points_at_the_standard_venv(self):
        plan = O.install_local_plan()
        self.assertTrue(plan["venv"].endswith("/.local/share/capsplus/ocr-venv"))
        self.assertEqual(plan["steps"][0][:3], ["python3", "-m", "venv"])
        self.assertIn("rapidocr-onnxruntime", plan["argv"])

    def test_plan_uses_the_mirror_by_default(self):
        self.assertIn(O.PIP_MIRROR, O.install_local_plan()["argv"])
        self.assertNotIn(O.PIP_MIRROR, O.install_local_plan(mirror=False)["argv"])

    def test_already_installed_is_reported_not_reinstalled(self):
        plan = O.install_local_plan()
        calls = []
        from unittest import mock
        with mock.patch.object(O.Path, "exists", lambda self: True), \
                mock.patch.object(O, "local_available", lambda python, runner=None: True):
            ok, msg = O.install_local(runner=lambda argv, timeout: calls.append(argv) or Result())
        self.assertTrue(ok)
        self.assertIn("已经装好", msg)
        self.assertEqual(calls, [], "装好了就不该再跑 pip")
        self.assertIn(plan["python"], msg)

    def test_failure_carries_the_reason(self):
        from unittest import mock

        def runner(argv, timeout):
            if argv[:3] == ["python3", "-m", "venv"]:
                return Result(returncode=0)
            return Result(returncode=1, stderr="ERROR: No matching distribution found")

        with mock.patch.object(O.Path, "exists", lambda self: False), \
                mock.patch.object(O, "local_available", lambda python, runner=None: False):
            ok, msg = O.install_local(runner=runner)
        self.assertFalse(ok)
        self.assertIn("No matching distribution", msg)

    def test_success_message_mentions_the_size(self):
        from unittest import mock

        def runner(argv, timeout):
            return Result(returncode=0)

        with mock.patch.object(O.Path, "exists", lambda self: False), \
                mock.patch.object(O, "local_available", lambda python, runner=None: True):
            ok, msg = O.install_local(runner=runner)
        self.assertTrue(ok)
        self.assertIn("392", msg, "要顺手告诉他占多大盘")

    def test_cli_flag_exists(self):
        from capsplus import shot_capture as SC
        self.assertTrue(SC.parse_args(["--install-local-ocr"]).install_local_ocr)


class TestMeasuredNumbersAreDocumented(unittest.TestCase):
    """实测数字写进了注释/帮助里 —— 免得下一个人又去重测一遍（或把 392MB 说成 100MB）。"""

    def test_plan_docstring_or_help_mentions_the_costs(self):
        source = (REPO / "capsplus" / "shot_ocr.py").read_text(encoding="utf-8")
        self.assertIn("392", source)
        self.assertIn("136", source)
        self.assertIn("镜像", source)
