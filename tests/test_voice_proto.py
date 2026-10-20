# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""语音输入协议层与 worker 生命周期的单测。

为什么这套用例值得存在（2026-10-17）：语音输入的按键路径**只看得到结果**（字上屏了没有），
中间任何一环坏了都是"按了没反应"。所以这里把四类东西钉死：

  ① 帧格式：坏输入必须归成 error 帧而不是把服务端抛死（`extcheck.py` 同口径）；
  ② socket 单实例语义：陈旧 socket 要清掉并自起 worker（照 Qbar 的实测行为）；
  ③ 权限 0600：这个 socket 收的是用户说的话，不给同机别人连；
  ④ 整链路（假麦克风 → 真判定 → 假识别服务 → 假上屏）：状态序列与**降级路径**，
     外加一条隐私判据 —— **识别出来的文本绝不进日志**。

`TestWorkerEndToEnd` 里那些"注入"全用假解释器/假 runner，**不会往屏幕上打任何一个键**，
也不会开任何窗口（`CAPSPLUS_VOICE_UI=0`）。真窗口的用例在 `tests/test_voice_portability.py`
里，默认 skip，只有 `CAPSPLUS_GUI=1` 才跑。
"""

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_proto as P          # noqa: E402
from capsplus import voice_worker as W         # noqa: E402

NOGI_NOTE = "协议层/服务端不许 import gi（CI 不装 PyGObject）"


# ── 假识别服务：照 voice-svc 的契约答话（POST 字节、RIFF 就直接吃、回 JSON）──────
class _StubHandler(BaseHTTPRequestHandler):
    seen = []                                   # 类级：每个实例都往里记

    def log_message(self, *_a):                 # 别往 stderr 吐访问日志
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        type(self).seen.append({"path": self.path, "bytes": len(raw), "riff": raw[:4]})
        reply = self.server.reply                               # 由测试注入
        body = json.dumps(reply, ensure_ascii=False).encode("utf-8")
        code = getattr(self.server, "status_code", 200)
        self.send_response(code)
        if getattr(self.server, "raw_body", False):
            self.send_header("Content-Type", "text/html")
        else:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = json.dumps(self.server.health).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def close_stub(server):
    server.shutdown()
    server.server_close()          # 不关监听 socket 会刷一堆 ResourceWarning


def start_stub(reply=None, health=None, status_code=200, raw_body=False):
    """起一个本机假识别服务，返回 (server, port, seen)。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.reply = reply if reply is not None else {"ok": True, "text": "测试文本", "ms": 7}
    server.health = health if health is not None else {"ok": True, "model_loaded": True,
                                                       "uptime_s": 1}
    server.status_code = status_code
    server.raw_body = raw_body
    _StubHandler.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], _StubHandler.seen


# ── 假麦克风：把 PCM 当成 arecord 的 stdout 一帧帧给 ──────────────────────────
class FakeStream:
    def __init__(self, data: bytes):
        self._io = MemoryFile(data)

    def read(self, n=-1):
        return self._io.read(n)

    def close(self):
        pass


class MemoryFile:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self.data) - self.pos
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


class FakeProc:
    returncode = 0

    def __init__(self, argv, **_kw):
        self.stdout = FakeStream(SPEECH_PCM)
        self.stderr = FakeStream(b"")

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def speech_pcm() -> bytes:
    """2 秒 440Hz 正弦（RMS≈2300），CI 上没有真人语音文件时的"有声"料。"""
    import math
    import struct

    rate = 16000
    samples = [int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate * 2)]
    return struct.pack("<%dh" % len(samples), *samples)


SPEECH_PCM = speech_pcm()


def quiet_pcm(ms: int = 1000) -> bytes:
    import struct

    n = int(16000 * ms / 1000)
    return struct.pack("<%dh" % n, *([12] * n))


