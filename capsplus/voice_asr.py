# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""识别客户端：PCM 拼 WAV 头 → POST 本机 voice-svc → 结构化结果。

**本模块不做识别**，只对着已在跑的 voice-svc 说话。契约（读 `~/voice-svc/daemon.py` 源码核对，
2026-10-17）：

    POST http://127.0.0.1:18090/asr-upload?lang=zh   body = 原始音频字节
         -> 200 {"ok":true,"text":"…","ms":N}
         -> 200 {"ok":false,"error":"…"}（`bad-size` / 解码失败都走这条，状态码仍是 200）
         -> 404 {"ok":false,"error":"not-found"} / 500 {"ok":false,"error":"…"}
    GET  http://127.0.0.1:18090/health               -> {"ok":true,"model_loaded":true,…}

两条实测到的事实决定了这里的写法：
  1. daemon 里 **`raw[:4] == b'RIFF'` 就直接当 wav 吃**，不叫 ffmpeg
     （本机 ffmpeg 未装，2026-10-17 `command -v ffmpeg` 为空）⇒ 我们自己拼 WAV 头，
     用标准库 `wave`，**不引第三方**。
  2. `2026-10-17 实测 :18090 拒连`（`curl` → Connection refused，unit `voice-svc.service` 是 disabled）
     ⇒ 服务不在这台机上跑是**日常状态**，所以这里必须给出"下一步敲什么"的提示，
     而不是把 `URLError` 甩给调用方。

