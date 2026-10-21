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

后端选择（`resolve_backend()` 现在是**配置驱动**的纯函数）：`CAPSPLUS_TRANSLATE=local|cloud|auto`
（默认 auto）**或配置文件里的端点名**；`auto` = 按**书写顺序**挨个试（配置里 local 在前就 local 优先），
否则用云；两边都不行 → 给**可操作**的话。

**服务在哪现在是可配的**（2026-10-21，用户原话：「关于 capsplus 工具，我希望关于翻译 OCR 语音识别
的服务都是可以配置的，方便配置远程的服务或者是本地的」）：改之前"本机还是云端"是写死的两个名字，
搬到另一台机器上除了改代码没别的办法。现在端点从 `~/.config/capsplus/services.toml` 读
（底座是 `capsplus/services.py`，**本模块只读不改**），三条链路共用同一份：

    优先级（全仓口径）：**环境变量 > 配置文件 > 内置默认**
      · 环境变量 = 临时覆盖（现场救火用），`CAPSPLUS_TRANSLATE` 还能直接写**端点名**；
      · 配置文件 = 长期设置，`[translate.local]` / `[translate.cloud]` / 任意自建端点；
      · 内置默认 = 与 2026-10-21 之前的行为**逐字一致**（不许因为加了配置层就把老用法弄坏）。

返回给面板的 dict 与 OCR 同一套约定（`shot_annotate.normalize_result`）：
  `{"ok", "src", "text", "backend", "ms", "note", "hint"}`
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import tomllib
import urllib.request
from pathlib import Path

try:                                    # 包内导入（正常路径）
    from . import services as S
except ImportError:                     # 直接当脚本跑（`python3 capsplus/shot_translate.py`）
    import services as S

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


# ------------------------------------------------------------------ 服务配置（2026-10-21 新增）
# 底座是 `capsplus/services.py`（Lead 写的那份，**本模块只读不改**）。
# 为什么这里自己包一层薄封装而不是直接 `services.load_services()`：
#   ① 三条链路（翻译/OCR/识别）各自要的是"自己那一段"，薄封装把
#      「文件在哪（可用环境变量指到别处）」与「读坏了怎么办」统一成一处；
#   ② `services.py` 的 `default_services()` 给翻译的默认档是 `auto`，而**本模块**的默认档是
#      `local`（2026-10-19 用户定的口径："翻译最好用免费的"，默认档绝不许静默切到计费云）。
#      这个冲突只能靠"用户**显式写了**没有"来判 ⇒ 见 `declared_endpoint()`。
CONFIG_ENV = "CAPSPLUS_SERVICES_FILE"      # 指到别的 services.toml（多套配置 / 判据用）


def services_config_path(path=None) -> str:
    """服务配置文件的落点：入参 > `CAPSPLUS_SERVICES_FILE` > 默认落点。

    为什么给环境变量留这个口子：判据要拿**注入的 fixture 文件**跑（不许碰用户真配的那份），
    生产上偶尔也有"临时换一套配置"的场景 —— 与其改代码不如换个环境变量。
    """
    return path or os.environ.get(CONFIG_ENV) or str(S.SERVICES_PATH)


def load_config(path=None):
    """读服务配置。**读不了 / 语法错都不抛栈** —— `services.load_services()` 自己兜住了
    （错误写进 `services.error`，界面照常能开），这与仓里"状态必须带动作"的规矩一致。"""
    return S.load_services(services_config_path(path))