class TestFrameFormat(unittest.TestCase):
    def test_round_trip_keeps_chinese_readable(self):
        frame = P.make_event("text", text="你好， Caps+M", text_len=8)
        wire = P.encode(frame)
        self.assertTrue(wire.endswith(b"\n"))
        self.assertIn("你好", wire.decode("utf-8"), "ensure_ascii=False 失效 = 日志与线上都变乱码")
        self.assertEqual(P.parse_frame(wire.decode("utf-8"))["text"], "你好， Caps+M")

    def test_newline_in_a_value_stays_one_wire_line(self):
        """值里带换行也必须只出一帧：行协议靠"一帧一行"，不是靠运气。"""
        wire = P.encode({"kind": "state", "note": "坏\n行"})
        self.assertEqual(wire.count(b"\n"), 1)
        self.assertEqual(P.parse_frame(wire.decode())["note"], "坏\n行")

    def test_frame_over_limit_is_refused(self):
        with self.assertRaises(ValueError):
            P.encode({"kind": "text", "text": "x" * (P.MAX_FRAME_BYTES + 10)})

    def test_bad_input_becomes_error_frame_not_exception(self):
        cases = {
            "": "empty_frame",
            "not json": "bad_json",
            "[1,2]": "not_object",
            '{"cmd":"dance"}': "bad_command",
            '{"kind":"song"}': "bad_kind",
            '{"kind":"state","state":"spoofed"': "bad_json",
        }
        for text, code in cases.items():
            with self.subTest(text=text):
                self.assertEqual(P.parse_frame(text)["code"], code)

    def test_command_and_event_whitelists_are_enforced(self):
        for bad in (lambda: P.make_command("reboot"), lambda: P.make_event("toast"),
                    lambda: P.state_event("paused")):
            with self.assertRaises(ValueError):
                bad()

    def test_parse_accepts_every_documented_command(self):
        for cmd in P.COMMANDS:
            self.assertEqual(P.parse_frame(P.encode(P.make_command(cmd)).decode())["cmd"], cmd)

    def test_event_and_state_names_are_the_documented_sets(self):
        self.assertEqual(P.COMMANDS, ("start", "stop", "status", "ping"))
        self.assertEqual(P.EVENT_KINDS, ("state", "text", "error"))
        self.assertEqual(P.STATES, ("idle", "recording", "decoding", "review"))


class TestPaths(unittest.TestCase):
    def test_state_dir_and_socket_follow_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": tmp}):
                self.assertEqual(P.state_dir(), Path(tmp))
                self.assertEqual(P.socket_path(), Path(tmp) / "voice.sock")
                self.assertEqual(P.log_path(), Path(tmp) / "voice.log")

    def test_socket_path_can_be_overridden_entirely(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_SOCK": "/tmp/zz-voice.sock"}):
            self.assertEqual(str(P.socket_path()), "/tmp/zz-voice.sock")

    def test_module_source_has_no_hardcoded_home(self):
        text = (REPO / "capsplus" / "voice_proto.py").read_text(encoding="utf-8")
        self.assertNotIn("/home/", text, "写死家目录 = 换台机器就坏（判据同 test_portability）")
        self.assertNotIn("/run/user/", text)

    def test_no_gi_import_anywhere_in_proto_or_server(self):
        for name in ("voice_proto.py", "voice_worker.py", "voice_recorder.py",
                     "voice_vad.py", "voice_asr.py", "voice_inject.py"):
            with self.subTest(module=name):
                text = (REPO / "capsplus" / name).read_text(encoding="utf-8")
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("import gi", "from gi.")):
                        self.assertTrue(line.startswith(("    ", "\t")),
                                        f"{name}: 顶层 import gi，{NOGI_NOTE}")


class TestTogglePlan(unittest.TestCase):
    def test_idle_and_unknown_start_recording(self):
        for state in (None, "idle", "没读到"):
            self.assertEqual(P.toggle_plan(state), "start", state)

    def test_any_live_state_stops(self):
        for state in ("recording", "decoding", "review"):
            self.assertEqual(P.toggle_plan(state), "stop", state)