覆盖口子：`CAPSPLUS_ASR_HOST` / `CAPSPLUS_ASR_PORT` / `CAPSPLUS_ASR_LANG`
（测试把端口指到本机临时 stub，CI 不需要真服务）。
"""

from __future__ import annotations

import io
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18090
UPLOAD_PATH = "/asr-upload"
HEALTH_PATH = "/health"
DEFAULT_LANG = "zh"

RECOGNIZE_TIMEOUT_S = 20.0      # 本机实测 5.5s 音频识别约 0.3s，20s 是"服务在解码但很慢"的上限
HEALTH_TIMEOUT_S = 1.5
MAX_BODY_BYTES = 8 * 1024 * 1024   # daemon 侧硬上限：超过它直接回 bad-size

# ── 诊断留证：把最近送识别的 WAV 留一份（默认**关**）──────────────────────────
# 为什么要有（2026-10-20）：明江抱怨"识别不准"时，我们没有音频可复盘 ——
# 能看的只有 `voice.log` 里那行状态（`noise_rms=… gate=…`），它连"当时说了什么"都不该有。
# 于是每次只能靠复述现象去猜。开了这个开关，最近的音频就躺在状态目录里，直接拿去服务上重跑。
# 默认关的理由：这是**用户说的话**，落盘要用户自己点头（0o600，只给本人读）。
KEEP_LAST_ENV = "CAPSPLUS_VOICE_KEEP_LAST"
KEEP_LAST_LIMIT = 5            # 留几份。60s 一段 = 32KB/s × 60 = 1.9MB，5 份上界约 10MB
KEEP_LAST_GLOB = "voice-last-*.wav"

# 错误码 → 人话下一步（照 capsplus/extcheck.py 的 HINTS 口径：状态必须带动作，不让人再翻文档）
HINTS = {
    "unreachable": "识别服务没在跑：`systemctl --user start voice-svc`，"
                   "再 `curl -s http://127.0.0.1:{p}/health` 应回 \"ok\":true；"
                   "unit 不存在就前台跑 `python3 ~/voice-svc/daemon.py` 看它报什么",
    "timeout": "等识别回音超时（{t}s）：服务在但卡住了 —— "
               "`systemctl --user restart voice-svc`，看 `~/.dsh/logs/voice-svc.log`",
    "http_error": "服务答了个 {code}：路径/版本对不上（期望 POST {path}?lang=zh）",
    "bad_json": "服务答的不是 JSON：`curl -sv http://{host}:{p}{path}` 看一眼真实返回",
    "svc_error": "服务自己报了错（见 message）；模型/依赖没装好时最常见，"
                 "看 `~/.dsh/logs/voice-svc.log` 里那行的 traceback",
    "bad_size": "音频字节数服务不收（空或超 {max} MiB）：缩短录音或检查采样率",
    "empty": "识别结果为空：多半是没说话/声音太小 —— 离麦克风近一点再说一遍",
    "no_pcm": "没有可送的数据：录音那步就没成",
}


@dataclass
class AsrResult:
    """一次识别的结论。**调用方只看这个，不用 catch 异常**。"""
    ok: bool = False
    text: str = ""
    ms: int = 0
    code: str = ""          # 见 HINTS 的键；ok=True 时为 ""
    message: str = ""
    hint: str = ""
    body_bytes: int = 0
    terms: int = 0          # 术语替换表命中了几条（0 = 一个字没动）

    def describe(self) -> str:
        """一行诊断（进日志用，**不含识别出来的文本**）。"""
        if self.ok:
            return f"ok={self.ms}ms chars={len(self.text)} terms={self.terms}"
        return f"code={self.code or '?'} message={self.message[:120]}"


def host() -> str:
    return os.environ.get("CAPSPLUS_ASR_HOST") or DEFAULT_HOST


def port() -> int:
    for var in ("CAPSPLUS_ASR_PORT", "VOICE_SVC_PORT"):     # 后者是 daemon 自己的变量，跟着它走
        value = os.environ.get(var)
        if value:
            try:
                return int(value)
            except ValueError:
                pass
    return DEFAULT_PORT


def lang() -> str:
    return os.environ.get("CAPSPLUS_ASR_LANG") or DEFAULT_LANG


def _hint(name: str, **kw) -> str:
    kw.setdefault("p", port())
    kw.setdefault("host", host())
    kw.setdefault("t", int(RECOGNIZE_TIMEOUT_S))
    kw.setdefault("max", MAX_BODY_BYTES // (1024 * 1024))
    kw.setdefault("path", UPLOAD_PATH)
    kw.setdefault("code", "")
    try:
        return HINTS.get(name, "").format(**kw)
    except (KeyError, IndexError):
        return HINTS.get(name, "")


def pcm_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1,
               sample_width: int = 2) -> bytes:
    """裸 PCM(s16le) → 完整 WAV 字节。标准库 `wave`，头部 44 字节那种。

    为什么必须拼头：daemon 认 `RIFF` 就直接吃，不认就去找 ffmpeg，而本机**没装 ffmpeg**。
    """
    if sample_width != 2:
        raise ValueError("只支持 s16le（sample_width=2）")
    if len(pcm) % 2:
        pcm = pcm[:-1]              # 半个采样：丢掉尾巴，别让 wave 抛"样本长度不是整数倍"
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def keep_last_enabled() -> bool:
    """`CAPSPLUS_VOICE_KEEP_LAST=1` 才落盘。别的值一律按关（写文件这种事不许"猜着开"）。"""
    return os.environ.get(KEEP_LAST_ENV) == "1"


def keep_last_wav(wav: bytes) -> str:
    """把这次送识别的 WAV 留一份，返回落点（关着 / 写不进去时返回 ""）。

    文件名带**纳秒尾数**，按名字排就是时间序 —— **清理只认这个模式**（`voice-last-*.wav`），
    绝不碰状态目录里别的东西。超过 `KEEP_LAST_LIMIT` 份就把最旧的删掉：
    这是个**上限**而不是"攒着看历史"，攒多了一个是占盘，一个是留了一堆用户说过的话。
    写失败只返回 ""：诊断留证失败不许影响识别本身。

    时间戳只读**一次钟**（`time_ns`），再用它同时算"秒"和"纳秒尾数"：
    早先分两次读（`strftime()` 一次、`time_ns()` 一次），跨秒那一瞬间会拼出
    `…-190748-995971` 和 `…-190748-089322` 这种**名字顺序与时序相反**的一对，
    清理按最旧的删就删错文件了（2026-10-20 GUI 口径跑出来的真 bug）。
    """
    from .voice_proto import state_dir

    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        now_ns = time.time_ns()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ns / 1_000_000_000))
        path = directory / f"voice-last-{stamp}-{now_ns % 1_000_000_000:09d}.wav"
        path.write_bytes(wav)
        path.chmod(0o600)                    # 用户说的话：只给本人读
        for old in sorted(directory.glob(KEEP_LAST_GLOB))[:-KEEP_LAST_LIMIT]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(path)
    except OSError:
        return ""


def read_audio_file(path: str) -> bytes:
    """读一个音频文件 → 裸 PCM。`RIFF` 开头就按 WAV 解（`wave` 读头，不写死 44 字节）。

    给 `capsplus-voice meter` 的对照口与 `KEEP_LAST` 留下的 wav 复盘用。
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:4] != b"RIFF":
        return data
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.readframes(w.getnframes())


