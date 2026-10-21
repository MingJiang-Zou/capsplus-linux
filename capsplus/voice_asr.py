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

**服务在哪现在是可配的**（2026-10-21，用户原话：「关于 capsplus 工具，我希望关于翻译 OCR 语音识别
的服务都是可以配置的，方便配置远程的服务或者是本地的」）：落点从
`~/.config/capsplus/services.toml` 的 `[asr]` 段读（底座是 `capsplus/services.py`，
**本模块只读不改**）：

    [asr]
    lang = "zh"
    timeout_s = 20

    [asr.local]
    base_url = "http://127.0.0.1:18090"     # 或 host = "…" / port = 18090
    # 远端就换成另一台机器的地址，例如：
    # base_url = "http://192.0.2.10:18090"

    优先级（全仓口径）：**环境变量 > 配置文件 > 内置默认**。
    环境变量那三个**保留**（现场救火用），而且**压过**配置文件 ——
    认它们的人（脚本、临时排查）不该被一份长期配置挡住。
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

try:                                    # 包内导入（正常路径）
    from . import services as S
except ImportError:                     # 直接当脚本跑（`python3 capsplus/voice_asr.py`）
    import services as S

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18090
UPLOAD_PATH = "/asr-upload"
HEALTH_PATH = "/health"
DEFAULT_LANG = "zh"

RECOGNIZE_TIMEOUT_S = 20.0      # 本机实测 5.5s 音频识别约 0.3s，20s 是"服务在解码但很慢"的上限
HEALTH_TIMEOUT_S = 1.5
MAX_BODY_BYTES = 8 * 1024 * 1024   # daemon 侧硬上限：超过它直接回 bad-size

# ── 诊断留证：把最近送识别的 WAV 留一份（默认**关**）──────────────────────────
# 为什么要有（2026-10-20）：用户抱怨"识别不准"时，我们没有音频可复盘 ——
# 能看的只有 `voice.log` 里那行状态（`noise_rms=… gate=…`），它连"当时说了什么"都不该有。
# 于是每次只能靠复述现象去猜。开了这个开关，最近的音频就躺在状态目录里，直接拿去服务上重跑。
# 默认关的理由：这是**用户说的话**，落盘要用户自己点头（0o600，只给本人读）。
KEEP_LAST_ENV = "CAPSPLUS_VOICE_KEEP_LAST"
KEEP_LAST_LIMIT = 5            # 留几份。60s 一段 = 32KB/s × 60 = 1.9MB，5 份上界约 10MB
KEEP_LAST_GLOB = "voice-last-*.wav"

# 错误码 → 人话下一步（照 capsplus/extcheck.py 的 HINTS 口径：状态必须带动作，不让人再翻文档）
HINTS = {
    "unreachable": "识别服务没在跑：`systemctl --user start voice-svc`，"
                   "再 `curl -s {url}/health` 应回 \"ok\":true；"
                   "unit 不存在就前台跑 `python3 ~/voice-svc/daemon.py` 看它报什么",
    "timeout": "等识别回音超时（{t}s）：服务在但卡住了 —— "
               "`systemctl --user restart voice-svc`（{url}），看 `~/.dsh/logs/voice-svc.log`",
    "http_error": "服务答了个 {code}：路径/版本对不上（期望 POST {path}?lang=zh）",
    "bad_json": "服务答的不是 JSON：`curl -sv {url}{path}` 看一眼真实返回",
    "svc_error": "服务自己报了错（见 message）；模型/依赖没装好时最常见，"
                 "看 `~/.dsh/logs/voice-svc.log` 里那行的 traceback",
    "bad_size": "音频字节数服务不收（空或超 {max} MiB）：缩短录音或检查采样率",
    "empty": "识别结果为空：多半是没说话/声音太小 —— 离麦克风近一点再说一遍",
    "no_pcm": "没有可送的数据：录音那步就没成",
}

