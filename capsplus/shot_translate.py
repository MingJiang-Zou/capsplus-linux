# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""翻译（第二批 F2）：**默认走本机模型**，一键切云。

两条后端（都是 OpenAI 兼容的 `/chat/completions`，差别只在"谁翻译"和"能不能读图"）：
  * `local` —— 本机 `http://127.0.0.1:12087/v1`（provider 名 bonsai27b，模型 `qwen3.8-27b`）。
    它 **只吃文本**（`/v1/models` 里 `"modalities":{"vision":false}`，2026-10-19 实测）
    ⇒ 本地链路必须是 **本地 OCR → 本地翻译**，图不能直接喂给它。
  * `cloud` —— 阿里 token-plan（`deepseek-v4.1-flash`，**多模态**）：可以让它**直接读图**，
    连 OCR 都省掉（`translate_image()`）。凭据从 `~/.bailian/config.json` 读
    （`bl auth login` 的落点，2026-10-19 实测：`token-plan.api_key` / `token-plan.base_url`），
    也可以用 `CAPSPLUS_CLOUD_*` 环境变量覆盖。**值一律不打印、不落文档。**

后端选择（`resolve_backend()` 是纯函数）：`CAPSPLUS_TRANSLATE=local|cloud|auto`（默认 auto）。
`auto` = 本机端点探得到就用本机（离线、不花他的钱），否则用云；两边都不行 → 给**可操作**的话。

返回给面板的 dict 与 OCR 同一套约定（`shot_annotate.normalize_result`）：
  `{"ok", "src", "text", "backend", "ms", "note", "hint"}`
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.request
from pathlib import Path

BACKEND_LOCAL = "local"
BACKEND_CLOUD = "cloud"
BACKENDS = (BACKEND_LOCAL, BACKEND_CLOUD)

MODE_ENV = "CAPSPLUS_TRANSLATE"
LOCAL_URL_ENV = "CAPSPLUS_TRANSLATE_URL"
LOCAL_MODEL_ENV = "CAPSPLUS_TRANSLATE_MODEL"
CLOUD_CONFIG_ENV = "CAPSPLUS_BAILIAN_CONFIG"

DEFAULT_LOCAL_URL = "http://127.0.0.1:12087/v1"
DEFAULT_LOCAL_MODEL = "qwen3.8-27b"
DEFAULT_CLOUD_MODEL = "deepseek-v4.1-flash"
DEFAULT_CLOUD_CONFIG = "~/.bailian/config.json"
LOCAL_KEY = "not-needed"          # 本机端点不收 key；给个占位免得有些实现在这上面挑刺

# ── 超时与 token 上限：都是**实测**定的（2026-10-20，本机 llama.cpp `qwen3.8-27b`）──
# 服务端自己的计时：一次翻译 = prompt eval 0.04–0.21 s + 生成 0.39 s（54 tok/s，22 个 token）
# ⇒ 墙钟 0.43 s（缓存命中）/ 0.78 s（冷）。原来写的 60 s 是**被队排挤爆过**的：服务端串行处理请求，
# 同一台机另一个会话正在用模型时，同一条 3-token 请求实测墙钟 **63.7 s**（服务端只花 0.39 s），
# 于是 60 s 超时把"在排队"误报成"本机模型没应答"。所以：超时给足余量（排队归排队，别误判端点死了），
# 真正 runaway 靠 max_tokens 兜住（下面 TIME_* 那条注释写了怎么区分两者）。
TIMEOUT_S = 120
TIMEOUT_ENV = "CAPSPLUS_TRANSLATE_TIMEOUT"
TEXT_MAX_TOKENS = 1024            # 一段译文远用不到；给推理模型留出"思考完还能出正文"的余量
IMAGE_MAX_TOKENS = 4096           # 直读图那条要吐「原文：…译文：…」两段
PROBE_MAX_TOKENS = 8              # --translate-status 的真往返：只让它出几个字
PROBE_TIMEOUT_S = 6               # 探活那次往返等多久（诊断要秒回，不跟排队耗）
SYSTEM_PROMPT = ("你是翻译引擎。把用户给的文字翻译成目标语言，只输出译文本身，"
                 "不要解释、不要加引号、不要重复原文。")