def _request(url: str, body: bytes | None = None, timeout: float = RECOGNIZE_TIMEOUT_S):
    """发一次 HTTP 请求，返回 (状态码, 文本) 或抛 OSError/URLError 由调用方归码。"""
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/octet-stream" if body is not None else "*/*")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def recognize(pcm: bytes, *, sample_rate: int = 16000, lang_code: str | None = None,
              timeout: float = RECOGNIZE_TIMEOUT_S, wav_bytes: bytes | None = None) -> AsrResult:
    """把 PCM 送去识别。任何失败都返回带 `code`+`hint` 的结果，**不抛异常**。"""
    if not pcm and wav_bytes is None:
        return AsrResult(code="no_pcm", message="没有 PCM", hint=_hint("no_pcm"))
    wav = wav_bytes if wav_bytes is not None else pcm_to_wav(pcm, sample_rate)
    if keep_last_enabled():
        keep_last_wav(wav)                  # 诊断留证：默认不做，失败也不影响识别
    if len(wav) > MAX_BODY_BYTES:
        return AsrResult(code="bad_size", message=f"{len(wav)} 字节超过服务上限",
                         hint=_hint("bad_size"), body_bytes=len(wav))
    url = (f"http://{host()}:{port()}{UPLOAD_PATH}"
           f"?lang={urllib.parse.quote(lang_code or lang())}")
    try:
        status, text = _request(url, wav, timeout)
    except urllib.error.HTTPError as exc:            # 4xx/5xx：服务在，但不是 200
        body = ""
        try:
            with exc:                                # HTTPError 自己是个响应对象：读完要关
                body = exc.read().decode("utf-8", "replace")
        except Exception:                            # noqa: BLE001
            pass
        parsed = _try_json(body)
        if exc.code == 404:
            # 404 = 路径/接口对不上，**不是**服务内部的错：报 http_error 才会去核对契约
            return AsrResult(code="http_error", message=f"HTTP 404 {str(parsed)[:120]}",
                             hint=_hint("http_error", code=exc.code), body_bytes=len(wav))
        if parsed is not None and not parsed.get("ok", False):
            return AsrResult(code="svc_error", message=str(parsed.get("error", ""))[:200],
                             hint=_hint("svc_error"), body_bytes=len(wav))
        return AsrResult(code="http_error", message=f"HTTP {exc.code}",
                         hint=_hint("http_error", code=exc.code), body_bytes=len(wav))
    except (urllib.error.URLError, OSError) as exc:  # 连不上 / 超时（URLError 是 OSError 的子路）
        reason = getattr(exc, "reason", exc)
        code = "timeout" if isinstance(reason, socket.timeout) else "unreachable"
        return AsrResult(code=code, message=f"{type(exc).__name__}: {reason}",
                         hint=_hint(code), body_bytes=len(wav))
    except socket.timeout:
        return AsrResult(code="timeout", message=f"{int(timeout)}s 超时",
                         hint=_hint("timeout"), body_bytes=len(wav))

    if status != 200:
        return AsrResult(code="http_error", message=f"HTTP {status}",
                         hint=_hint("http_error", code=status), body_bytes=len(wav))
    data = _try_json(text)
    if data is None:
        return AsrResult(code="bad_json", message=text[:200], hint=_hint("bad_json"),
                         body_bytes=len(wav))
    if not data.get("ok", False):
        code = str(data.get("error", "") or "svc_error")
        if code == "bad-size":
            code = "bad_size"
        return AsrResult(code=code if code in HINTS else "svc_error",
                         message=str(data.get("error", ""))[:200],
                         hint=_hint(code if code in HINTS else "svc_error"),
                         body_bytes=len(wav))
    got = str(data.get("text", "")).strip()
    if not got:
        return AsrResult(code="empty", message="服务答了空串", hint=_hint("empty"),
                         body_bytes=len(wav))
    got, terms = _apply_glossary(got)
    return AsrResult(ok=True, text=got, ms=int(data.get("ms", 0) or 0),
                     body_bytes=len(wav), terms=terms)


def _apply_glossary(text: str) -> tuple[str, int]:
    """术语替换表这一层（规则与理由见 `voice_glossary`）。**它坏了也不许让识别失败** ——
    表是数据、用户会自己改，改错表的代价必须是"这次没替换"，不是"这次没识别"。"""
    try:
        from . import voice_glossary as G
        fixed, hits = G.apply(text)
        return fixed, len(hits)
    except Exception:                                 # noqa: BLE001
        return text, 0


def _try_json(text: str) -> dict | None:
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def health(timeout: float = HEALTH_TIMEOUT_S) -> AsrResult:
    """`GET /health` 的体检。给 `capsplus-voice status` 用，同样不抛。"""
    url = f"http://{host()}:{port()}{HEALTH_PATH}"
    try:
        status, text = _request(url, None, timeout)
    except (urllib.error.URLError, OSError, socket.timeout) as exc:
        reason = getattr(exc, "reason", exc)
        code = "timeout" if isinstance(reason, socket.timeout) else "unreachable"
        return AsrResult(code=code, message=f"{type(exc).__name__}: {reason}", hint=_hint(code))
    data = _try_json(text)
    if status != 200 or data is None:
        return AsrResult(code="http_error" if status != 200 else "bad_json",
                         message=f"HTTP {status}" if status != 200 else text[:120],
                         hint=_hint("bad_json"))
    if not data.get("ok", False):
        return AsrResult(code="svc_error", message=str(data.get("error", ""))[:200],
                         hint=_hint("svc_error"))
    return AsrResult(ok=True, text=f"model_loaded={data.get('model_loaded')} "
                                   f"uptime_s={data.get('uptime_s')}")
