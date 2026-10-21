# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""识别客户端的单测：WAV 封装 + 对着**契约桩**跑通所有失败分支。

桩是按 `~/voice-svc/daemon.py` 的源码写的（2026-10-17 逐条核对）：
`POST /asr-upload?lang=zh`、body 原始字节、**`raw[:4]==b'RIFF'` 就直接吃**（本机没装 ffmpeg，
所以"我们自己拼 WAV 头"是硬要求，不是洁癖）、200 也可能回 `{"ok":false,"error":…}`、
`bad-size` / 404 / 500 的口径都照抄。

真服务在不在都不影响这批用例（CI 上没有 :18090，也不该有）；
`TestAgainstRealServiceWhenPresent` 在**本机服务起着**时额外跑一次真识别 —— 起不来就显式 skip，
不拿 skip 装绿（跑法见文件末尾）。
"""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_asr as A            # noqa: E402

REAL_SERVICE_HEALTH = "http://127.0.0.1:18090/health"
ZH_WAV = (Path.home() / ".cache/sherpa-onnx"
          / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
          / "test_wavs" / "zh.wav")


class _Server(ThreadingHTTPServer):
    """超时用例里客户端先撤了，桩写回应必然破管 —— 那是**预期**，别刷整屏 traceback。"""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    """契约桩。行为由 `server.mode` 决定，测试只改这一个字段。"""

    def log_message(self, *_a):
        pass

    def _send(self, obj, code=200, raw_text=None):
        body = raw_text.encode("utf-8") if raw_text is not None else \
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"ok": True, "model_loaded": True, "uptime_s": 42})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        self.server.seen.append({"path": self.path, "len": n, "head": raw[:4], "body": raw})
        mode = self.server.mode
        if mode == "sleep":
            import time
            time.sleep(1.2)                     # 比测试给的超时更长
            return self._send({"ok": True, "text": "晚了", "ms": 1200})
        if mode == "ok":
            return self._send({"ok": True, "text": "早上九点开会", "ms": 137})
        if mode.startswith("text:"):
            # 任意文本的桩：术语替换表那条链要拿**指定的识别结果**验（2026-10-20 加）
            return self._send({"ok": True, "text": mode[len("text:"):], "ms": 7})
        if mode == "not_wav":
            if raw[:4] != b"RIFF":
                return self._send({"ok": False, "error": "ffmpeg-failed"})
            return self._send({"ok": True, "text": "是 wav，能吃", "ms": 9})
        if mode == "svc_error":
            return self._send({"ok": False, "error": "boom"})
        if mode == "bad_size":
            return self._send({"ok": False, "error": "bad-size"})
        if mode == "empty_text":
            return self._send({"ok": True, "text": "", "ms": 3})
        if mode == "no_text_key":
            return self._send({"ok": True, "ms": 3})
        if mode == "html":
            return self._send(None, raw_text="<html><body>502 Bad Gateway</body></html>")
        if mode == "not_found":
            return self._send({"ok": False, "error": "not-found"}, 404)
        if mode == "http500_json":
            return self._send({"ok": False, "error": "Traceback…"}, 500)
        if mode == "http500_plain":
            return self._send(None, code=500, raw_text="Internal Server Error")
        return self._send({"ok": False, "error": f"未知 mode {mode}"})


def start_stub(mode="ok"):
    server = _Server(("127.0.0.1", 0), _Handler)
    server.mode = mode
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def with_stub(mode):
    """把桩端口塞进环境，返回 (server, seen)。"""
    server = start_stub(mode)
    port = server.server_address[1]
    patch = mock.patch.dict(os.environ, {"CAPSPLUS_ASR_HOST": "127.0.0.1",
                                         "CAPSPLUS_ASR_PORT": str(port)})
    patch.start()
    return server, server.seen, patch


def stop_stub(server, patch):
    patch.stop()
    server.shutdown()
    server.server_close()


def close_server(server):
    """只关服务器、不动环境变量（配置驱动的用例要的就是"环境变量没设"）。"""
    server.shutdown()
    server.server_close()


def pcm_silence(ms=100):
    n = int(16000 * ms / 1000)
    return b"\x00\x00" * n


class TestPcmToWav(unittest.TestCase):
    def test_header_and_frames_survive_a_round_trip(self):
        payload = bytes(range(256)) * 8
        wav = A.pcm_to_wav(payload, 16000)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        with wave.open(io.BytesIO(wav), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getframerate(), 16000)
            self.assertEqual(w.getnframes(), len(payload) // 2)
            self.assertEqual(w.readframes(w.getnframes()), payload)

    def test_odd_tail_is_dropped_instead_of_raising(self):
        wav = A.pcm_to_wav(b"\x01\x02\x03", 16000)
        with wave.open(io.BytesIO(wav), "rb") as w:
            self.assertEqual(w.getnframes(), 1)

    def test_non_s16_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            A.pcm_to_wav(b"\x00\x00", 16000, sample_width=4)

    def test_empty_pcm_still_produces_a_valid_wav(self):
        """空 PCM 也要给出合法的 44 字节头：`recognize` 靠 `if not pcm` 拦，不靠这里崩。"""
        wav = A.pcm_to_wav(b"", 16000)
        with wave.open(io.BytesIO(wav), "rb") as w:
            self.assertEqual(w.getnframes(), 0)


class TestRecognizeHappyPath(unittest.TestCase):
    def test_text_and_ms_come_back_and_body_is_wav(self):
        server, seen, patch = with_stub("ok")
        self.addCleanup(stop_stub, server, patch)
        result = A.recognize(pcm_silence(200))
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(result.text, "早上九点开会")
        self.assertEqual(result.ms, 137)
        self.assertEqual(seen[0]["head"], b"RIFF",
                         "送出去的不是 WAV —— 本机没装 ffmpeg，服务会直接失败")
        self.assertIn("/asr-upload", seen[0]["path"])
        self.assertIn("lang=zh", seen[0]["path"], "查询串丢了 = 契约不符")
        self.assertEqual(seen[0]["len"], result.body_bytes)

    def test_lang_can_be_overridden(self):
        server, seen, patch = with_stub("ok")
        self.addCleanup(stop_stub, server, patch)
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_LANG": "en"}):
            A.recognize(pcm_silence(50))
        self.assertIn("lang=en", seen[0]["path"])

    def test_wav_bytes_can_be_passed_directly(self):
        server, seen, patch = with_stub("not_wav")
        self.addCleanup(stop_stub, server, patch)
        wav = A.pcm_to_wav(pcm_silence(100))
        result = A.recognize(b"", wav_bytes=wav)
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(seen[0]["head"], b"RIFF")

    def test_health_endpoint_shape(self):
        server, _seen, patch = with_stub("ok")
        self.addCleanup(stop_stub, server, patch)
        result = A.health()
        self.assertTrue(result.ok, result.describe())
        self.assertIn("model_loaded=True", result.text)


class TestFailureBranchesAreStructured(unittest.TestCase):
    """每条失败分支都要有 `code` + **能照着敲**的 hint，且**绝不抛异常**。"""

    def _one(self, mode, pcm=None, **kw):
        server, _seen, patch = with_stub(mode)
        self.addCleanup(stop_stub, server, patch)
        return A.recognize(pcm if pcm is not None else pcm_silence(100), **kw)

    def test_service_level_error(self):
        result = self._one("svc_error")
        self.assertEqual((result.ok, result.code), (False, "svc_error"))
        self.assertIn("boom", result.message)
        self.assertIn("voice-svc", result.hint)

    def test_bad_size_maps_to_its_own_code(self):
        result = self._one("bad_size")
        self.assertEqual(result.code, "bad_size")
        self.assertIn("MiB", result.hint)

    def test_empty_text_is_not_success(self):
        """`ok:true` 但文本为空 = **失败**：把空串上屏等于往用户文档里塞空气。"""
        self.assertEqual(self._one("empty_text").code, "empty")
        self.assertEqual(self._one("no_text_key").code, "empty")

    def test_non_json_body(self):
        result = self._one("html")
        self.assertEqual(result.code, "bad_json")
        self.assertIn("curl", result.hint)

    def test_404_and_500(self):
        self.assertEqual(self._one("not_found").code, "http_error",
                         "404 是契约不符，不许混进「服务内部的错」")
        plain = self._one("http500_plain")
        self.assertEqual((plain.code, plain.message), ("http_error", "HTTP 500"))
        withjson = self._one("http500_json")
        self.assertEqual(withjson.code, "svc_error", "500 里带 ok:false 就按服务的错报")

    def test_timeout(self):
        result = self._one("sleep", timeout=0.3)
        self.assertEqual(result.code, "timeout")
        self.assertIn("restart voice-svc", result.hint)

    def test_unreachable_when_nothing_listens(self):
        """找一个真的没人听的端口：hint 必须给出下一步命令，而不是 URLError。"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_HOST": "127.0.0.1",
                                          "CAPSPLUS_ASR_PORT": str(dead_port)}):
            result = A.recognize(pcm_silence(50), timeout=1.0)
            health = A.health(timeout=1.0)
        self.assertEqual(result.code, "unreachable")
        self.assertEqual(health.code, "unreachable")
        self.assertIn("systemctl --user start voice-svc", result.hint)
        self.assertIn("/health", result.hint)

    def test_empty_pcm_and_oversize_never_touch_the_network(self):
        with mock.patch.object(A, "_request", side_effect=AssertionError("不该发请求")):
            self.assertEqual(A.recognize(b"").code, "no_pcm")
            big = A.pcm_to_wav(b"\x00\x00" * (A.MAX_BODY_BYTES))
            self.assertEqual(A.recognize(b"\x00\x00" * (A.MAX_BODY_BYTES), wav_bytes=big).code,
                             "bad_size")

    def test_describe_never_contains_the_text(self):
        """日志口径判据：`describe()` 只报长度与耗时，**识别出来的话不落盘**。"""
        server, _seen, patch = with_stub("ok")
        self.addCleanup(stop_stub, server, patch)
        result = A.recognize(pcm_silence(50))
        self.assertNotIn("早上九点开会", result.describe())
        self.assertIn("chars=6", result.describe())