# **远程**识别服务的话术（2026-10-21 新增）：服务在别的机器上时，
# "在你这台机上 systemctl --user start" 是**错的** —— 那台机上的 unit 得在那台机起。
# 所以远程另有一套话：说清"在哪台机器上做什么" + 网络这一层也要看。
REMOTE_HINTS = {
    "unreachable": "连不上远端识别服务 {url}：那台机器上的服务没在跑 —— "
                   "在**那台机器**上 `systemctl --user start voice-svc`；"
                   "再 `curl -sv {url}/health` 应回 \"ok\":true（顺带看防火墙与网络通不通）",
    "timeout": "等远端识别回音超时（{t}s）：服务在但卡住了 —— 在那台机器上 "
               "`systemctl --user restart voice-svc`（{url}），看 `~/.dsh/logs/voice-svc.log`",
}

# ── 服务配置（2026-10-21 新增）────────────────────────────────────────────
# 底座是 `capsplus/services.py`（Lead 写的那份，**本模块只读不改**）。
# 三个客户端各留一份薄封装（不新建共享模块）：`services.py` 是别人写的地基，不该由我改；
# 而新建共享模块就得动仓的结构，那要先问人。
CONFIG_ENV = "CAPSPLUS_SERVICES_FILE"      # 指到别的 services.toml（多套配置 / 判据用）
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def services_config_path(path=None) -> str:
    """服务配置文件的落点：入参 > `CAPSPLUS_SERVICES_FILE` > 默认落点。

    为什么给环境变量留这个口子：判据要拿**注入的 fixture 文件**跑（不许碰用户真配的那份）。
    """
    return path or os.environ.get(CONFIG_ENV) or str(S.SERVICES_PATH)


def load_config(path=None):
    """读服务配置。**读不了 / 语法错都不抛栈** —— `services.load_services()` 自己兜住了。"""
    return S.load_services(services_config_path(path))


def asr_endpoint(cfg=None):
    """`[asr]` 段里那个端点（按 `mode` 选；mode=auto 时取第一个有地址的）。"""
    cap = (cfg if cfg is not None else load_config()).get("asr")
    if cap is None:
        return None
    ep = cap.endpoint()
    if ep is not None and (ep.base_url or ep.extra.get("host")):
        return ep
    for candidate in cap.ordered_endpoints():
        if candidate.base_url or candidate.extra.get("host"):
            return candidate
    return ep


def _config_base_url(cfg=None) -> str:
    """服务配置里 `[asr]` 那一段的地址（没配就是空串）。

    除了 `base_url`，也认 `host` + `port` 两种写法（那两键在 `services.py` 里进 `extra`）。
    """
    ep = asr_endpoint(cfg)
    if ep is None:
        return ""
    if ep.base_url:
        return ep.base_url.strip().rstrip("/")
    host_part = str(ep.extra.get("host") or "").strip()
    if host_part:
        port_part = str(ep.extra.get("port") or DEFAULT_PORT).strip()
        return f"http://{host_part}:{port_part}"
    return ""


def _env_port() -> int:
    """环境变量里的端口（`CAPSPLUS_ASR_PORT` → `VOICE_SVC_PORT`）；没有就 0。"""
    for var in ("CAPSPLUS_ASR_PORT", "VOICE_SVC_PORT"):     # 后者是 daemon 自己的变量，跟着它走
        value = os.environ.get(var)
        if value:
            try:
                return int(value)
            except ValueError:
                pass
    return 0


def _url_host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _url_port(url: str) -> int:
    try:
        return urllib.parse.urlsplit(url).port or 0
    except ValueError:
        return 0


def _pick(env_value: str, config_value: str, default: str) -> str:
    """「谁说了算」的**优先级本体**：环境变量 > 配置文件 > 内置默认。

    单独抽成一个函数，是为了让判据能**反例具型**：把这里的顺序反过来，判据必须红
    （见 `tests/test_voice_asr.py::TestCounterExamples`）。
    """
    return env_value or config_value or default


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
    """识别服务的主机名（**环境变量 > 服务配置 > 内置默认**）。"""
    env = (os.environ.get("CAPSPLUS_ASR_HOST") or "").strip()
    if env:
        return env
    return _url_host(_config_base_url()) or DEFAULT_HOST


