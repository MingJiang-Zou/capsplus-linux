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
TIMEOUT_S = 60

SYSTEM_PROMPT = ("你是翻译引擎。把用户给的文字翻译成目标语言，只输出译文本身，"
                 "不要解释、不要加引号、不要重复原文。")
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


def cloud_config(config_path=None, credentials_path=None) -> dict:
    """阿里 token-plan 的 base_url / key / 默认模型（**一律不打印值**）。

    凭据顺序（**三级**，从高到低）：
      ① `CAPSPLUS_CLOUD_KEY` 环境变量（临时/CI 用）
      ② `~/.bailian/config.json` 的 `token-plan` 段（`bl` CLI 的文件；**只读，绝不改写**）
      ③ `~/.dsh/.credentials.yaml` 的 `refs.AL_TOKEN_PLAN_API_KEY`（DSH 自己那把，实测可用）

    纪律：只读、绝不打印、绝不写回任何一个凭据文件；`source` 字段只记"哪来的"，不带值。
    返回里带上 `source` 与 `config_file`，面板与报错据此说清"用的是哪一把"。
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
    key, source = "", ""
    if env_key:
        key, source = env_key, SOURCE_ENV
    elif section.get("api_key"):
        key, source = section["api_key"], SOURCE_BAILIAN
    else:
        key = read_dsh_credential(credentials_path)
        if key:
            source = SOURCE_DSH
    return {"base_url": normalize_base_url(env_url or section.get("base_url", "")),
            "api_key": key,
            "model": env_model or DEFAULT_CLOUD_MODEL,
            "config_file": str(path),
            "credentials_file": str(Path(os.path.expanduser(credentials_path
                                                            or CREDENTIALS_FILE))),
            "source": _safe_source(source)}


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


def parse_completion(payload) -> tuple:
    """从 OpenAI 兼容响应里取正文。返回 `(ok, text_or_hint)`。"""
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
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):                       # 有些实现回 content 数组
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        return False, "响应里没有正文（content 为空）"
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


def _post_chat(base_url: str, api_key: str, model: str, messages, post=None,
               timeout: int = TIMEOUT_S) -> tuple:
    post = post or default_post
    body = json.dumps({"model": model, "messages": messages, "temperature": 0,
                       "stream": False}).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key or LOCAL_KEY}"}
    raw = post(f"{base_url.rstrip('/')}/chat/completions", body, headers, timeout)
    return parse_completion(raw.decode("utf-8"))


def translate_local(text, *, post=None, note="") -> dict:
    t0 = time.perf_counter()
    try:
        ok, payload = _post_chat(local_url(), LOCAL_KEY, local_model(),
                                 build_messages(text), post=post)
    except Exception as exc:
        return make_result(False, src=text, backend=BACKEND_LOCAL,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 本机模型没应答：{exc}（`curl {local_url()}/models` 看一眼；"
                                f"或按面板上的『切到云』）")
    if not ok:
        return make_result(False, src=text, backend=BACKEND_LOCAL,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 本机模型返回不可用：{payload}")
    return make_result(True, src=text, text=payload, backend=BACKEND_LOCAL,
                       ms=int((time.perf_counter() - t0) * 1000),
                       note=note or f"{local_model()}（本机，只吃文本）")


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


def translate_cloud(text, *, cloud=None, post=None, note="") -> dict:
    cloud = cloud if cloud is not None else cloud_config()
    t0 = time.perf_counter()
    if not (cloud.get("base_url") and cloud.get("api_key")):
        return make_result(False, src=text, backend=BACKEND_CLOUD,
                           hint=f"✗ 云翻译没凭据：{cloud.get('config_file')} 缺 token-plan.api_key"
                                "（或设 CAPSPLUS_CLOUD_KEY / CAPSPLUS_CLOUD_BASE_URL）")
    try:
        ok, payload = _post_chat(cloud["base_url"], cloud["api_key"], cloud["model"],
                                 build_messages(text), post=post)
    except HTTPStatusError as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        _ok, hint = cloud_hint_for_error(code, exc, cloud)
        return make_result(False, src=text, backend=BACKEND_CLOUD,
                           ms=int((time.perf_counter() - t0) * 1000), hint=hint)
    except Exception as exc:
        _ok, hint = cloud_hint_for_error(None, exc, cloud)
        return make_result(False, src=text, backend=BACKEND_CLOUD,
                           ms=int((time.perf_counter() - t0) * 1000), hint=hint)
    if not ok:
        _ok, hint = cloud_hint_for_error(None, payload, cloud)
        return make_result(False, src=text, backend=BACKEND_CLOUD,
                           ms=int((time.perf_counter() - t0) * 1000), hint=hint)
    return make_result(True, src=text, text=payload, backend=BACKEND_CLOUD,
                       ms=int((time.perf_counter() - t0) * 1000),
                       note=note or f"{cloud['model']}（云）")


def translate_image(png_path, *, cloud=None, post=None) -> dict:
    """**云端多模态直读图**：跳过 OCR，一步出译文（本地没这能力，见 `local_vision`）。"""
    cloud = cloud if cloud is not None else cloud_config()
    t0 = time.perf_counter()
    if not (cloud.get("base_url") and cloud.get("api_key")):
        return make_result(False, backend=BACKEND_CLOUD,
                           hint=f"✗ 云直读图需要凭据：{cloud.get('config_file')} 缺 token-plan.api_key")
    try:
        image_b64 = base64.b64encode(Path(png_path).read_bytes()).decode("ascii")
    except OSError as exc:
        return make_result(False, backend=BACKEND_CLOUD, hint=f"✗ 读不了 {png_path}：{exc}")
    try:
        ok, payload = _post_chat(cloud["base_url"], cloud["api_key"], cloud["model"],
                                 build_messages("", image_b64=image_b64), post=post)
    except Exception as exc:
        return make_result(False, backend=BACKEND_CLOUD,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 云直读图失败：{exc}（没网？key 过期？）")
    if not ok:
        return make_result(False, backend=BACKEND_CLOUD,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 云端返回不可用：{payload}")
    return make_result(True, src=payload, text=payload, backend=BACKEND_CLOUD,
                       ms=int((time.perf_counter() - t0) * 1000),
                       note=f"{cloud['model']}（云·直读图，跳过 OCR）")


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


def describe(get=None) -> str:
    """一页现状（`capsplus-shot --translate-status` 与排查用）。"""
    cloud = cloud_config()
    reachable = local_reachable(get=get)
    lines = [f"模式：{mode()}",
             f"本机端点：{local_url()} → {'活着' if reachable else '探不到'}",
             f"本机模型：{local_model()}（能读图？{'能' if reachable and local_vision(get=get) else '不能——只吃文本'}）",
             f"云端：{cloud['model']} @ {cloud['base_url'] or '（没配 base_url）'}"
             f"，凭据 {'有' if cloud['api_key'] else '没有'}（{cloud['config_file']}）"]
    return "\n".join(lines)


if __name__ == "__main__":                                    # pragma: no cover - 手查用
    print(describe())