class TestPort(unittest.TestCase):
    def test_voice_svc_port_env_is_honoured(self):
        """daemon 自己认 `VOICE_SVC_PORT`，我们跟着它，别要求两边各配一遍。"""
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_PORT": "", "VOICE_SVC_PORT": "19999"},
                             clear=False):
            self.assertEqual(A.port(), 19999)
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_PORT": "18123",
                                          "VOICE_SVC_PORT": "19999"}):
            self.assertEqual(A.port(), 18123, "CAPSPLUS_* 优先，VOICE_SVC_PORT 兜底")
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_PORT": "", "VOICE_SVC_PORT": ""}):
            self.assertEqual(A.port(), A.DEFAULT_PORT)

    def test_garbage_port_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_PORT": "十八点九零"}):
            self.assertEqual(A.port(), A.DEFAULT_PORT)


class TestKeepLastIsOptIn(unittest.TestCase):
    """诊断留证（`CAPSPLUS_VOICE_KEEP_LAST=1`）—— 默认**不落盘**，开了才有上限地留。

    为什么值得有判据（2026-10-20）：用户说"识别不准"时我们手里没有音频，
    只能凭现象猜；这个开关就是为"下次能拿真音频复盘"装的。它同时踩两条线：
    落盘用户说的话（隐私）与磁盘上界（清理），所以默认关 + 上限 + 只清自己的文件，
    三条都得钉住，不能只钉一条。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self._patch = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": str(self.state)})
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(os.environ.pop, A.KEEP_LAST_ENV, None)

    def _run(self):
        server, seen, patch = with_stub("ok")
        self.addCleanup(stop_stub, server, patch)
        return A.recognize(pcm_silence(200)), seen

    def test_default_writes_nothing(self):
        self._run()
        self.assertEqual(list(self.state.glob("*.wav")), [],
                         "默认落盘 = 把用户说的话存在磁盘上，必须显式开")

    def test_enabled_keeps_the_exact_bytes_that_were_sent(self):
        os.environ[A.KEEP_LAST_ENV] = "1"
        result, seen = self._run()
        kept = sorted(self.state.glob(A.KEEP_LAST_GLOB))
        self.assertEqual(len(kept), 1, f"该留一份：{kept}")
        self.assertEqual(kept[0].read_bytes()[:4], b"RIFF")
        self.assertEqual(len(kept[0].read_bytes()), seen[0]["len"],
                         "留的必须是**真正送出去**的那串字节，不是重新拼的")
        self.assertEqual(len(kept[0].read_bytes()), result.body_bytes)

    def test_kept_file_is_private(self):
        os.environ[A.KEEP_LAST_ENV] = "1"
        self._run()
        kept = sorted(self.state.glob(A.KEEP_LAST_GLOB))[0]
        mode = kept.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"用户说的话只给自己读，实际权限 {oct(mode)}")

    def test_runs_are_capped_and_only_ours_are_removed(self):
        """上限清理：多跑几次只留最新的 N 份；状态目录里**别人的文件一个都不许动**。"""
        keep = self.state / "voice.log"
        keep.write_text("老日志", encoding="utf-8")
        stranger = self.state / "voice.sock.bak"
        stranger.write_text("别动我", encoding="utf-8")
        wav = A.pcm_to_wav(pcm_silence(30))
        for _ in range(A.KEEP_LAST_LIMIT + 3):
            A.keep_last_wav(wav)
        kept = sorted(self.state.glob(A.KEEP_LAST_GLOB))
        self.assertEqual(len(kept), A.KEEP_LAST_LIMIT, f"上界没生效：{len(kept)} 份")
        self.assertTrue(keep.exists(), "清理把状态目录里的日志删了 —— 只能删自己那个模式")
        self.assertTrue(stranger.exists(), "清理把不认识的文件删了")
        self.assertEqual(keep.read_text(encoding="utf-8"), "老日志")

    def test_names_sort_in_time_order(self):
        """文件名按名字排就是时间序（清理按最旧的删，靠的就是这条）。"""
        wav = A.pcm_to_wav(pcm_silence(20))
        first = A.keep_last_wav(wav)
        second = A.keep_last_wav(wav)
        self.assertTrue(first and second)
        self.assertLess(first, second, f"后写的必须排在后面：{first} / {second}")


class TestAgainstRealServiceWhenPresent(unittest.TestCase):
    """本机起着 voice-svc 时**真打一次**：契约是活的，桩对不上就红在这儿。"""

    @staticmethod
    def _service_up():
        try:
            with socket.create_connection(("127.0.0.1", A.DEFAULT_PORT), timeout=0.4):
                return True
        except OSError:
            return False

    @unittest.skipUnless(ZH_WAV.exists() and _service_up.__func__(),
                         "本机没有 voice-svc 在 :18090 或没有参考音频（CI 正常没有）")
    def test_real_service_translates_the_reference_clip(self):
        import wave as _wave

        with _wave.open(str(ZH_WAV), "rb") as w:
            pcm = w.readframes(w.getnframes())
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_PORT": str(A.DEFAULT_PORT)}):
            result = A.recognize(pcm)
        self.assertTrue(result.ok, result.describe())
        self.assertGreaterEqual(len(result.text), 2, f"识别回来的该是中文句子：{result.code}")
        self.assertLess(result.ms, 8000, "本机实测 63x 实时，5.6 秒音频不该超过 8 秒")

    @unittest.skipUnless(ZH_WAV.exists() and _service_up.__func__(),
                         "本机没有 voice-svc 在 :18090 或没有参考音频（CI 正常没有）")
    def test_real_health(self):
        result = A.health()
        self.assertTrue(result.ok, result.describe())


class TestGuardHasTeeth(unittest.TestCase):
    """反例具型：每条护栏都得能红。"""

    def test_body_actually_carries_the_pcm(self):
        """如果哪天封装写错（把 PCM 当 WAV 发），桩那边必须看见 RIFF。"""
        server, seen, patch = with_stub("not_wav")
        self.addCleanup(stop_stub, server, patch)
        result = A.recognize(pcm_silence(100))          # 裸 PCM → 我们自己拼头
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(seen[0]["head"], b"RIFF")
        # 反证：真把裸 PCM 发出去，服务就会走到 ffmpeg 那条不存在的路
        bad = A.recognize(b"", wav_bytes=pcm_silence(100))
        self.assertEqual(bad.code, "svc_error")
        self.assertIn("ffmpeg", bad.message)

    def test_empty_text_branch_is_reachable(self):
        """`ok:true` + 空文本必须被判失败 —— 如果哪天改成透传，这条立刻红。"""
        server, _seen, patch = with_stub("empty_text")
        self.addCleanup(stop_stub, server, patch)
        self.assertFalse(A.recognize(pcm_silence(50)).ok)

    def test_timeout_is_really_measured(self):
        """超时值真的生效：给 1.2s 的桩、0.3s 的超时 → 必须报 timeout。"""
        server, _seen, patch = with_stub("sleep")
        self.addCleanup(stop_stub, server, patch)
        self.assertEqual(A.recognize(pcm_silence(50), timeout=0.3).code, "timeout")

    def test_hint_table_covers_every_code(self):
        """新加错误码忘了配 hint = 用户看到一句没有下一步的错误。"""
        for code in ("unreachable", "timeout", "http_error", "bad_json", "svc_error",
                     "bad_size", "empty", "no_pcm"):
            self.assertIn(code, A.HINTS, f"HINTS 少了 {code}")
            self.assertTrue(A.HINTS[code].strip(), f"HINTS[{code}] 是空的")


class TestGlossaryReachesTheRecognitionText(unittest.TestCase):
    """术语替换表必须**接在识别结果上**，而且它坏了不许把识别带崩。

    为什么这批判据值得写（2026-10-20）：SenseVoice 这个模型**没有热词**可用
    （理由写在 `voice_glossary` 的模块 docstring 里），于是"专名总是认错"这件事只能靠
    一层后处理兜。光有纯函数不够 —— 它得真的走在 `recognize()` 的成功路径上，
    而且 `AsrResult.terms` 要能把"这次改了几处"带出去，否则用户看到的还是一个说不清的"识别率低"。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.table = Path(self._tmp.name) / "glossary.tsv"
        self.table.write_text("帮晒\tBonsai\n", encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_GLOSSARY": str(self.table)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _recognize(self, reply_text: str):
        server, _seen, patch = with_stub(f"text:{reply_text}")
        self.addCleanup(stop_stub, server, patch)
        return A.recognize(pcm_silence(50))

    def test_the_table_rewrites_the_text_and_the_count_comes_back(self):
        result = self._recognize("帮晒这个项目")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Bonsai这个项目")
        self.assertEqual(result.terms, 1, "命中数没带出来，用户就没法知道表到底生没生效")
        self.assertIn("terms=1", result.describe())

    def test_no_hit_leaves_the_text_alone(self):
        result = self._recognize("早上九点开会")
        self.assertEqual(result.text, "早上九点开会")
        self.assertEqual(result.terms, 0)

    def test_a_broken_table_never_breaks_recognition(self):
        """表是**用户自己改的数据**：它坏掉时正确的表现是"这次没替换"，不是"这次没识别"。"""
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_GLOSSARY": "/dev/null/没有这个目录/x"}):
            from capsplus import voice_glossary as G
            with mock.patch.object(G, "load", side_effect=OSError("表读炸了")):
                result = self._recognize("帮晒这个项目")
        self.assertTrue(result.ok, "表坏了把识别也带崩了")
        self.assertEqual(result.text, "帮晒这个项目")
        self.assertEqual(result.terms, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ══════════════════════════════════════════════════════════════════════════
# 2026-10-21：服务配置可配（`services.toml`）—— 识别服务的落点从配置来
# ══════════════════════════════════════════════════════════════════════════
# 老判据一条没改的理由：这一轮**没有改任何既有行为** ——
#   · 没有配置文件时，落点/语言/超时与 2026-10-21 之前一致；
#   · 有配置文件时，老的三个环境变量（`CAPSPLUS_ASR_HOST` / `CAPSPLUS_ASR_PORT` /
#     `CAPSPLUS_ASR_LANG`）仍然认，而且优先级更高。

CONFIG_ASR = """
[asr]
mode = "local"
lang = "zh"
timeout_s = 25

[asr.local]
kind = "local"
style = "http"
base_url = "http://192.0.2.10:18090"
model = "sense_voice"
"""

CONFIG_ASR_HOSTPORT = """
[asr.local]
host = "192.0.2.11"
port = 18091
"""


@contextlib.contextmanager
def services_file(text: str):
    """把服务配置指到一段临时 TOML（判据**不许碰**用户真配的那份）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "services.toml"
        path.write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, {"CAPSPLUS_SERVICES_FILE": str(path)}):
            yield path


def setUpModule():
    """**没配置**的状态是老判据的前提 —— 先把真配置挡在外面。"""
    os.environ["CAPSPLUS_SERVICES_FILE"] = "/tmp/capsplus-no-such-services.toml"


def tearDownModule():
    os.environ.pop("CAPSPLUS_SERVICES_FILE", None)


class TestAsrBaseUrlComesFromTheConfig(unittest.TestCase):
    """① 识别服务的落点从 `[asr]` 来（本机 or 远程任选）。"""

    def test_the_base_url_is_read_from_the_config(self):
        with services_file(CONFIG_ASR):
            self.assertEqual(A.base_url(), "http://192.0.2.10:18090")
            self.assertEqual(A.host(), "192.0.2.10")
            self.assertEqual(A.port(), 18090)

    def test_host_and_port_may_be_written_instead_of_a_base_url(self):
        with services_file(CONFIG_ASR_HOSTPORT):
            self.assertEqual(A.base_url(), "http://192.0.2.11:18091")

    def test_the_request_really_goes_to_the_configured_address(self):
        """不只是"读出来"—— 请求必须真的打到配置里那个地址。"""
        server = start_stub("ok")
        self.addCleanup(close_server, server)
        port = server.server_address[1]
        with services_file(f'[asr.local]\nbase_url = "http://127.0.0.1:{port}"\n'):
            with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_HOST": "",
                                              "CAPSPLUS_ASR_PORT": "",
                                              "VOICE_SVC_PORT": ""}):
                result = A.recognize(pcm_silence(50))
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(len(server.seen), 1, "请求没落到配置的地址上")

    def test_the_lang_comes_from_the_config(self):
        with services_file("[asr]\nlang = \"en\"\n"):
            self.assertEqual(A.lang(), "en")

    def test_the_timeout_comes_from_the_config(self):
        with services_file("[asr]\ntimeout_s = 45\n"):
            self.assertEqual(A.recognize_timeout(), 45.0)

    def test_without_a_config_the_old_defaults_hold(self):
        self.assertEqual(A.base_url(), f"http://{A.DEFAULT_HOST}:{A.DEFAULT_PORT}")
        self.assertEqual(A.lang(), A.DEFAULT_LANG)
        self.assertEqual(A.recognize_timeout(), A.RECOGNIZE_TIMEOUT_S)


class TestAsrEnvironmentBeatsTheConfigFile(unittest.TestCase):
    """② **环境变量 > 配置文件 > 内置默认**（那三个变量是现场救火用的，不许被配置挡住）。"""

    def test_the_host_and_port_env_vars_beat_the_config(self):
        with services_file(CONFIG_ASR):
            with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_HOST": "127.0.0.1",
                                              "CAPSPLUS_ASR_PORT": "18123"}):
                self.assertEqual(A.base_url(), "http://127.0.0.1:18123")
                self.assertEqual(A.port(), 18123)

    def test_the_lang_env_var_beats_the_config(self):
        with services_file("[asr]\nlang = \"en\"\n"):
            with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_LANG": "ja"}):
                self.assertEqual(A.lang(), "ja")

    def test_the_voice_svc_port_still_wins_when_capsplus_port_is_empty(self):
        with services_file(CONFIG_ASR):
            with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_PORT": "",
                                              "VOICE_SVC_PORT": "19999"}):
                self.assertEqual(A.port(), 19999)


class TestRemoteOutageSaysWhatToDoNext(unittest.TestCase):
    """③ **远端**不可达时，提示要带"下一步"（而且是**在那台机器上**做什么）。"""

    def test_a_remote_outage_names_the_target_and_the_next_step(self):
        with services_file('[asr.local]\nbase_url = "http://192.0.2.99:18090"\n'):
            result = A.recognize(pcm_silence(50), timeout=0.6)
        self.assertFalse(result.ok)
        self.assertIn(result.code, ("unreachable", "timeout"))
        self.assertIn("192.0.2.99", result.hint, "要报出连的是哪台")
        self.assertIn("那台机器", result.hint,
                      "远程的提示必须说清『在**那台机器**上』做什么 —— 在本机敲 systemctl 是错的")
        self.assertIn("voice-svc", result.hint, "要给出能照着敲的命令")

    def test_a_local_outage_keeps_the_local_wording(self):
        """本机那档的话术不许被远程那套覆盖（老判据钉的就是它）。"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_HOST": "127.0.0.1",
                                          "CAPSPLUS_ASR_PORT": str(dead_port)}):
            result = A.recognize(pcm_silence(50), timeout=1.0)
        self.assertEqual(result.code, "unreachable")
        self.assertIn("systemctl --user start voice-svc", result.hint)
        self.assertNotIn("那台机器", result.hint, "本机故障不该说『那台机器』")