class TestSocketLifecycle(unittest.TestCase):
    """单实例语义 + 陈旧 socket：这四条是 Qbar 实测过的行为，语音这条链也得有。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.sock = self.dir / "voice.sock"
        self._patch = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": str(self.dir)})
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_bind_then_ping_reports_alive_with_state(self):
        server = P.bind_server(self.sock)
        try:
            threading.Thread(target=lambda: self._answer_one(server), daemon=True).start()
            alive, reason = P.ping(self.sock)
            self.assertTrue(alive, reason)
            self.assertIn("state=review", reason)
        finally:
            P.release_server(server, self.sock)

    def test_socket_file_is_owner_only(self):
        server = P.bind_server(self.sock)
        try:
            mode = stat.S_IMODE(os.stat(self.sock).st_mode)
            self.assertEqual(mode, 0o600, f"socket 权限 {oct(mode)}：同机任何用户都能问到你说的话")
        finally:
            P.release_server(server, self.sock)

    def test_second_instance_refuses_to_steal_the_socket(self):
        server = P.bind_server(self.sock)
        try:
            with self.assertRaises(OSError) as ctx:
                P.bind_server(self.sock)
            self.assertIn("已有 worker", str(ctx.exception))
        finally:
            P.release_server(server, self.sock)

    def test_stale_socket_is_unlinked_and_ping_says_cleaned(self):
        """进程没了但文件还在（kill -9）：必须清掉并说"已清理"，按键下一次就能自起。"""
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.sock))
        stale.close()                                   # 文件留下，但没人 listen
        self.assertTrue(self.sock.exists())
        alive, reason = P.ping(self.sock)
        self.assertFalse(alive)
        self.assertIn("已清理", reason)
        self.assertFalse(self.sock.exists(), "陈旧 socket 没删 = 下次启动还是连不上")

    def test_release_removes_file_even_if_listener_is_gone(self):
        server = P.bind_server(self.sock)
        P.release_server(server, self.sock)
        self.assertFalse(self.sock.exists())
        P.release_server(server, self.sock)             # 再清一次不许抛

    def test_request_when_no_worker_returns_structured_error(self):
        frames = P.request("start", path=self.sock)
        self.assertEqual(frames[0]["code"], "unreachable")
        self.assertIn("capsplus-voice start", frames[0]["hint"])

    @staticmethod
    def _answer_one(server):
        conn, _ = server.accept()
        with conn:
            conn.recv(64)
            conn.sendall(P.encode(P.state_event("review", note="测试")))


class TestReadFrames(unittest.TestCase):
    def test_two_frames_in_one_chunk_are_both_parsed(self):
        a, b = socket.socketpair()
        with a, b:
            b.sendall(P.encode(P.state_event("idle")) + P.encode(P.error_event("boom", "x")))
            frames = P.read_frames(a, limit=2, timeout=1.0)
        self.assertEqual([f["kind"] for f in frames], ["state", "error"])

    def test_timeout_becomes_error_frame(self):
        a, b = socket.socketpair()
        with a, b:
            frames = P.read_frames(a, limit=1, timeout=0.2)
        self.assertEqual(frames[0]["code"], "read_timeout")

    def test_peer_close_is_not_an_error(self):
        a, b = socket.socketpair()
        b.close()
        with a:
            self.assertEqual(P.read_frames(a, limit=3, timeout=0.5), [])


class TestLevelsAndTruncationReachTheUser(unittest.TestCase):
    """录音**结束后**的两个可感知出口：电平进了 status，截断进了事件与 status。

    为什么单独立类（2026-10-20）：`voice_vad` / `voice_levels` 里算得再对，只要这两条没接到
    worker 的出口上，明江看到的仍然是"识别率低"和"我后半句没了" —— 判据必须钉在**看得见的地方**。
    全程假麦克风 + 假识别服务 + 不弹窗（`CAPSPLUS_VOICE_UI=0`）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(os.environ, {
            "CAPSPLUS_STATE_DIR": self._tmp.name,
            "CAPSPLUS_VOICE_UI": "0",
        })
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, pcm: bytes, params=None):
        from capsplus import voice_inject as INJ
        from capsplus import voice_vad as V

        server, port, _seen = start_stub(reply={"ok": True, "text": "电平要看得见", "ms": 5})
        self.addCleanup(close_stub, server)
        os.environ["CAPSPLUS_ASR_PORT"] = str(port)
        srv = W.Server(params=params or V.VadParams(max_ms=4000),
                       popener=lambda argv, **kw: self._proc(pcm),
                       run_command=lambda argv, **kw: type("R", (), {"returncode": 0,
                                                                    "stderr": b""})(),
                       review_ui=False)
        srv.probe = lambda: INJ.Probe(wl_copy="/bin/true")
        events: list = []
        srv.broadcast = events.append
        session = srv._ensure_session()
        session.start_recording()
        self.assertTrue(session.wait_done(20), "会话没走完")
        return srv, session, events

    @staticmethod
    def _proc(pcm):
        proc = FakeProc([])
        proc.stdout = FakeStream(pcm)
        return proc

    def test_status_carries_the_segment_levels(self):
        srv, session, _events = self._run(SPEECH_PCM)
        frame = srv._status_frame()
        self.assertTrue(frame.get("level"), f"status 里没有电平那一行：{frame.get('level')!r}")
        for key in ("rms=", "peak=", "voiced_rms=", "gain="):
            self.assertIn(key, frame["level"], f"电平行少了 {key}：{frame['level']}")
        self.assertEqual(frame["level"], session.level_summary)
        self.assertEqual(frame.get("truncated"), "", "没截断时不许挂截断提示")

    def test_log_gets_one_level_line_per_utterance(self):
        self._run(SPEECH_PCM)
        log = P.log_path().read_text(encoding="utf-8")
        # 日志行前面带 ISO 时间戳（`P.log_line` 加的），所以按"行内出现"匹配
        lines = [ln for ln in log.splitlines() if " voice_level " in ln]
        self.assertEqual(len(lines), 1, f"每次录音该有且只有一行 voice_level：{log}")
        for key in ("rms=", "peak=", "voiced_rms=", "gain=", "gate="):
            self.assertIn(key, lines[0], f"日志行少了 {key}：{lines[0]}")

    def test_hitting_the_limit_is_announced_not_silent(self):
        import math
        import struct

        # 6 秒连续有声、上限 4 秒 → 必然 max_duration
        n = 16000 * 6
        pcm = struct.pack("<%dh" % n, *[int(6000 * math.sin(2 * math.pi * 220 * i / 16000))
                                        for i in range(n)])
        srv, session, events = self._run(pcm)
        notes = [e.get("note", "") for e in events if e.get("state") == "decoding"]
        self.assertTrue(notes, f"没看到 decoding 事件：{events}")
        self.assertIn("上限", notes[0], f"decoding 的说明里必须写明是到上限了：{notes[0]}")
        self.assertIn("截断", notes[0])
        self.assertTrue(srv._status_frame().get("truncated"),
                        f"status 里也得有这条：{srv._status_frame()}")
        self.assertTrue(session.truncation)
        log = P.log_path().read_text(encoding="utf-8")
        self.assertIn("truncated ", log, "日志里也要留一条，事后才查得出")