def port() -> int:
    """识别服务的端口（**环境变量 > 服务配置 > 内置默认**）。

    `VOICE_SVC_PORT` 也认 —— 那是 daemon 自己的变量，跟着它走（老口径，2026-10-17 起就有）。
    """
    value = _env_port()
    if value:
        return value
    return _url_port(_config_base_url()) or DEFAULT_PORT


def lang() -> str:
    """识别语言（**环境变量 > 服务配置 > 内置默认**）。"""
    cap = load_config().get("asr")
    config_value = str(cap.lang if cap else "").strip()
    env = (os.environ.get("CAPSPLUS_ASR_LANG") or "").strip()
    return _pick(env, config_value, DEFAULT_LANG)


def base_url() -> str:
    """识别服务的落点（协议 + 主机 + 端口 + 可选路径）。

    优先级（全仓口径）：**环境变量 > 配置文件 > 内置默认**。
      · 环境变量 `CAPSPLUS_ASR_HOST` / `CAPSPLUS_ASR_PORT`（`VOICE_SVC_PORT` 也认）——
        现场救火用；一旦设了，按老口径拼成 `http://{host}:{port}`；
      · 配置文件 `[asr]` 那一段的 `base_url`（可以带 https 与路径，
        比如 `https://asr.example.com/asr`）；
      · 内置默认 `http://127.0.0.1:18090`。
    """
    env_host = (os.environ.get("CAPSPLUS_ASR_HOST") or "").strip()
    env_port = _env_port()
    cfg = _config_base_url()
    if env_host or env_port:
        # 主机与端口各自按"环境变量 > 配置 > 默认"来（`_pick` 是这条优先级本体）
        host_part = _pick(env_host, _url_host(cfg), DEFAULT_HOST)
        port_part = _pick(str(env_port) if env_port else "",
                          str(_url_port(cfg) or ""), str(DEFAULT_PORT))
        return f"http://{host_part}:{port_part}"
    return (cfg or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}").rstrip("/")


def recognize_timeout(cfg=None) -> float:
    """识别超时（秒）：**服务配置里的 `timeout_s` > 内置默认**。

    为什么可配（2026-10-21）：20s 是**本机那台**的数（实测 5.5s 音频约 0.3s）；
    远程那台机器慢一点很正常（网络 + 那边自己在忙），一刀切 20s 会把"慢"报成"坏了"。
    """
    cap = (cfg if cfg is not None else load_config()).get("asr")
    value = float(cap.timeout_s or 0) if cap else 0.0
    if not value:
        ep = asr_endpoint(cfg)
        value = float(ep.timeout_s or 0) if ep is not None else 0.0
    return value if value > 0 else RECOGNIZE_TIMEOUT_S


def is_remote() -> bool:
    """目标是不是**别的机器**（远程识别服务）—— 话术分两套的原因。"""
    return host().strip().lower() not in LOCAL_HOSTS


def _hint(name: str, **kw) -> str:
    """错误码 → 下一步。**远程目标用另一套话**（在别的机器上，命令得在那台机器敲）。"""
    kw.setdefault("p", port())
    kw.setdefault("host", host())
    kw.setdefault("t", int(recognize_timeout()))
    kw.setdefault("max", MAX_BODY_BYTES // (1024 * 1024))
    kw.setdefault("path", UPLOAD_PATH)
    kw.setdefault("code", "")
    kw.setdefault("url", base_url())
    table = REMOTE_HINTS if (is_remote() and name in REMOTE_HINTS) else HINTS
    try:
        return table.get(name, "").format(**kw)
    except (KeyError, IndexError):
        return table.get(name, "")


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
    url = (f"{base_url()}{UPLOAD_PATH}"
           f"?lang={urllib.parse.quote(lang_code or lang())}")
    try:
        status, text = _request(url, wav,
                                timeout if timeout is not None else recognize_timeout())
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
    url = f"{base_url()}{HEALTH_PATH}"
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