class TestCounterExamples(unittest.TestCase):
    """反例具型：每条判据都得能红。**判据抓不住东西 = 没有判据。**"""

    def test_counterexample_config_beating_the_env_var_is_caught(self):
        """反例 ①：把优先级反过来（配置压过环境变量）⇒ 判据必须红。"""
        with services_file(CONFIG_ASR):
            with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_HOST": "127.0.0.1",
                                              "CAPSPLUS_ASR_PORT": "18123"}):
                good = A.base_url()
                with mock.patch.object(A, "_pick", lambda e, c, d: c or e or d):
                    bad = A.base_url()
        self.assertEqual(good, "http://127.0.0.1:18123",
                         "判据本体：环境变量必须压过配置文件")
        self.assertNotEqual(bad, "http://127.0.0.1:18123",
                            "优先级写反了这条判据抓不住 —— 它就是摆设")

    def test_counterexample_the_old_env_var_dying_is_caught(self):
        """反例 ②：让老的 `CAPSPLUS_ASR_PORT` 失效 ⇒ 判据必须红。"""
        with services_file(CONFIG_ASR):
            with mock.patch.dict(os.environ, {"CAPSPLUS_ASR_PORT": "18123"}):
                good = A.port()
                with mock.patch.object(A, "_env_port", lambda: 0):
                    bad = A.port()
        self.assertEqual(good, 18123, "判据本体：老的端口环境变量必须仍然生效")
        self.assertNotEqual(bad, 18123, "老环境变量失效这条判据抓不住 —— 它就是摆设")

    def test_counterexample_the_config_being_ignored_is_caught(self):
        """反例 ③：配置里的地址不生效（退回写死的本机端口）⇒ 判据必须红。"""
        with services_file(CONFIG_ASR):
            good = A.base_url()
            with mock.patch.object(A, "_config_base_url", lambda cfg=None: ""):
                bad = A.base_url()
        self.assertEqual(good, "http://192.0.2.10:18090",
                         "判据本体：地址必须来自服务配置")
        self.assertNotEqual(bad, "http://192.0.2.10:18090",
                            "配置不生效这条判据抓不住 —— 它就是摆设")


if __name__ == "__main__":
    unittest.main(verbosity=2)