class TestMeterIsSideEffectFree(unittest.TestCase):
    """`capsplus-voice meter` 只量麦克风：**不动会话、不写 socket、不弹窗**（交付要求如此）。

    它跑在**客户端进程**里（`capsplus-voice` → `voice_worker.py meter`），所以判据直接调
    `W.cmd_meter` 这一层：不经过 `Server`，也就不该有任何一条会话事件。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(os.environ, {
            "CAPSPLUS_STATE_DIR": self._tmp.name,
            "CAPSPLUS_VOICE_SOCK": str(Path(self._tmp.name) / "voice.sock"),
        })
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run_meter(self, pcm: bytes) -> str:
        import contextlib
        import io

        raw = Path(self._tmp.name) / "probe.pcm"
        raw.write_bytes(pcm)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_METER_PCM": str(raw)}):
            with contextlib.redirect_stdout(buf):
                code = W.cmd_meter(1.0)
        self.assertEqual(code, 0, f"meter 返回 {code}：{buf.getvalue()}")
        return buf.getvalue()

    def test_reads_a_file_and_prints_the_six_numbers(self):
        out = self._run_meter(SPEECH_PCM)
        for label in ("RMS", "分位", "过门帧", "底噪", "当前门限", "建议增益"):
            self.assertIn(label, out, f"meter 输出少了「{label}」：{out}")

    def test_no_socket_and_no_session_side_effects(self):
        self._run_meter(SPEECH_PCM)
        self.assertFalse(P.socket_path().exists(), "meter 建了 socket = 动到会话了")
        self.assertFalse(P.ping()[0], "meter 把 worker 拉起来了")

    def test_missing_input_is_an_error_not_a_crash(self):
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_METER_PCM": "/nonexistent/x.pcm"}):
            import contextlib
            import io

            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                code = W.cmd_meter(1.0)
        self.assertEqual(code, 1, "读不到输入该返回 1，不是抛出去")
        self.assertIn("meter", buf.getvalue())


class TestWorkerEndToEnd(unittest.TestCase):
    """整链路：假麦克风 → VAD → 假识别服务 → 假上屏。**不开窗、不打键**。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(os.environ, {
            "CAPSPLUS_STATE_DIR": self._tmp.name,
            "CAPSPLUS_VOICE_UI": "0",          # 明江红线：不许弹窗
        })
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run_session(self, pcm: bytes, reply: dict, backend: str = "clipboard",
                     verified: bool = True):
        from capsplus import voice_inject as INJ
        from capsplus import voice_vad as V

        server, port, seen = start_stub(reply=reply)
        self.addCleanup(close_stub, server)
        os.environ["CAPSPLUS_ASR_PORT"] = str(port)

        calls: list = []

        def fake_run(argv, **kw):
            calls.append((argv, kw.get("input")))
            return type("R", (), {"returncode": 0, "stderr": b""})()

        probe = INJ.Probe(wl_copy="/bin/true", wl_copy_primary=True, ydotool="/bin/true",
                          syntax=("keycode", "click-bitmask"),
                          keycodes={"KEY_LEFTCTRL": 29, "KEY_V": 47},
                          ydotool_daemon=True, keycodes_source="/tmp/h")
        if not verified:
            probe = INJ.Probe(wl_copy="/bin/true")

        srv = W.Server(params=V.VadParams(max_ms=4000, silence_stop_ms=500),
                       popener=lambda argv, **kw: self._proc(pcm),
                       run_command=fake_run, review_ui=False)
        srv.backend = backend
        srv.probe = lambda: probe
        events: list = []
        srv.broadcast = events.append

        session = srv._ensure_session()
        session.start_recording()
        self.assertTrue(session.wait_done(15), "会话没走完（状态机或线程挂了）")
        return session, events, calls, seen

    @staticmethod
    def _proc(pcm):
        proc = FakeProc([])
        proc.stdout = FakeStream(pcm)
        return proc

    def test_speech_goes_through_all_four_states_and_injects(self):
        session, events, calls, seen = self._run_session(SPEECH_PCM,
                                                        {"ok": True, "text": "早上九点开会", "ms": 9})
        states = [e.get("state") for e in events if e.get("kind") == "state"]
        self.assertIn("recording", states)
        self.assertIn("decoding", states)
        self.assertIn("idle", states)
        self.assertEqual(session.state, "idle")
        self.assertEqual(session.text, "早上九点开会")
        self.assertTrue(session.inject_summary.startswith("clipboard"), session.inject_summary)
        # 送出去的必须是 WAV（服务靠 RIFF 判断"不用 ffmpeg"）
        self.assertEqual(seen[0]["riff"], b"RIFF", "喂给服务的不是 WAV，本机没装 ffmpeg 会直接失败")
        self.assertIn("lang=zh", seen[0]["path"])
        # 注入两步：写剪贴板 + Ctrl+V
        self.assertEqual([argv[0] for argv, _ in calls], ["/bin/true", "/bin/true"])
        self.assertEqual(calls[1][0][1:3], ["key", "29:1"])
        self.assertEqual(calls[0][1], "早上九点开会".encode("utf-8"),
                         "wl-copy 该走 stdin，不该把用户说的话拼进命令行参数")
        self.assertNotIn("早上九点开会", " ".join(calls[0][0]))

    def test_ui_unavailable_falls_back_to_direct_injection(self):
        """`review` 状态在没 UI 时**不出现**，文本直接交上屏后端（交付要求里的降级）。"""
        _session, events, calls, _seen = self._run_session(SPEECH_PCM,
                                                          {"ok": True, "text": "降级直投", "ms": 5})
        states = [e.get("state") for e in events if e.get("kind") == "state"]
        self.assertNotIn("review", states)
        self.assertEqual(len(calls), 2)

    def test_unverified_backend_never_pretends_to_inject(self):
        session, events, calls, _seen = self._run_session(
            SPEECH_PCM, {"ok": True, "text": "别乱注入", "ms": 5}, verified=False)
        self.assertIn("没自动上屏", session.inject_summary)      # 不许把它当成功报
        self.assertEqual([a for a, _ in calls], [["/bin/true"]],
                         "探测没过还发了按键 = 拿用户的屏幕当试验台")
        self.assertFalse([a for a in calls if len(a[0]) > 1 and a[0][1] == "key"],
                         "探测没过还发了按键 = 拿用户的屏幕当试验台")

    def test_failed_window_falls_back_to_injection_not_into_a_trash(self):
        """弹窗走不通时**不许把文本静默丢掉** —— 退回直接上屏。

        2026-10-17 重构后的形状：开窗搬到了**主线程**（`_open_review`），单测里没有跑动的
        主循环 —— `post()` 原地调用，`_open_review` 的"主线程才碰 GTK"门岗拒绝（这里
        `_decode_and_review` 在后台线程，正是那条路）。`open_review` 返回 failed /
        unavailable 的两个分支由 `tests/test_voice_mainloop.py::TestReviewDegradation`
        用真主线程逐个验。
        """
        from capsplus import voice_inject as INJ
        from capsplus import voice_vad as V

        server, port, _seen = start_stub(reply={"ok": True, "text": "弹窗没起来也要给字", "ms": 6})
        self.addCleanup(close_stub, server)
        os.environ["CAPSPLUS_ASR_PORT"] = str(port)
        calls = []
        srv = W.Server(params=V.VadParams(max_ms=4000),
                       popener=lambda argv, **kw: self._proc(SPEECH_PCM),
                       run_command=lambda argv, **kw: (calls.append(list(argv)) or
                                                       type("R", (), {"returncode": 0,
                                                                      "stderr": b""})()),
                       review_ui=True)
        srv.probe = lambda: INJ.Probe(wl_copy="/bin/true", wl_copy_primary=True,
                                      ydotool="/bin/true", syntax=("keycode", "click-bitmask"),
                                      keycodes={"KEY_LEFTCTRL": 29, "KEY_V": 47},
                                      ydotool_daemon=True)
        events = []
        srv.broadcast = events.append
        session = srv._ensure_session()
        with mock.patch.object(W.Session, "_ui_available", return_value=True):
            session.start_recording()
            self.assertTrue(session.wait_done(20))
        self.assertEqual(session.state, "idle")
        self.assertTrue(calls, "弹窗失败后一个上屏命令都没发 = 用户的字被吞了")
        self.assertIn("已上屏", session.inject_summary)
        notes = [e.get("note", "") for e in events if e.get("kind") == "text"]
        self.assertTrue(any("弹窗没走成" in n for n in notes),
                        f"降级要让用户看得见是怎么走的：{notes}")

    def test_service_failure_is_broadcast_and_session_ends_idle(self):
        session, events, calls, _seen = self._run_session(SPEECH_PCM,
                                                         {"ok": False, "error": "bad-size"})
        codes = [e.get("code") for e in events if e.get("kind") == "error"]
        self.assertIn("bad_size", codes)
        self.assertEqual(session.state, "idle")
        self.assertEqual(calls, [], "识别没成就不该动上屏")

    def test_recognized_text_never_reaches_the_log(self):
        """隐私判据：日志只记状态与耗时，**用户说的话一个字都不落盘**。"""
        secret = "我把银行卡号念出来了"
        session, _events, _calls, _seen = self._run_session(SPEECH_PCM,
                                                           {"ok": True, "text": secret, "ms": 3})
        self.assertEqual(session.text, secret)                  # 确认它真的走完了链路
        log = P.log_path().read_text(encoding="utf-8")
        self.assertNotIn(secret, log)
        self.assertNotIn(secret, json.dumps(_events, ensure_ascii=False),
                         "事件帧里不许带文本，只许带长度")

    def test_no_speech_never_reaches_the_asr_service(self):
        """底噪必须被能量门挡在识别之前 —— 服务会把 3 秒底噪识别成字（本机实测）。"""
        from capsplus import voice_inject as INJ

        server, port, seen = start_stub(reply={"ok": True, "text": "嗯", "ms": 4})
        self.addCleanup(close_stub, server)
        os.environ["CAPSPLUS_ASR_PORT"] = str(port)
        from capsplus import voice_vad as V

        probe = INJ.Probe(wl_copy="/bin/true")
        srv = W.Server(params=V.VadParams(max_ms=4000), popener=lambda argv, **kw: self._proc(
            quiet_pcm(2000)), run_command=lambda argv, **kw: None, review_ui=False)
        srv.probe = lambda: probe
        events = []
        srv.broadcast = events.append
        session = srv._ensure_session()
        session.start_recording()
        session.wait_done(15)
        self.assertEqual(seen, [], "静音样本被送去识别了：能量门没起作用")
        codes = [e.get("code") for e in events if e.get("kind") == "error"]
        self.assertIn("no_speech", codes)

    def test_abandoned_session_does_not_emit_stale_events(self):
        """被超时放弃的会话不许再搅乱状态（实测过：1.3s 放弃、2.7s 录音又自己回了 idle）。"""
        from capsplus import voice_vad as V

        srv = W.Server(params=V.VadParams(), review_ui=False)
        events = []
        srv.broadcast = events.append
        session = srv._ensure_session()
        session._set_state("recording")
        session.abandon("测试放弃")
        session._set_state("decoding", note="迟到的回调")
        self.assertEqual(session.state, "idle", "discarded 的会话还能改状态 = 按键状态会乱跳")
        self.assertNotIn("decoding", [e.get("state") for e in events if e.get("kind") == "state"])


