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


if __name__ == "__main__":
    unittest.main(verbosity=2)