# **关思考**（2026-10-20 实测，两台都是推理模型）：
# `chat_template_kwargs: {"enable_thinking": false}` 对**本机 llama.cpp** 与**阿里 token-plan**
# 都成立 —— 本机：reasoning_content 从 134 字变空、22 token 出完整译文（不关思考时 32 个 token
# 全花在 reasoning_content 里、content 是空串、finish_reason=length）；云端 deepseek-v4.1-flash：
# reasoning_tokens 149→0、completion_tokens 159→9、2.73 s→0.83 s。
# ⚠ 提示词里加 `/no_think` **不管用**（实测：照样把 32 个 token 全用在思考上、content 仍为空）
#   —— 那是模板里的开关，不是提示词能触发的，别再照抄那条。
NO_THINK_KWARG = {"enable_thinking": False}
try:                                            # 云那条链路用 requests（有就认它的异常）
    from requests.exceptions import HTTPError as HTTPStatusError
except Exception:                               # pragma: no cover - 没装 requests 也能跑
    class HTTPStatusError(Exception):
        pass


IMAGE_PROMPT = ("识别这张截图里的全部文字，然后把它翻译成目标语言。"
                "输出格式：先一行「原文：」+识别到的原文，再一行「译文：」+译文。")


# ------------------------------------------------------------------ 纯逻辑（判据直接调）

DEFAULT_MODE = BACKEND_LOCAL


def mode() -> str:
    """当前后端档位；**默认 `local`**（2026-10-19 明江定的口径）。

    他问"翻译用的是哪个云服务？最好用免费的" —— `cloud` 那条是**按 token 计费的 LLM API**，
    不是免费翻译服务，而且那把 key 还 401 了。所以默认永远先走本机（离线、不限次、免费），
    **绝不因为云 key 失效而失败**；`auto`/`cloud` 两档保留，但要他主动切才走。
    """
    value = (os.environ.get(MODE_ENV) or DEFAULT_MODE).strip().lower()
    return value if value in BACKENDS + ("auto",) else DEFAULT_MODE


def local_url() -> str:
    return (os.environ.get(LOCAL_URL_ENV) or DEFAULT_LOCAL_URL).rstrip("/")


def local_model() -> str:
    return os.environ.get(LOCAL_MODEL_ENV) or DEFAULT_LOCAL_MODEL


def target_language() -> str:
    return (os.environ.get("CAPSPLUS_TRANSLATE_TO") or "中文").strip()


# 凭据来源（**三级**，从高到低）。第三级是 DSH 自己的凭据文件 —— 只读、绝不打印、绝不改写。
CREDENTIALS_FILE = "~/.dsh/.credentials.yaml"
CREDENTIALS_REF = "AL_TOKEN_PLAN_API_KEY"
SOURCE_ENV = "环境变量 CAPSPLUS_CLOUD_KEY"
SOURCE_BAILIAN = "~/.bailian/config.json"
SOURCE_DSH = CREDENTIALS_FILE


def read_dsh_credential(path=None, ref: str = CREDENTIALS_REF) -> str:
    """从 `~/.dsh/.credentials.yaml` 的 `refs.<NAME>` 读一把 token（**只读、绝不打印**）。

    为什么自己写这十几行而不引 YAML 库：仓里没有 pyyaml 依赖（本地 OCR 那是另一条独立 venv），
    而这个文件的结构就是"`refs:` 下面一层缩进的键值" —— 用最小的解析器读，少一个依赖少一处风险。
    解析不出来就返回空串（调用方据此报"凭据没找到"，不编造）。
    """
    file = Path(os.path.expanduser(path or os.environ.get("CAPSPLUS_CLOUD_CREDENTIALS")
                                   or CREDENTIALS_FILE))
    try:
        text = file.read_text(encoding="utf-8")       # **只读**：这里没有任何写操作
    except OSError:
        return ""
    in_refs = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_refs = line.split(":", 1)[0].strip() == "refs"
            continue
        if in_refs:
            key, _, value = line.strip().partition(":")
            if key.strip() == ref:
                return value.strip().strip('"').strip("'")
    return ""


def _safe_source(text: str) -> str:
    """只保留"哪来的"这类标签，**绝不带值** —— 任何要往外说的话都过这道闸。"""
    return "".join(ch for ch in text if ch not in "\n\r")[:120]