class TestRecordingStateBroadcast(unittest.TestCase):
    """录音期的状态要能广播出去（弹窗默认不开，指示只能从这条通道拿）。"""

    def setUp(self):
        self.srv = W.Server(review_ui=False)
        self.srv.broadcast_live = True

    def test_broadcast_reaches_a_registered_watcher(self):
        a, b = socket.socketpair()
        with a, b:
            self.srv.add_watcher(b)
            self.srv.broadcast(P.state_event("recording", note="开始采集"))
            frames = P.read_frames(a, limit=1, timeout=1.0)
        self.assertEqual(frames[0]["state"], "recording")

    def test_dead_watcher_is_pruned_instead_of_raising(self):
        a, b = socket.socketpair()
        b.close()
        with a:
            self.srv.add_watcher(a)
            self.srv.broadcast(P.state_event("recording"))          # 写失败被吞掉
            self.srv.broadcast(P.state_event("decoding"))           # 第二次不该再撞上它
            self.assertEqual(self.srv.watchers, [], "写失败的连接没摘掉 = 每次广播都撞异常")

    def test_broadcast_with_no_listener_is_silent_not_fatal(self):
        self.srv.broadcast(P.state_event("idle"))                   # 不许抛

    def test_state_events_leave_the_session_only_through_set_state(self):
        """状态帧只许从 `_set_state` 出去（`error` / `text` 帧本来就该由具体环节发）。

        这条锁的是"状态与事件会漂"：谁想发个 `recording` 就自己 `broadcast` 一次，
        历史、日志、`wait_done` 与状态帧就会各说各话。
        """
        import inspect

        for name, meth in inspect.getmembers(W.Session, inspect.isfunction):
            if name == "_set_state":
                continue
            src = inspect.getsource(meth)
            self.assertNotIn("state_event(", src,
                             f"{name}() 绕过 _set_state 自己发状态帧")
        self.assertEqual(inspect.getsource(W.Session._set_state).count("P.state_event("), 1)