def declared_endpoint(capability: str, endpoint: str = "", path=None) -> dict:
    """配置文件里**显式写了**的那些键（没写就是没写）。

    为什么自己读一次 TOML（而不是直接问 `services.load_services()`）：
    `load_services()` 把"用户写的"和"内置默认"揉在一起，分不清 —— 翻译的默认档就是例子
    （它的内置默认是 `auto`，本模块的默认档是 `local`）。要按"用户显式写没写"判优先级，
    就得自己读那一个键。

    ⚠ **只读**：绝不写回这个文件（写回是设置窗那一半的事）。
    """
    try:
        with open(services_config_path(path), "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    table = data.get(capability)
    if not isinstance(table, dict):
        return {}
    if endpoint:
        sub = table.get(endpoint)
        return dict(sub) if isinstance(sub, dict) else {}
    return {k: v for k, v in table.items() if not isinstance(v, dict)}


def translate_endpoints(cfg=None) -> list:
    """配置里的翻译端点。

    顺序 = **配置文件里的书写顺序**（`auto` 的优先级就是这个顺序）——
    这一条由 `services.load_services()` 保证（2026-10-21 Lead 复核时改成"文件里写过就完全
    以文件为准"，不再与内置默认合并），客户端**不再自己重排**（别造第二份真相）。
    文件里没写 `[translate]` 那一段时，内置默认原样保留（local → cloud）。
    """
    cfg = cfg if cfg is not None else load_config()
    cap = cfg.get("translate")
    return list(cap.endpoints.values()) if cap else []


def endpoint_by_name(name: str, cfg=None):
    """按名字取一个端点（找不到就是 None —— 调用方要给"怎么改"的提示，不许静默当没有）。"""
    for ep in translate_endpoints(cfg):
        if ep.name == name:
            return ep
    return None


def _pick(env_value: str, config_value: str, default: str) -> str:
    """「谁说了算」的**优先级本体**：环境变量 > 配置文件 > 内置默认。

    单独抽成一个函数，是为了让判据能**反例具型**：把这里的顺序反过来，判据必须红
    （见 `tests/test_shot_translate.py::TestCounterExamples`）。散在各处的话，判据抓不住。
    """
    return env_value or config_value or default


def _config_note() -> str:
    """服务配置那行怎么说：**还没这个文件**时也要如实说（不然看着像"配过了"）。"""
    path = services_config_path()
    if not Path(path).exists():
        return f"{path}（**还没这个文件** ⇒ 用的是内置默认）"
    return path


def _known_mode(value: str, cfg=None) -> bool:
    """这个档位名字认不认得（`local`/`cloud`/`auto` 或配置里的端点名）。"""
    if not value:
        return False
    if value.lower() in BACKENDS + ("auto",):
        return True
    return any(ep.name == value for ep in translate_endpoints(cfg))


# ------------------------------------------------------------------ 纯逻辑（判据直接调）

DEFAULT_MODE = BACKEND_LOCAL


def mode(cfg=None) -> str:
    """当前后端档位；**默认 `local`**（2026-10-19 用户定的口径）。

    他问"翻译用的是哪个云服务？最好用免费的" —— `cloud` 那条是**按 token 计费的 LLM API**，
    不是免费翻译服务，而且那把 key 还 401 了。所以默认永远先走本机（离线、不限次、免费），
    **绝不因为云 key 失效而失败**；`auto`/`cloud` 两档保留，但要他主动切才走。

    **优先级（2026-10-21 起是配置驱动的）**：环境变量 > 配置文件 > 内置默认。
      · `CAPSPLUS_TRANSLATE` 可以是 `local` / `cloud` / `auto`，**也可以是配置里的端点名**
        （自建的远程端点，比如公司机上那个 OpenAI 兼容服务）；
      · 配置文件里 `[translate]` 的 `mode` **显式写了**才算数（没写就是没写 ——
        `services.default_services()` 给的默认是 `auto`，那是库的默认，不是用户的意愿）；
      · 内置默认是 `local`。
    """
    raw = (os.environ.get(MODE_ENV) or "").strip()
    env_value = raw if _known_mode(raw, cfg) else ""
    declared = str(declared_endpoint("translate").get("mode") or "").strip()
    config_value = declared if _known_mode(declared, cfg) else ""
    return _pick(env_value, config_value, DEFAULT_MODE)


def local_url(cfg=None) -> str:
    """本机端点的地址：环境变量 > 配置文件 > 内置默认。

    为什么要从配置取：`CAPSPLUS_TRANSLATE_URL` 只能"临时"改，而"把翻译服务搬到另一台机器"
    是长期设置 ⇒ 落在 `services.toml` 的 `[translate.local] base_url` 上。
    """
    ep = endpoint_by_name(BACKEND_LOCAL, cfg)
    config_value = (ep.base_url if ep else "").strip()
    env = (os.environ.get(LOCAL_URL_ENV) or "").strip()
    return _pick(env, config_value, DEFAULT_LOCAL_URL).rstrip("/")


def local_model(cfg=None) -> str:
    """本机端点的模型名：环境变量 > 配置文件 > 内置默认。

    ⚠ 配置文件里那一段是**整段替换**内置默认的（`services.load_services()` 的口径），
    所以用户只写 `base_url` 没写 `model` 时，这里会退回内置默认 —— 不许因此报"没模型"。
    """
    ep = endpoint_by_name(BACKEND_LOCAL, cfg)
    config_value = (ep.model if ep else "").strip()
    env = (os.environ.get(LOCAL_MODEL_ENV) or "").strip()
    return _pick(env, config_value, DEFAULT_LOCAL_MODEL)


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


def _declared_credential(table: dict) -> tuple:
    """从**显式声明的那几项**里解出一把 key（顺序与 `Endpoint.resolved_api_key()` 一致）。

    为什么要绕这一下（而不是直接 `Endpoint.resolved_api_key()`）：内置默认的云端端点已经写着
    `credentials_file = ~/.dsh/.credentials.yaml`，直接解会把**用户自己的**凭据文件读进来 ——
    判据必须能在不碰真凭据的前提下测"配置里的凭据来源排第一"。
    这里只用**文件里显式写了**的那几项，判据因此可以拿 fixture 测。
    """
    if not table:
        return "", ""
    ep = S.Endpoint(
        name="declared",
        api_key=str(table.get("api_key", "")),
        api_key_env=str(table.get("api_key_env", "")),
        credentials_file=str(table.get("credentials_file", "")),
        credentials_key=str(table.get("credentials_key", "")),
        env_file=str(table.get("env_file", "")),
        env_key=str(table.get("env_key", "")),
    )
    return ep.resolved_api_key()


def cloud_candidates(config_path=None, credentials_path=None) -> list:
    """云端凭据的**候选清单**（按优先级排序，去重；每项与 `cloud_config()` 同形状）。

    为什么要一份清单（2026-10-20 实测）：`~/.bailian/config.json` 里那把 key **实测 401**
    （`Invalid API-key provided`），而 `~/.dsh/.credentials.yaml` 的 `AL_TOKEN_PLAN_API_KEY`
    同一时刻 **HTTP 200 正常出字** —— 优先级高的那把坏了，光"有凭据"是不够的，要能**换下一把**。
    清单里只有"哪来的"标签（`source`），**绝不带值**。

    **2026-10-21 新增（服务可配）**：配置文件 `services.toml` 里 `[translate.cloud]` **显式写了**的
    凭据来源排在**最前**（用户明确说了"这条链路的 key 在这"），老的三级顺序原样留在后面当兜底；
    `base_url` / `model` 也认配置里显式写的那两个（环境变量仍然最高）。
    ⚠ **没有配置文件时，本函数的行为与 2026-10-21 之前逐字一致**（那两条新规则都读不到东西）。
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
    # 服务配置里**显式写**的那几项（没有配置文件时全是空串 ⇒ 行为不变）
    declared = declared_endpoint("translate", BACKEND_CLOUD)
    # base_url：环境变量 > 服务配置 > `~/.bailian/config.json` 里记的那个
    # ⚠ 服务配置里那个**不补兼容路径**：那是用户自己写的地址（自建端点可能是 `http://nas:8080/v1`，
    #   补成 `/compatible-mode/v1` 就打错地方了）；只有 bailian 那个站点根才需要补。
    if env_url:
        base_url = normalize_base_url(env_url)
    elif declared.get("base_url"):
        base_url = str(declared["base_url"]).strip().rstrip("/")
    else:
        base_url = normalize_base_url(section.get("base_url", ""))
    model = env_model or str(declared.get("model") or "").strip() or DEFAULT_CLOUD_MODEL
    found = []
    cfg_key, cfg_source = _declared_credential(declared)
    if cfg_key:
        found.append((cfg_key, f"服务配置的 {cfg_source}"))
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


def endpoint_candidates(ep, *, legacy=None) -> list:
    """这个端点要按顺序试哪几把凭据（每项与 `cloud_config()` 同形状）。

    ① 端点**自己**写的凭据来源（`api_key_env` / `credentials_file`+键 / `env_file`+键 / 明文）
       —— 用户明确说了"这条链路的 key 在这"，它排第一；
    ② `cloud` 端点额外挂上**老的三级候选**（`CAPSPLUS_CLOUD_KEY` → `~/.bailian/config.json`
       → `~/.dsh/.credentials.yaml`）当兜底 —— 实测 `~/.bailian` 那把 key 是 401 的，
       "有凭据"不等于"能用"，所以第一把坏了要能换下一把。

    每项的 `source` 只记"哪来的"，**绝不带值**。

    ⚠ `cloud` 那一档**只认配置里显式写了**的凭据来源：内置默认端点上那个
    `~/.dsh/.credentials.yaml` 是**库给的**默认值，不是用户写的 —— 直接解它会把用户自己的
    凭据文件读进来（判据不许碰真凭据），而且会把老的三级顺序改掉。
    """
    if ep.name == BACKEND_CLOUD:
        # 老的三级候选 + 配置里显式写的那个（`cloud_candidates()` 已经把两者合上了）
        # ⚠ `legacy` 可能是**一个**候选 dict（老判据传进来的），也可能是清单 —— 两种都收
        if legacy is None:
            out = list(cloud_candidates())
        elif isinstance(legacy, dict):
            out = [legacy]
        else:
            out = list(legacy)
        if not out:
            out = [{"base_url": ep.base_url, "api_key": "", "model": ep.model,
                    "config_file": services_config_path(),
                    "credentials_file": "", "source": ""}]
        return out
    out = []
    key, source = ep.resolved_api_key()
    if key:
        out.append({"base_url": ep.base_url, "api_key": key, "model": ep.model,
                    "config_file": services_config_path(),
                    "credentials_file": "", "source": _safe_source(source)})
    if not out:                        # 一把都没有：也要返回一条，好让文案说清"缺凭据"
        out.append({"base_url": ep.base_url, "api_key": "", "model": ep.model,
                    "config_file": services_config_path(),
                    "credentials_file": "", "source": ""})
    return out


def _endpoint_label(ep) -> str:
    """端点怎么叫人：`local` 那一档叫"本机模型"，远程的叫"远程端点"。

    为什么分开叫：配置之后"本机"这两个字不再等于"127.0.0.1"（可以把本机模型指到另一台机器），
    文案还说"本机模型"就会骗人。
    """
    return "本机模型" if ep.kind == S.KIND_LOCAL else "远程端点"


def _endpoint_ready(ep, *, cloud=None, local_probe=None, strict=False) -> tuple:
    """这个端点现在能不能用？返回 `(能用, 说明)`。

    `strict=True` = 用户**点名**要这个端点（这时不许静默换别的，只如实说缺什么）；
    `strict=False` = `auto` 挨个试（这时只要说清为什么不行，好让下一条接着试）。
    """
    if ep.name == BACKEND_LOCAL:
        probe = local_probe or local_reachable
        try:
            reachable = bool(probe())
        except Exception:
            reachable = False
        if reachable:
            return True, f"{_endpoint_label(ep)}（{local_model()}）"
        # 默认档就是本机：这时**不谈"自动切云"**，只如实说"本机没起、怎么起"
        return False, (f"✗ 本机模型没起：{local_url()} 探不到 —— "
                       f"起来：`ollama serve` 或 systemctl --user start 你那条服务"
                       f"（判活：`curl {local_url()}/models`）"
                       f"；想用云端就按面板上的『切到云』（那是计费 API，不是免费翻译）")
    if ep.name == BACKEND_CLOUD:
        cands = endpoint_candidates(ep, legacy=cloud)
        usable = [c for c in cands if c.get("base_url") and c.get("api_key")]
        model = (usable[0].get("model") if usable else "") or ep.model or DEFAULT_CLOUD_MODEL
        if usable:
            return True, f"云端（{model}，可直读图）"
        where = cands[0].get("config_file") if cands else services_config_path()
        if strict:
            return False, (f"✗ 你指定了云翻译，但 {where} 里没有 token-plan 凭据"
                           f"（或设 CAPSPLUS_CLOUD_KEY / CAPSPLUS_CLOUD_BASE_URL）")
        return False, (f"✗ 云端也没凭据（{where} 缺 token-plan.api_key，"
                       f"或设 CAPSPLUS_CLOUD_KEY / CAPSPLUS_CLOUD_BASE_URL）")
    # 自建端点：看它自己写的凭据来源
    key, source = ep.resolved_api_key()
    if not ep.base_url:
        return False, (f"✗ 端点 {ep.name} 没填 base_url —— 在 {services_config_path()} 的 "
                       f"`[translate.{ep.name}]` 里补上")
    if not key:
        return False, (f"✗ 端点 {ep.name} 没凭据（{source}）—— 在 {services_config_path()} 的 "
                       f"`[translate.{ep.name}]` 里写 api_key_env / credentials_file，"
                       f"或设对应的环境变量")
    return True, f"{_endpoint_label(ep)} {ep.name}（{ep.model or '未指定模型'}）"


def resolve_backend(local_probe=None, cloud=None, cfg=None) -> tuple:
    """`(backend | None, 说明)`；不可用时说明必须**可操作**。

    **现在是配置驱动的**（2026-10-21）：后端不再是写死的 `local` / `cloud` 两个，
    而是"配置里的端点名"；`auto` 按**书写顺序**挨个试（配置里 local 在前就 local 优先）。

    `local_probe` 缺省就是**真探一次** `GET /v1/models`。别拿 `lambda: False` 当默认 ——
    那会让直接调它的人（包括我 2026-10-19 自己）得到"本机模型没起"的错结论。
    """
    mode_ = mode(cfg)
    local_probe = local_probe or local_reachable
    endpoints = translate_endpoints(cfg)

    # 环境变量写了个**认不出来的档位**（多半是端点名打错了）：这时必须**说出来**，
    # 不许静默退回默认 —— 用户以为在用 nas，其实在用本机。
    # ⚠ 只在**有配置文件**时才有"端点名"这回事（2026-10-21 之前只有 local/cloud/auto），
    # 所以没有配置文件时仍按老口径：认不出来就回默认（见 `mode()`）。
    raw = (os.environ.get(MODE_ENV) or "").strip()
    if raw and not _known_mode(raw, cfg) and endpoints:
        known = "、".join(e.name for e in endpoints)
        return None, (f"✗ 档位 `{raw}` 认不出来 —— 可用的是：{known}"
                      f"（改 {MODE_ENV}，或在 {services_config_path()} 里写端点）")

    if mode_ != "auto":
        ep = endpoint_by_name(mode_, cfg)
        if ep is None:
            known = "、".join(e.name for e in endpoints) or "（一个都没有）"
            return None, (f"✗ 档位 `{mode_}` 在服务配置里没有对应的端点"
                          f"（{services_config_path()}）—— 可用的是：{known}")
        ok, why = _endpoint_ready(ep, cloud=cloud, local_probe=local_probe, strict=True)
        return (ep.name, why) if ok else (None, why)

    reasons = []
    for ep in endpoints:
        ok, why = _endpoint_ready(ep, cloud=cloud, local_probe=local_probe, strict=False)
        if ok:
            return ep.name, why
        reasons.append(why)
    # 每条原因自带"✗"前缀，串起来只留一个（免得出现"✗ ✗ …"）
    return None, ("✗ " + "；".join(r.lstrip("✗ ").strip() for r in reasons))


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

    ⚠ **空 content 必须给明确原因**（2026-10-20 用户真机："翻译没反应"）：推理模型把
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


def _no_credential_hint(cand, style: str = "cloud") -> str:
    if style == "cloud":
        return (f"✗ 云翻译没凭据：{cand.get('config_file')} 缺 token-plan.api_key"
                "（或设 CAPSPLUS_CLOUD_KEY / CAPSPLUS_CLOUD_BASE_URL）")
    return (f"✗ 端点没凭据：{cand.get('config_file')} 里没读到 key"
            f"（来源：{cand.get('source') or '没配'}）—— 在服务配置里写 api_key_env / "
            f"credentials_file，或设对应的环境变量")


def endpoint_hint_for_error(status, reason, cand) -> tuple:
    """自建端点失败的话术：**下一步要具体**（换端点还是换 key？）。"""
    source = _safe_source(cand.get("source") or "")
    where = f"（当前凭据来源：{source}）" if source else ""
    text = str(reason or "")
    if status == 401 or "401" in text or "Unauthorized" in text:
        return False, (f"✗ 端点凭据失效（401）{where}—— 换一把 key，或在服务配置里改这个端点的"
                       "凭据来源；也可能是这个端点根本不收 key（那就把它摘掉）")
    if status in (403, 404):
        return False, (f"✗ 端点不支持这个请求（{status}）{where}—— 换个模型或端点"
                       "（服务配置里改 base_url / model）")
    if status == 429:
        return False, f"✗ 端点限流（429）{where}—— 等一会儿再试，或换一个端点"
    if status is None:
        return False, (f"✗ 连不上端点 {cand.get('base_url')}（{text or '网络/证书问题'}）{where}"
                       "—— 查那台机器上的服务在不在、网络通不通（`curl -sv "
                       f"{cand.get('base_url')}/models`）")
    return False, f"✗ 端点返回 {status}{where}：{text[:200]}"


def _hint_for_error(status, reason, cand, style: str = "cloud") -> tuple:
    if style == "cloud":
        return cloud_hint_for_error(status, reason, cand)
    return endpoint_hint_for_error(status, reason, cand)


def _all_401_hint(tried, style: str = "cloud") -> str:
    if style == "cloud":
        return ("✗ 云端凭据都失效了（401）—— 试过：" + "、".join(tried)
                + "；按面板上的『切到本地』用本机模型（离线、免费），"
                  "或换一把 key（CAPSPLUS_CLOUD_KEY / ~/.bailian/config.json / "
                + f"{CREDENTIALS_FILE} 三处任选）")
    return ("✗ 端点的凭据都失效了（401）—— 试过：" + "、".join(tried)
            + "；换一把 key，或在服务配置里改凭据来源")


def _translate_attempts(text, attempts, *, backend, note="", post=None, style="cloud",
                         default_note="", messages=None, max_tokens=TEXT_MAX_TOKENS) -> dict:
    """按顺序试几把凭据：某一把 401 就换下一把；全都不行才报错并列出试过哪些。

    `style` 决定话术：`cloud` 是历史上的云端那档（提"切到本地"这条退路），
    `endpoint` 是自建端点（它可能压根没有本机那条退路 ⇒ 话术要给"换端点/换 key"）。
    """
    t0 = time.perf_counter()
    tried = []
    last = None
    body = messages if messages is not None else build_messages(text)
    for idx, cand in enumerate(attempts):
        if not (cand.get("base_url") and cand.get("api_key")):
            last = make_result(False, src=text, backend=backend,
                               hint=_no_credential_hint(cand, style=style))
            continue
        source = _safe_source(cand.get("source") or "")
        try:
            ok, payload, shape = _post_chat(cand["base_url"], cand["api_key"], cand["model"],
                                            body, post=post, max_tokens=max_tokens)
        except Exception as exc:
            status = http_status(exc)
            if status == 401 and idx + 1 < len(attempts):
                tried.append(source or "（未标明来源）")     # 这把坏了：换下一把（实测会遇上）
                continue
            _ok, hint = _hint_for_error(status, exc, cand, style=style)
            return make_result(False, src=text, backend=backend,
                               ms=int((time.perf_counter() - t0) * 1000), hint=hint)
        if not ok:
            _ok, hint = _hint_for_error(None, payload, cand, style=style)
            return make_result(False, src=text, backend=backend,
                               ms=int((time.perf_counter() - t0) * 1000), hint=hint)
        tail = f"；401 已跳过的凭据：{'、'.join(tried)}" if tried else ""
        return make_result(True, src=text, text=payload, backend=backend,
                           ms=int((time.perf_counter() - t0) * 1000),
                           note=(note or default_note or f"{cand['model']}（云）") + shape + tail)
    if last is not None:
        return last
    return make_result(False, src=text, backend=backend,
                       ms=int((time.perf_counter() - t0) * 1000),
                       hint=_all_401_hint(tried, style=style))


def translate_cloud(text, *, cloud=None, post=None, note="") -> dict:
    """云端翻译：**逐把凭据试**，某一把 401 就换下一把；全都不行才报错并列出试过哪些。"""
    return _translate_attempts(text, _cloud_attempts(cloud), backend=BACKEND_CLOUD,
                               note=note, post=post, style="cloud")


def translate_endpoint(text, ep, *, post=None, note="", candidates=None) -> dict:
    """按**配置里的端点**翻译一段文字（自建远程端点走这条）。

    为什么要有这条（2026-10-21）：改之前只有 local/cloud 两个**写死**的后端，
    "把翻译服务搬到公司机 / 另一台 NAS"这件事除了改代码没别的办法。现在端点写在
    `services.toml` 里，凭据来源也写在同一个文件里（只写来源，不写值）。
    """
    cands = candidates if candidates is not None else endpoint_candidates(ep)
    return _translate_attempts(text, cands, backend=ep.name, note=note, post=post,
                               style="endpoint",
                               default_note=f"{ep.model or '未指定模型'}（端点 {ep.name}）")


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


def translate_image_endpoint(png_path, ep, *, post=None, candidates=None) -> dict:
    """按**配置里的端点**直读图翻译（端点的模型得能看图 —— 本机那档实测**不能**，见 `local_vision`）。

    为什么单独一条：2026-10-21 之前只有云端那档有多模态能力，自建端点（公司机上的视觉模型）
    配好后也得有一条路把它接上。
    """
    try:
        image_b64 = base64.b64encode(Path(png_path).read_bytes()).decode("ascii")
    except OSError as exc:
        return make_result(False, backend=ep.name, hint=f"✗ 读不了 {png_path}：{exc}")
    cands = candidates if candidates is not None else endpoint_candidates(ep)
    result = _translate_attempts("", cands, backend=ep.name, post=post, style="endpoint",
                                 default_note=f"{ep.model or '未指定模型'}（端点 {ep.name}·直读图）",
                                 messages=build_messages("", image_b64=image_b64),
                                 max_tokens=IMAGE_MAX_TOKENS)
    result["src"] = result.get("text", "")
    return result


def translate(text, backend=None, *, local_probe=None, cloud=None, post=None, cfg=None) -> dict:
    """统一入口：按后端翻译一段文本。

    `backend` 可以是 `local` / `cloud`，**也可以是配置里的端点名**（自建远程端点）。
    """
    note = ""
    if backend is None:
        backend, note = resolve_backend(local_probe=local_probe or local_reachable,
                                        cloud=cloud, cfg=cfg)
        if backend is None:
            return make_result(False, src=text, hint=note)
    text = (text or "").strip()
    if not text:
        return make_result(False, backend=backend, hint="✗ 没有可翻译的原文")
    if backend == BACKEND_LOCAL:
        return translate_local(text, post=post, note=note)
    if backend == BACKEND_CLOUD:
        return translate_cloud(text, cloud=cloud, post=post, note=note)
    ep = endpoint_by_name(backend, cfg)
    if ep is not None:
        return translate_endpoint(text, ep, post=post, note=note)
    return make_result(False, src=text, hint=f"✗ 不认识的翻译后端：{backend}")


PROBE_TEXT = "把「hello」翻译成中文，只回两个字。"
PROBE_STATE_OK = "ok"              # 活着，而且真出了字
PROBE_STATE_EMPTY = "empty"        # 活着，但 content 是空的（推理模型把 token 花在思考里）
PROBE_STATE_TIMEOUT = "timeout"    # 连上了但没在超时内回
PROBE_STATE_DOWN = "down"          # 端点不在／直接报错


def probe_local(post=None, get=None) -> dict:
    """**真跑一次**本机端点（1~8 个 token 的翻译往返），按结果分类。

    为什么不再只看 `GET /v1/models`（2026-10-20 用户真机）：端点"活着"与"能出字"是两件事 ——
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
        local_state = "端点不在（`curl " + local_url() + "/models" + " 看一眼）"
    else:
        local_state = _state_text(**{k: v for k, v in probe_local(post=post).items()
                                     if k in ("state", "ms", "text", "detail")})
    lines = [f"模式：{mode()}",
             f"本机端点：{local_url()} → {local_state}",
             f"本机模型：{local_model()}（能读图？{'能' if reachable and local_vision(get=get) else '不能——只吃文本'}）",
             f"云端：{cloud['model']} @ {cloud['base_url'] or '（没配 base_url）'}"
             f"，凭据候选 {len([c for c in cloud_candidates() if c['api_key']])} 把"
             f"（{cloud['config_file']}）"]
    # 服务配置里那几个端点（2026-10-21 新增）：让人一眼看出"服务到底在哪"
    eps = translate_endpoints()
    if eps:
        lines.append(f"服务配置（{_config_note()}）：")
        for ep in eps:
            lines.append(f"  · {ep.name}（{ep.kind}/{ep.style}） {ep.base_url or '（没填地址）'}"
                         f" 模型 {ep.model or '—'}")
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