def cloud_candidates(config_path=None, credentials_path=None) -> list:
    """云端凭据的**候选清单**（按优先级排序，去重；每项与 `cloud_config()` 同形状）。

    为什么要一份清单（2026-10-20 实测）：`~/.bailian/config.json` 里那把 key **实测 401**
    （`Invalid API-key provided`），而 `~/.dsh/.credentials.yaml` 的 `AL_TOKEN_PLAN_API_KEY`
    同一时刻 **HTTP 200 正常出字** —— 优先级高的那把坏了，光"有凭据"是不够的，要能**换下一把**。
    清单里只有"哪来的"标签（`source`），**绝不带值**。
    """
    env_key = os.environ.get("CAPSPLUS_CLOUD_KEY", "")
    env_url = os.environ.get("CAPSPLUS_CLOUD_BASE_URL", "")
    env_model = os.environ.get("CAPSPLUS_CLOUD_MODEL", "")
    path = Path(os.path.expanduser(config_path or os.environ.get(CLOUD_CONFIG_ENV)
                                   or DEFAULT_CLOUD_CONFIG))
    section = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))     # **只读**
        active = data.get("active_config")
        section = (data.get(active) if isinstance(active, str) and isinstance(data.get(active), dict)
                   else data.get("token-plan", {})) or {}
    except (OSError, ValueError):
        section = {}
    # base_url 三级共用一套规则：环境变量优先，其次 `~/.bailian/config.json` 里记的那个
    base_url = normalize_base_url(env_url or section.get("base_url", ""))
    model = env_model or DEFAULT_CLOUD_MODEL
    found = []
    if env_key:
        found.append((env_key, SOURCE_ENV))
    if section.get("api_key"):
        found.append((section["api_key"], SOURCE_BAILIAN))
    dsh_key = read_dsh_credential(credentials_path)
    if dsh_key:
        found.append((dsh_key, SOURCE_DSH))
    out, seen = [], set()
    for key, source in found:
        if key in seen:                     # 两处存的是同一把 key 时只试一次
            continue
        seen.add(key)
        out.append({"base_url": base_url, "api_key": key, "model": model,
                    "config_file": str(path),
                    "credentials_file": str(Path(os.path.expanduser(
                        credentials_path or CREDENTIALS_FILE))),
                    "source": _safe_source(source)})
    if not out:                             # 一把都没有：也要返回一条，好让文案说清"文件里没有凭据"
        out.append({"base_url": base_url, "api_key": "", "model": model,
                    "config_file": str(path),
                    "credentials_file": str(Path(os.path.expanduser(
                        credentials_path or CREDENTIALS_FILE))),
                    "source": ""})
    return out


def cloud_config(config_path=None, credentials_path=None) -> dict:
    """阿里 token-plan 的 base_url / key / 默认模型（**一律不打印值**）—— 清单里的**第一把**。

    凭据顺序（**三级**，从高到低）：
      ① `CAPSPLUS_CLOUD_KEY` 环境变量（临时/CI 用）
      ② `~/.bailian/config.json` 的 `token-plan` 段（`bl` CLI 的文件；**只读，绝不改写**）
      ③ `~/.dsh/.credentials.yaml` 的 `refs.AL_TOKEN_PLAN_API_KEY`（DSH 自己那把，实测可用）

    纪律：只读、绝不打印、绝不写回任何一个凭据文件；`source` 字段只记"哪来的"，不带值。
    返回里带上 `source` 与 `config_file`，面板与报错据此说清"用的是哪一把"。
    **真正发请求时用 `cloud_candidates()` 逐把试**（第一把可能是坏的，见那个函数的注释）。
    """
    return cloud_candidates(config_path, credentials_path)[0]