class TestWatchdogRule(unittest.TestCase):
    """整链路兜底时限**不许**咬到正在弹窗里改字的用户（改完按 Enter 却被丢 = 静默丢文本）。

    时限本身是 `W.MAX_SESSION_S`，用例一律**相对它**写，别再抄一个"100 秒"进来
    （2026-10-20 提高这个值时，抄死的数字当场变红 —— 那次红得对，但红的是用例不是实现）。
    """

    class Fake:
        def __init__(self, state, done=False, age=100.0):
            self.state, self._done = state, done
            self.started_at = 1000.0
            self._age = age

        def done(self):
            return self._done

    def test_stuck_recording_session_is_abandoned(self):
        fake = self.Fake("recording")
        self.assertTrue(W.should_abandon(fake, now=fake.started_at + W.MAX_SESSION_S + 1.0),
                        "卡住的会话必须被丢掉，否则 socket 再也不接单")

    def test_limit_leaves_room_for_a_full_length_recording(self):
        """兜底时限必须**大于**一次录音的上限 + 一次识别的余量。

        60s 录音（`voice_vad.MAX_MS`）刚落地时，90s 的兜底只剩 30s 给识别 + 起进程；
        真机一次慢识别就够把还在弹窗里等人的会话判死。这条把两个数字绑在一起：
        谁再改其中一个，这里会先红。
        """
        from capsplus import voice_vad as V

        self.assertGreaterEqual(W.MAX_SESSION_S, V.MAX_MS / 1000.0 + 60.0,
                                "兜底时限要留够「录满上限 + 识别 + 人的反应时间」")

    def test_review_is_never_abandoned_however_old(self):
        fake = self.Fake("review")
        self.assertFalse(W.should_abandon(fake, now=fake.started_at + 10 ** 6),
                         "用户还在弹窗里改字，超时不许把会话丢掉")

    def test_finished_session_is_not_abandoned(self):
        fake = self.Fake("idle", done=True)
        self.assertFalse(W.should_abandon(fake, now=fake.started_at + 100.0))

    def test_inside_the_limit_it_waits(self):
        fake = self.Fake("decoding")
        self.assertFalse(W.should_abandon(fake, now=fake.started_at + 10.0))
        self.assertFalse(W.should_abandon(fake, now=fake.started_at + W.MAX_SESSION_S - 1.0),
                         "差一秒还不该动手")


class TestGuardHasTeeth(unittest.TestCase):
    """每条护栏都造一份反例，证明它抓得住东西（判据抓不住 = 没有判据）。"""

    def test_parse_frame_does_not_silently_accept_a_spoofed_state(self):
        self.assertEqual(P.parse_frame('{"kind":"state","state":"rce"}')["state"], "rce",
                         "解析层不校验 state 值是**有意的**（服务端才管状态机）—— "
                         "所以这条判据锁的是「服务端只发白名单值」，见下一个用例")

    def test_state_event_refers_unknown_state(self):
        with self.assertRaises(ValueError):
            P.state_event("rce")

    def test_stale_detector_would_miss_a_live_listener(self):
        """反例具型：`remove_stale` 不许把**正在用**的 socket 删掉（那等于踢掉活着的 worker）。"""
        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / "voice.sock"
            with mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": tmp}):
                server = P.bind_server(sock)
                try:
                    self.assertFalse(P.remove_stale(sock), "把活 worker 的 socket 删了 = 单实例语义坏掉")
                    self.assertTrue(sock.exists())
                finally:
                    P.release_server(server, sock)
                self.assertTrue(P.remove_stale(sock) or not sock.exists())

    def test_toggle_plan_is_not_hardcoded_to_start(self):
        """反例具型：如果 toggle_plan 退化成"永远 start"，第二次按键会开出两段录音。"""
        self.assertNotEqual(P.toggle_plan("recording"), P.toggle_plan("idle"))

    def test_end_to_end_actually_depends_on_the_gate(self):
        """反例具型：把门降成 0，同一段"静音"就会被判成有语音 ⇒ 上面那条判据不是摆设。"""
        from capsplus import voice_vad as V

        loose = V.VadParams(gate_floor=0.0, gate_multiple=0.0)
        self.assertTrue(V.analyze(quiet_pcm(1000), loose).has_speech,
                        "门限为 0 还能判「没说话」= 能量门根本没参与判定")


if __name__ == "__main__":
    unittest.main(verbosity=2)