def normalize_base_url(raw: str) -> str:
    """把「各家给的 base_url」补成 OpenAI 兼容路径。

    实测（2026-10-19）：`~/.bailian/config.json` 里记的是**站点根**
    `https://token-plan.cn-beijing.maas.aliyuncs.com`，直接拼 `/chat/completions` 会 **404**；
    而 DSH 那边的 provider 配置写的是 `…/compatible-mode/v1`。所以：没带兼容路径就补上，
    已经带了（或者本来就是 `…/v1`）就原样用。
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith("/v1") or "compatible-mode" in raw:
        return raw
    return f"{raw}/compatible-mode/v1"


def resolve_backend(local_probe=None, cloud=None) -> tuple:
    """`(backend | None, 说明)`；不可用时说明必须**可操作**。

    `local_probe` 缺省就是**真探一次** `GET /v1/models`。别拿 `lambda: False` 当默认 ——
    那会让直接调它的人（包括我 2026-10-19 自己）得到"本机模型没起"的错结论。
    """
    mode_ = mode()
    cloud = cloud if cloud is not None else cloud_config()
    local_probe = local_probe or local_reachable
    local_ok = False
    if mode_ in ("auto", BACKEND_LOCAL):
        try:
            local_ok = bool(local_probe())
        except Exception:
            local_ok = False
    has_cloud = bool(cloud.get("base_url") and cloud.get("api_key"))

    if mode_ == BACKEND_LOCAL:
        if local_ok:
            return BACKEND_LOCAL, f"本机模型（{local_model()}）"
        # 默认档就是本机：这时**不谈"自动切云"**，只如实说"本机没起、怎么起"
        return None, (f"✗ 本机模型没起：{local_url()} 探不到 —— "
                      f"起来：`ollama serve` 或 systemctl --user start 你那条服务"
                      f"（判活：`curl {local_url()}/models`）"
                      f"；想用云端就按面板上的『切到云』（那是计费 API，不是免费翻译）")
    if mode_ == BACKEND_CLOUD:
        if has_cloud:
            return BACKEND_CLOUD, f"云端（{cloud['model']}，可直读图）"
        return None, (f"✗ 你指定了云翻译，但 {cloud.get('config_file')} 里没有 token-plan 凭据"
                      f"（或设 CAPSPLUS_CLOUD_KEY / CAPSPLUS_CLOUD_BASE_URL）")
    if local_ok:
        return BACKEND_LOCAL, f"本机模型（{local_model()}）"
    if has_cloud:
        return BACKEND_CLOUD, f"云端（{cloud['model']}；本机模型没起，自动走云）"
    return None, (f"✗ 两条翻译都不能用：本机模型没起（`curl {local_url()}/models` 看看），"
                  f"云端也没凭据（{cloud.get('config_file')} 缺 token-plan.api_key，"
                  f"或设 CAPSPLUS_CLOUD_KEY / CAPSPLUS_CLOUD_BASE_URL）")


def build_messages(text: str, *, target=None, image_b64: str = "") -> list:
    """组请求体里的 messages。**文本链路与多模态链路共用**，只在有没有图这里分叉。"""
    target = target or target_language()
    if image_b64:
        content = [{"type": "text", "text": f"{IMAGE_PROMPT}\n目标语言：{target}"},
                   {"type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"}}]
    else:
        content = f"目标语言：{target}\n\n{text}"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content}]


def request_timeout() -> int:
    """超时（秒）：`CAPSPLUS_TRANSLATE_TIMEOUT` 可覆盖，默认 `TIMEOUT_S`（实测定的，见常量注）。"""
    raw = os.environ.get(TIMEOUT_ENV, "")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return TIMEOUT_S
    return value if value > 0 else TIMEOUT_S


def parse_completion(payload) -> tuple:
    """从 OpenAI 兼容响应里取正文。返回 `(ok, text_or_hint)`。

    ⚠ **空 content 必须给明确原因**（2026-10-20 明江真机："翻译没反应"）：推理模型把
    `max_tokens` 全花在 `reasoning_content` 里时，`content` 就是**空串**而 HTTP 仍是 200 ——
    只回一句"没有正文"等于让人猜。这条要认出这种形状并说清怎么修（关思考 / 加大 max_tokens）。
    """
    if isinstance(payload, (bytes, bytearray)):     # HTTP 层有时给的是原始字节
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            return False, f"响应不是 UTF-8：{exc}"
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return False, "响应不是 JSON"
    if not isinstance(payload, dict):
        return False, "响应不是对象"
    if payload.get("error"):
        err = payload["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        return False, f"服务端报错：{message}"
    choices = payload.get("choices") or []
    if not choices:
        return False, "响应里没有 choices"
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):                       # 有些实现回 content 数组
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        finish = choice.get("finish_reason")
        if reasoning:
            return False, (f"content 是空的，{len(reasoning)} 个字的思考全花在 reasoning_content 里"
                           f"（finish_reason={finish}）—— 这是个**推理模型**，"
                           f"要发 chat_template_kwargs={{\"enable_thinking\": false}} 关掉思考，"
                           f"或把 max_tokens 加大到思考之后还有余量；提示词里写 /no_think **不管用**")
        return False, f"响应里没有正文（content 为空，finish_reason={finish}）"
    return True, str(content).strip()


def make_result(ok, *, src="", text="", backend="", ms=0, note="", hint="") -> dict:
    return {"ok": bool(ok), "src": src, "text": text, "backend": backend, "ms": int(ms),
            "note": note, "hint": hint}


def default_post(url, data: bytes, headers: dict, timeout: int = TIMEOUT_S) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def default_get(url, timeout: int = 5) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


# ------------------------------------------------------------------ 两条后端 / 探测

def local_reachable(get=None, timeout: int = 5) -> bool:
    """本机端点活着吗（`GET /v1/models`）—— 判定"本机模型没起"用的就是它。"""
    get = get or default_get
    try:
        payload = json.loads(get(f"{local_url()}/models", timeout).decode("utf-8"))
    except Exception:
        return False
    return bool(payload.get("data"))


def local_vision(get=None) -> bool:
    """本机模型能不能读图（实测 `modalities.vision=false` ⇒ 本地链路必须先 OCR）。"""
    get = get or default_get
    try:
        payload = json.loads(get(f"{local_url()}/models", timeout=5).decode("utf-8"))
    except Exception:
        return False
    for item in payload.get("data") or []:
        modalities = (item or {}).get("modalities") or {}
        if item.get("id") == local_model():
            return bool(modalities.get("vision"))
    return False


def http_status(exc) -> int:
    """从异常里抠 HTTP 状态码（urllib 的 `HTTPError.code` / requests 的 `.response.status_code`）。"""
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _post_chat(base_url: str, api_key: str, model: str, messages, post=None,
               timeout: int = None, max_tokens: int = None, no_think: bool = True) -> tuple:
    """发一次 `/chat/completions`。返回 `(ok, 正文或原因, note)`；`note` 记"这次用的什么形状"。

    `no_think=True` 时带 `chat_template_kwargs={"enable_thinking": false}`（**实测唯一有效的关思考
    开关**，见常量注）；万一对面是个不认这个字段的实现（**400**），自动**去掉它重试一次**，
    并把这件事写进 `note` —— 宁可退化成"多花点 token 思考"，也不能因为一个可选字段整个翻译废掉。
    """
    post = post or default_post
    timeout = int(timeout or request_timeout())

    def send(kwargs: bool):
        body = {"model": model, "messages": messages, "temperature": 0, "stream": False}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if kwargs and no_think:
            body["chat_template_kwargs"] = dict(NO_THINK_KWARG)
        raw = post(f"{base_url.rstrip('/')}/chat/completions",
                   json.dumps(body).encode("utf-8"),
                   {"Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key or LOCAL_KEY}"}, timeout)
        return parse_completion(raw.decode("utf-8"))

    if not no_think:
        ok, payload = send(False)
        return ok, payload, ""
    try:
        ok, payload = send(True)
        return ok, payload, ""
    except Exception as exc:
        if http_status(exc) != 400:                 # 不是"不认这个字段"，照原样报出去
            raise
        ok, payload = send(False)
        return ok, payload, "（对面不认 chat_template_kwargs，已去掉关思考开关重试）"


def _failure_hint(exc, what: str, extra: str = "") -> str:
    """一次失败的**可操作**说法：超时与连不上要分开（排队 ≠ 端点死了）。"""
    timeout = isinstance(exc, (TimeoutError, socket.timeout))
    if timeout:
        return (f"✗ {what}超时（等了 {request_timeout()}s 还没回）{extra} —— 本机模型是**串行**的，"
                f"另一个会话正在用它时会排队（实测同一台机排到过 63.7s）；"
                f"要么等一会儿再按，要么把 {TIMEOUT_ENV} 调大")
    status = http_status(exc)
    head = f"✗ {what}返回 {status}" if status else f"✗ {what}没应答：{exc}"
    return f"{head}{extra}"


def translate_local(text, *, post=None, note="") -> dict:
    t0 = time.perf_counter()
    try:
        ok, payload, shape = _post_chat(local_url(), LOCAL_KEY, local_model(),
                                        build_messages(text), post=post,
                                        max_tokens=TEXT_MAX_TOKENS)
    except Exception as exc:
        return make_result(False, src=text, backend=BACKEND_LOCAL,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=_failure_hint(exc, "本机模型",
                                              f"（`curl {local_url()}/models` 看一眼；"
                                              f"或按面板上的『切到云』）"))
    if not ok:
        return make_result(False, src=text, backend=BACKEND_LOCAL,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 本机模型返回不可用：{payload}")
    return make_result(True, src=text, text=payload, backend=BACKEND_LOCAL,
                       ms=int((time.perf_counter() - t0) * 1000),
                       note=(note or f"{local_model()}（本机，只吃文本）") + shape)


def cloud_hint_for_error(status, reason, cloud) -> tuple:
    """把云端失败翻译成**可操作**的话；401 时明说"凭据失效 → 切本地 / 换一把 key"。

    纪律：文案里**只出现来源标签**（哪一级、哪个文件），绝不出凭据片段。
    """
    source = _safe_source(cloud.get("source") or "")
    where = f"（当前凭据来源：{source}）" if source else ""
    text = str(reason or "")
    if status == 401 or "401" in text or "Unauthorized" in text:
        return False, ("✗ 云端凭据失效（401）→ **按面板上的『切到本地』**用本机模型（离线、免费），"
                       f"或换一把 key（CAPSPLUS_CLOUD_KEY / ~/.bailian/config.json / "
                       f"{CREDENTIALS_FILE} 三处任选）{where}")
    if status in (403, 404):
        return False, (f"✗ 云端不支持这个请求（{status}）{where}—— 换个模型或端点；"
                       "本机那档不受影响，按『切到本地』即可")
    if status == 429:
        return False, f"✗ 云端限流（429）{where}—— 等一会儿或按『切到本地』（本机不限次）"
    if status is None:
        return False, (f"✗ 连不上云端（{text or '网络/证书问题'}）{where}"
                       "—— 按『切到本地』用本机模型更稳")
    return False, f"✗ 云端返回 {status}{where}：{text[:200]}"


def _cloud_attempts(cloud) -> list:
    """这次要按顺序试哪几把凭据：显式传了 `cloud` 就只试它（判据/调用方自己指定），
    否则用三级清单 —— 因为**优先级高的那把可能是坏的**（实测 bailian 那把 401）。"""
    return [cloud] if cloud is not None else cloud_candidates()


def _no_credential_hint(cand) -> str:
    return (f"✗ 云翻译没凭据：{cand.get('config_file')} 缺 token-plan.api_key"
            "（或设 CAPSPLUS_CLOUD_KEY / CAPSPLUS_CLOUD_BASE_URL）")


def translate_cloud(text, *, cloud=None, post=None, note="") -> dict:
    """云端翻译：**逐把凭据试**，某一把 401 就换下一把；全都不行才报错并列出试过哪些。"""
    attempts = _cloud_attempts(cloud)
    t0 = time.perf_counter()
    tried = []
    last = None
    for idx, cand in enumerate(attempts):
        if not (cand.get("base_url") and cand.get("api_key")):
            last = make_result(False, src=text, backend=BACKEND_CLOUD,
                               hint=_no_credential_hint(cand))
            continue
        source = _safe_source(cand.get("source") or "")
        try:
            ok, payload, shape = _post_chat(cand["base_url"], cand["api_key"], cand["model"],
                                            build_messages(text), post=post,
                                            max_tokens=TEXT_MAX_TOKENS)
        except Exception as exc:
            status = http_status(exc)
            if status == 401 and idx + 1 < len(attempts):
                tried.append(source or "（未标明来源）")     # 这把坏了：换下一把（实测会遇上）
                continue
            _ok, hint = cloud_hint_for_error(status, exc, cand)
            return make_result(False, src=text, backend=BACKEND_CLOUD,
                               ms=int((time.perf_counter() - t0) * 1000), hint=hint)
        if not ok:
            _ok, hint = cloud_hint_for_error(None, payload, cand)
            return make_result(False, src=text, backend=BACKEND_CLOUD,
                               ms=int((time.perf_counter() - t0) * 1000), hint=hint)
        tail = f"；401 已跳过的凭据：{'、'.join(tried)}" if tried else ""
        return make_result(True, src=text, text=payload, backend=BACKEND_CLOUD,
                           ms=int((time.perf_counter() - t0) * 1000),
                           note=(note or f"{cand['model']}（云）") + shape + tail)
    if last is not None:
        return last
    return make_result(False, src=text, backend=BACKEND_CLOUD,
                       ms=int((time.perf_counter() - t0) * 1000),
                       hint=("✗ 云端凭据都失效了（401）—— 试过：" + "、".join(tried)
                             + "；按面板上的『切到本地』用本机模型（离线、免费），"
                               "或换一把 key（CAPSPLUS_CLOUD_KEY / ~/.bailian/config.json / "
                             + f"{CREDENTIALS_FILE} 三处任选）"))


def translate_image(png_path, *, cloud=None, post=None) -> dict:
    """**云端多模态直读图**：跳过 OCR，一步出译文（本地没这能力，见 `local_vision`）。"""
    attempts = _cloud_attempts(cloud)
    t0 = time.perf_counter()
    tried = []
    last = None
    for idx, cand in enumerate(attempts):
        if not (cand.get("base_url") and cand.get("api_key")):
            last = make_result(False, backend=BACKEND_CLOUD,
                               hint=f"✗ 云直读图需要凭据：{cand.get('config_file')} "
                                    "缺 token-plan.api_key")
            continue
        try:
            image_b64 = base64.b64encode(Path(png_path).read_bytes()).decode("ascii")
        except OSError as exc:
            return make_result(False, backend=BACKEND_CLOUD, hint=f"✗ 读不了 {png_path}：{exc}")
        try:
            ok, payload, shape = _post_chat(cand["base_url"], cand["api_key"], cand["model"],
                                            build_messages("", image_b64=image_b64), post=post,
                                            max_tokens=IMAGE_MAX_TOKENS)
        except Exception as exc:
            status = http_status(exc)
            if status == 401 and idx + 1 < len(attempts):
                tried.append(_safe_source(cand.get("source") or "") or "（未标明来源）")
                continue
            return make_result(False, backend=BACKEND_CLOUD,
                               ms=int((time.perf_counter() - t0) * 1000),
                               hint=_failure_hint(exc, "云直读图", "（没网？key 过期？）"))
        if not ok:
            return make_result(False, backend=BACKEND_CLOUD,
                               ms=int((time.perf_counter() - t0) * 1000),
                               hint=f"✗ 云端返回不可用：{payload}")
        tail = f"；401 已跳过的凭据：{'、'.join(tried)}" if tried else ""
        return make_result(True, src=payload, text=payload, backend=BACKEND_CLOUD,
                           ms=int((time.perf_counter() - t0) * 1000),
                           note=f"{cand['model']}（云·直读图，跳过 OCR）" + shape + tail)
    if last is not None:
        return last
    return make_result(False, backend=BACKEND_CLOUD,
                       ms=int((time.perf_counter() - t0) * 1000),
                       hint="✗ 云端凭据都失效了（401）—— 试过：" + "、".join(tried))


def translate(text, backend=None, *, local_probe=None, cloud=None, post=None) -> dict:
    """统一入口：按后端翻译一段文本。"""
    note = ""
    if backend is None:
        backend, note = resolve_backend(local_probe=local_probe or local_reachable, cloud=cloud)
        if backend is None:
            return make_result(False, src=text, hint=note)
    text = (text or "").strip()
    if not text:
        return make_result(False, backend=backend, hint="✗ 没有可翻译的原文")
    if backend == BACKEND_LOCAL:
        return translate_local(text, post=post, note=note)
    if backend == BACKEND_CLOUD:
        return translate_cloud(text, cloud=cloud, post=post, note=note)
    return make_result(False, src=text, hint=f"✗ 不认识的翻译后端：{backend}")


PROBE_TEXT = "把「hello」翻译成中文，只回两个字。"
PROBE_STATE_OK = "ok"              # 活着，而且真出了字
PROBE_STATE_EMPTY = "empty"        # 活着，但 content 是空的（推理模型把 token 花在思考里）
PROBE_STATE_TIMEOUT = "timeout"    # 连上了但没在超时内回
PROBE_STATE_DOWN = "down"          # 端点不在／直接报错


def probe_local(post=None, get=None) -> dict:
    """**真跑一次**本机端点（1~8 个 token 的翻译往返），按结果分类。

    为什么不再只看 `GET /v1/models`（2026-10-20 明江真机）：端点"活着"与"能出字"是两件事 ——
    换了推理模型之后这条链路的实际结果是 **content 恒为空**，而 `--translate-status` 照样报"活着"，
    等于把故障说成正常。所以这里发一次**与真翻译同形状**的请求（同 no_think 开关、同 max_tokens 档），
    分四类：`ok` / `empty` / `timeout` / `down`。
    """
    t0 = time.perf_counter()
    try:
        ok, payload, _shape = _post_chat(local_url(), LOCAL_KEY, local_model(),
                                         build_messages(PROBE_TEXT), post=post,
                                         timeout=PROBE_TIMEOUT_S,
                                         max_tokens=PROBE_MAX_TOKENS)
    except Exception as exc:
        kind = (PROBE_STATE_TIMEOUT if isinstance(exc, (TimeoutError, socket.timeout))
                else PROBE_STATE_DOWN)
        return {"state": kind, "ms": int((time.perf_counter() - t0) * 1000),
                "detail": str(exc)[:160], "text": ""}
    ms = int((time.perf_counter() - t0) * 1000)
    if ok:
        return {"state": PROBE_STATE_OK, "ms": ms, "detail": "", "text": payload}
    return {"state": PROBE_STATE_EMPTY, "ms": ms, "detail": payload, "text": ""}


def probe_cloud(post=None, candidates=None) -> list:
    """云端：**逐把凭据**真跑一次小往返，返回每把的状态（401 就接着试下一把，与真翻译一致）。"""
    cands = candidates if candidates is not None else cloud_candidates()
    out = []
    for cand in cands:
        source = _safe_source(cand.get("source") or "") or "（未标明来源）"
        if not (cand.get("base_url") and cand.get("api_key")):
            out.append({"source": source, "state": PROBE_STATE_DOWN, "ms": 0,
                        "detail": "没有凭据"})
            continue
        t0 = time.perf_counter()
        try:
            ok, payload, _shape = _post_chat(cand["base_url"], cand["api_key"], cand["model"],
                                             build_messages(PROBE_TEXT), post=post,
                                             timeout=PROBE_TIMEOUT_S,
                                             max_tokens=PROBE_MAX_TOKENS)
        except Exception as exc:
            status = http_status(exc)
            kind = (PROBE_STATE_TIMEOUT if isinstance(exc, (TimeoutError, socket.timeout))
                    else PROBE_STATE_DOWN)
            out.append({"source": source, "state": kind,
                        "ms": int((time.perf_counter() - t0) * 1000),
                        "detail": (f"HTTP {status}" if status else str(exc)[:160])})
            continue
        ms = int((time.perf_counter() - t0) * 1000)
        out.append({"source": source, "state": PROBE_STATE_OK if ok else PROBE_STATE_EMPTY,
                    "ms": ms, "detail": "" if ok else payload, "text": payload if ok else ""})
    return out


def _state_text(state: str, ms: int, text: str = "", detail: str = "") -> str:
    if state == PROBE_STATE_OK:
        return f"活着且能出字（实测 {ms}ms，回「{text[:20]}」）"
    if state == PROBE_STATE_EMPTY:
        return f"**活着但出不了字**（{detail}）"
    if state == PROBE_STATE_TIMEOUT:
        return f"超时（{PROBE_TIMEOUT_S}s 内没回）—— 端点活着但在排队或卡住"
    return f"端点不在／报错（{detail or '连不上'}）"


def describe(get=None, post=None, probe: bool = True) -> str:
    """一页现状（`capsplus-shot --translate-status` 与排查用）。

    `probe=True`（默认）会**真跑一次**本机小往返（见 `probe_local`）；判据/离线场景可以传
    `probe=False` 只看配置，或注入 `get`/`post` 桩。凭据值一律不出现（只出"哪来的"标签）。
    """
    cloud = cloud_config()
    reachable = local_reachable(get=get)
    if not probe:
        local_state = f"端点{'探得到' if reachable else '探不到'}"
    elif not reachable:
        local_state = "端点不在（`curl " + local_url() + "/models` 看一眼）"
    else:
        local_state = _state_text(**{k: v for k, v in probe_local(post=post).items()
                                     if k in ("state", "ms", "text", "detail")})
    lines = [f"模式：{mode()}",
             f"本机端点：{local_url()} → {local_state}",
             f"本机模型：{local_model()}（能读图？{'能' if reachable and local_vision(get=get) else '不能——只吃文本'}）",
             f"云端：{cloud['model']} @ {cloud['base_url'] or '（没配 base_url）'}"
             f"，凭据候选 {len([c for c in cloud_candidates() if c['api_key']])} 把"
             f"（{cloud['config_file']}）"]
    if probe:
        lines.append("  ⚠ 云端是**计费** API，这条只在你真按了『切到云』/F2 走云时才有意义：")
        for item in probe_cloud(post=post):
            mark = {"ok": "✓", "empty": "⚠", "timeout": "…", "down": "✗"}[item["state"]]
            lines.append(f"  {mark} {item['source']}："
                         + _state_text(item["state"], item["ms"],
                                       text=item.get("text", ""), detail=item.get("detail", "")))
        lines.append("  （第一把 401 时会自动换下一把；全坏会明确报出来）")
    return "\n".join(lines)


if __name__ == "__main__":                                    # pragma: no cover - 手查用
    print(describe())