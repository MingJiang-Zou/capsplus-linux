# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""OCR（第二批 F4）：**本地优先**，云端（百度）可一键切。

两条后端：
  * `local` —— RapidOCR（PP-OCR + onnxruntime）跑在**独立 venv** 里，capsplus 用子进程调
    `capsplus/shot_ocr_local.py`。主进程一行重依赖都不加（截图工具不该因为装不上 onnx 就废掉）。
  * `cloud` —— 百度 OCR（`accurate_basic`，1000 次/月免费）。凭据读
    `~/.dsh/skills/ocr/.env`（`BAIDU_OCR_API_KEY` / `BAIDU_OCR_SECRET_KEY`），token 缓存在
    `$XDG_RUNTIME_DIR/capsplus-shot/baidu-token.json`（带过期时间，省一次往返）。

后端选择（`resolve_backend()` 现在是**配置驱动**的纯函数，判据直接调它）：
  `CAPSPLUS_OCR=local|cloud|auto`（默认 auto）**或配置文件里的端点名**。
  `auto` = 按**书写顺序**挨个试（配置里 local 在前就 local 优先）：本地探得到就用本地
  （离线、不花钱），探不到就用云端（要是连 key 都没有 → **明确说两边都不可用 + 怎么装**，
  绝不假装成功）。

**服务在哪现在是可配的**（2026-10-21，用户原话：「关于 capsplus 工具，我希望关于翻译 OCR 语音识别
的服务都是可以配置的，方便配置远程的服务或者是本地的」）：端点从 `~/.config/capsplus/services.toml`
读（底座是 `capsplus/services.py`，**本模块只读不改**）：

    [ocr.local]  base_url = "shot_ocr_local.py"   # 助手脚本路径（可指到别处）
                 python  = "~/…/ocr-venv/bin/python"  # 或 venv = "~/…/ocr-venv"
    [ocr.cloud]  base_url = "https://aip.baidubce.com/…"
                 env_file = "~/.dsh/skills/ocr/.env"  # 百度那两把钥匙
    [ocr.vlm]    style = "openai"                # 能读图的 OpenAI 兼容服务（**显式建了才走**）

    优先级（全仓口径）：**环境变量 > 配置文件 > 内置默认**。

返回给面板的永远是同一个 dict（见 `shot_annotate.normalize_result` 的约定）：
  `{"ok": bool, "text": str, "backend": "local"|"cloud", "ms": int, "note": str, "hint": str}"
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

try:                                    # 包内导入（正常路径）
    from . import services as S
except ImportError:                     # 直接当脚本跑（`python3 capsplus/shot_ocr.py`）
    import services as S

BACKEND_LOCAL = "local"
BACKEND_CLOUD = "cloud"
BACKENDS = (BACKEND_LOCAL, BACKEND_CLOUD)

MODE_ENV = "CAPSPLUS_OCR"                 # local / cloud / auto（默认 auto）
PYTHON_ENV = "CAPSPLUS_OCR_PYTHON"        # 本地 OCR 用哪个解释器（必须装了 rapidocr）
OCR_ENV_FILE = "CAPSPLUS_OCR_ENV"         # 百度凭据文件（默认 ~/.dsh/skills/ocr/.env）

# 本地 OCR venv 的候选位置（按顺序找第一个能用的）。**不写死用户名**：
#   ~ 展开成当前用户家目录，所以换个人装也是这几个位置。
LOCAL_VENV = "~/.local/share/capsplus/ocr-venv"      # 标准落点（`--install-local-ocr` 装这儿）
# 想放别处也行（`~/voice-svc/ocr-venv` 那种），但**别塞进语音服务自己的 venv** —— 那会换掉它的 numpy/onnx
LOCAL_PYTHON_CANDIDATES = (
    f"{LOCAL_VENV}/bin/python",
    "~/voice-svc/ocr-venv/bin/python",
) if True else ()
DEFAULT_OCR_ENV = "~/.dsh/skills/ocr/.env"

BAIDU_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
TIMEOUT_S = 15

# ------------------------------------------------------------------ 服务配置（2026-10-21 新增）
# 底座是 `capsplus/services.py`（Lead 写的那份，**本模块只读不改**）。
# 三个客户端各留一份薄封装（而不是新建一个共享模块）：`services.py` 是别人写的地基，
# 不该由我改；而共享模块就得新建文件 —— 那要动仓的结构，得先问人。
CONFIG_ENV = "CAPSPLUS_SERVICES_FILE"      # 指到别的 services.toml（多套配置 / 判据用）
DEFAULT_HELPER_NAME = "shot_ocr_local.py"  # 库给的默认助手脚本名（= 仓里那份）
DEFAULT_MODE = "auto"


def services_config_path(path=None) -> str:
    """服务配置文件的落点：入参 > `CAPSPLUS_SERVICES_FILE` > 默认落点。

    为什么给环境变量留这个口子：判据要拿**注入的 fixture 文件**跑（不许碰用户真配的那份）。
    """
    return path or os.environ.get(CONFIG_ENV) or str(S.SERVICES_PATH)


def load_config(path=None):
    """读服务配置。**读不了 / 语法错都不抛栈** —— `services.load_services()` 自己兜住了。"""
    return S.load_services(services_config_path(path))


def ocr_endpoints(cfg=None) -> list:
    """配置里的 OCR 端点。

    顺序 = **配置文件里的书写顺序**（`auto` 的优先级就是这个顺序）——
    这一条由 `services.load_services()` 保证（2026-10-21 Lead 复核时改成"文件里写过就完全
    以文件为准"，不再与内置默认合并），客户端**不再自己重排**（别造第二份真相）。
    文件里没写 `[ocr]` 那一段时，内置默认原样保留（local → cloud）。
    """
    cfg = cfg if cfg is not None else load_config()
    cap = cfg.get("ocr")
    return list(cap.endpoints.values()) if cap else []


def endpoint_by_name(name: str, cfg=None):
    """按名字取一个端点（找不到就是 None —— 调用方要给"怎么改"的提示）。"""
    for ep in ocr_endpoints(cfg):
        if ep.name == name:
            return ep
    return None


def _pick(env_value: str, config_value: str, default: str) -> str:
    """「谁说了算」的**优先级本体**：环境变量 > 配置文件 > 内置默认。

    单独抽成一个函数，是为了让判据能**反例具型**：把这里的顺序反过来，判据必须红
    （见 `tests/test_shot_ocr.py::TestCounterExamples`）。
    """
    return env_value or config_value or default


def endpoint_protocol(ep) -> str:
    """这个 OCR 端点用哪种协议：`script` / `baidu` / `openai`。

    怎么判（**为什么不只看 style**）：`services.default_services()` 给的云端那档 `style` 写的是
    `openai`，可它其实是**百度的 REST 协议**（`aip.baidubce.com`，不是 OpenAI 兼容）。
    所以：显式写 `protocol` 的听显式的；否则 `style=script` ⇒ 脚本；否则**地址里是百度的** ⇒ 百度；
    剩下的（自建地址）⇒ OpenAI 兼容。**默认行为不许变** —— 没配过的端点永远走原来那条路。
    """
    proto = str((ep.extra.get("protocol") if ep else "") or "").strip().lower()
    if proto:
        return proto
    if ep.style == S.STYLE_SCRIPT:
        return "script"
    if "aip.baidubce.com" in ep.base_url:
        return "baidu"
    return "openai"


def _script_endpoint(cfg=None):
    """跑本地 OCR 助手脚本的那个端点（`protocol=script` 或名字叫 local 的）。"""
    for ep in ocr_endpoints(cfg):
        if endpoint_protocol(ep) == "script":
            return ep
    return endpoint_by_name(BACKEND_LOCAL, cfg)


def _baidu_endpoint(cfg=None):
    """百度那一档的端点（`protocol=baidu` 或名字叫 cloud 的）。"""
    for ep in ocr_endpoints(cfg):
        if endpoint_protocol(ep) == "baidu":
            return ep
    return endpoint_by_name(BACKEND_CLOUD, cfg)


# ------------------------------------------------------------------ 纯逻辑（判据直接调）

def mode(cfg=None) -> str:
    """当前档位：**环境变量 > 配置文件 > 内置默认**（`auto`）。

    `CAPSPLUS_OCR` 可以是 `local` / `cloud` / `auto`，**也可以是配置里的端点名**
    （自建的远程端点，比如另一台机器上的视觉模型）。
    ⚠ OCR 这条链路的**内置默认与库默认一致**（都是 `auto`），所以直接取配置里的值就行 ——
    不像翻译那条（那边的库默认是 `auto`、本模块的默认是 `local`，得问"用户显式写了没有"）。
    """
    raw = (os.environ.get(MODE_ENV) or "").strip()
    env_value = raw if _known_mode(raw, cfg) else ""
    cap = (cfg if cfg is not None else load_config()).get("ocr")
    config_value = str(cap.mode if cap else "").strip()
    if not _known_mode(config_value, cfg):
        config_value = ""
    return _pick(env_value, config_value, DEFAULT_MODE)


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
    return any(ep.name == value for ep in ocr_endpoints(cfg))


def helper_path(cfg=None) -> Path:
    """本地 OCR 的**助手脚本**路径（配置可改；默认仓里那份）。

    配法：`[ocr.local] base_url = "/path/to/shot_ocr_local.py"`。
    内置默认就是 `shot_ocr_local.py`（= 仓里那份），所以只写文件名时按仓里那份算；
    写了别的路径就**照它走**（哪怕那个路径不存在 —— 那样报错才指得准，不许悄悄换回仓里那份）。
    """
    ep = _script_endpoint(cfg)
    raw = str(ep.base_url if ep else "").strip()
    if raw and raw != DEFAULT_HELPER_NAME:
        return Path(os.path.expanduser(raw))
    return Path(__file__).resolve().parent / "shot_ocr_local.py"


def parse_env_file(text: str) -> dict:
    """极简 .env 解析（`KEY=VALUE`、支持 `export `、忽略注释与引号）。不引第三方库。"""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not key:
            continue
        out[key] = value.strip().strip("'\"")
    return out


def _read_text(path: str) -> str:
    """读文件；读不到就空串（**不许抛栈**：配置文件缺失是日常状态，不是故障）。"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def ocr_credentials(env_file=None, cfg=None) -> dict:
    """百度 OCR 的两把钥匙：进程环境优先，其次**配置文件指定的 env_file**，最后老默认路径。

    老口径**不许丢**：`~/.dsh/skills/ocr/.env` 是 2026-10-19 以来一直在用的落点。
    显式给了路径（入参或 `CAPSPLUS_OCR_ENV`）就**只读那一个**（老行为）；没给就
    「配置里写的 → 老默认」依次找，**先找到的赢**（不许因为加了配置层就把老用法弄坏）。
    """
    ep = _baidu_endpoint(cfg)
    api_env_key = str(ep.env_key if ep else "").strip() or "BAIDU_OCR_API_KEY"
    secret_env_key = str((ep.extra.get("secret_env_key") if ep else "") or "").strip() \
        or "BAIDU_OCR_SECRET_KEY"
    secret_env_var = str((ep.extra.get("secret_key_env") if ep else "") or "").strip()

    explicit = env_file or os.environ.get(OCR_ENV_FILE)
    paths = []
    if explicit:
        paths = [os.path.expanduser(explicit)]
    else:
        for candidate in (str(ep.env_file if ep else "").strip(), DEFAULT_OCR_ENV):
            if not candidate:
                continue
            expanded = os.path.expanduser(candidate)
            if expanded not in paths:
                paths.append(expanded)
    values = {}
    used = ""
    for path in paths:                       # 先找到的赢（后面的不覆盖前面的）
        for key, value in parse_env_file(_read_text(path)).items():
            if key in values:
                continue
            values[key] = value
            if key in (api_env_key, secret_env_key) and not used:
                used = path              # 报"钥匙是从哪个文件读到的"，别让人找错地方
    return {"api_key": (os.environ.get("BAIDU_OCR_API_KEY") or os.environ.get(api_env_key)
                         or values.get(api_env_key, "")),
            "secret_key": (os.environ.get("BAIDU_OCR_SECRET_KEY")
                           or (os.environ.get(secret_env_var) if secret_env_var else "")
                           or values.get(secret_env_key, "")),
            "env_file": used or (paths[0] if paths else DEFAULT_OCR_ENV)}


def candidate_pythons(exists=None, cfg=None) -> list:
    """能用来跑本地 OCR 的解释器候选（只保留真的存在的）。

    顺序：**环境变量 > 配置文件 > 内置候选位置**（仓库口径：环境变量 > 配置文件 > 内置默认）。
    配置里可以写 `python = "/path/to/python"`（解释器全路径）或 `venv = "/path/to/venv"`
    （目录 ⇒ 取它下面的 `bin/python`）—— 装在别处的那份 rapidocr 也能用上了。
    """
    exists = exists or (lambda p: Path(p).exists())
    out = []
    override = os.environ.get(PYTHON_ENV)
    if override:
        out.append(override)
    ep = _script_endpoint(cfg)
    for key in ("python", "venv"):
        value = str((ep.extra.get(key) if ep else "") or "").strip()
        if not value:
            continue
        value = os.path.expanduser(value)
        if key == "venv":
            value = str(Path(value) / "bin" / "python")
        out.append(value)
    out.extend(os.path.expanduser(p) for p in LOCAL_PYTHON_CANDIDATES)
    return [p for p in out if exists(p)]


def local_available(python, runner=None) -> bool:
    """这个解释器**装了 rapidocr** 吗。

    用 `find_spec` 而不是 `import`：只想确认装没装，不想真把 onnxruntime 拉起来（那是秒级开销）。
    """
    runner = runner or default_runner
    code = ("import importlib.util as u, sys;"
            "sys.exit(0 if u.find_spec('rapidocr_onnxruntime') else 1)")
    try:
        done = runner([python, "-c", code], 20)
    except Exception:
        return False
    return getattr(done, "returncode", 1) == 0


def default_local_probe(exists=None, runner=None):
    """真探一次：第一个装了 rapidocr 的解释器；没有就 None。

    ⚠ 别拿 `lambda: None` 当默认（我 2026-10-19 就这么写过）：那样 `auto`/`local` 永远探不到本地，
    而状态命令因为自己传了探针又显示得好好的 —— 一边"本地可用"一边"按 F4 报没有后端"。
    """
    for python in candidate_pythons(exists=exists):
        if local_available(python, runner=runner):
            return python
    return None


def _endpoint_ready(ep, *, creds=None, local_probe=None, strict=False) -> tuple:
    """这个 OCR 端点现在能不能用？返回 `(能用, 说明)`。

    `strict=True` = 用户**点名**要这个端点（不许静默换别的，只如实说缺什么）；
    `strict=False` = `auto` 挨个试（说清为什么不行，好让下一条接着试）。
    """
    proto = endpoint_protocol(ep)
    if proto == "script":
        probe = local_probe or default_local_probe
        try:
            python = probe()
        except Exception:
            python = None
        if python:
            return True, f"本地 OCR（{python}）"
        return False, ("✗ 探不到装了 rapidocr 的解释器 —— "
                       f"装一份：`python3 -m venv ~/.local/share/capsplus/ocr-venv && "
                       f"~/.local/share/capsplus/ocr-venv/bin/pip install rapidocr-onnxruntime`"
                       f"（或用 {PYTHON_ENV} 指一个，或在服务配置的 `[ocr.local]` 里写 "
                       f"python = / venv =）")
    if proto == "baidu":
        if creds.get("api_key") and creds.get("secret_key"):
            label = "百度 OCR（accurate_basic，1000 次/月免费）"
            return True, (label if strict
                          else label + "；前面的端点不行才走这条")
        where = creds.get("env_file")
        if strict:
            return False, f"✗ 你指定了云 OCR，但 {where} 里没有百度凭据"
        return False, f"✗ 云端也没凭据（{where} 里缺 BAIDU_OCR_API_KEY/SECRET_KEY）"
    # openai 兼容的视觉端点：地址得有；key 可选（有的本机服务不收 key）
    if not ep.base_url:
        return False, (f"✗ 端点 {ep.name} 没填 base_url —— 在 {services_config_path()} 的 "
                       f"`[ocr.{ep.name}]` 里补上")
    return True, f"视觉端点 {ep.name}（{ep.model or '未指定模型'}）"


def resolve_backend(local_probe=None, creds=None, cfg=None) -> tuple:
    """返回 `(backend | None, 说明)`。三方都不可用时给**可操作**的说明。

    **现在是配置驱动的**（2026-10-21）：后端 = 配置里的端点名；`auto` 按**书写顺序**挨个试
    （配置里 local 在前就 local 优先）。
    """
    mode_ = mode(cfg)
    creds = creds if creds is not None else ocr_credentials(cfg=cfg)
    local_probe = local_probe or default_local_probe
    endpoints = ocr_endpoints(cfg)

    # 环境变量写了个**认不出来的档位**（多半是端点名打错了）：必须**说出来**，
    # 不许静默退回默认 —— 用户以为在用 nas，其实在用本地那档。
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
        ok, why = _endpoint_ready(ep, creds=creds, local_probe=local_probe, strict=True)
        return (ep.name, why) if ok else (None, why)

    reasons = []
    for ep in endpoints:
        ok, why = _endpoint_ready(ep, creds=creds, local_probe=local_probe, strict=False)
        if ok:
            return ep.name, why
        reasons.append(why)
    # 每条原因自带"✗"前缀，串起来只留一个（免得出现"✗ ✗ …"）
    return None, ("✗ " + "；".join(r.lstrip("✗ ").strip() for r in reasons))


def parse_helper_output(stdout: str) -> dict:
    """助手脚本的 stdout → dict（**认最后一行 JSON**：前面可能有引擎自己的日志）。"""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"ok": False, "hint": "✗ OCR 助手没吐出可解析的结果（看 stdout/stderr）"}


def make_result(ok: bool, *, text="", backend="", ms=0, note="", hint="") -> dict:
    return {"ok": bool(ok), "text": text, "backend": backend, "ms": int(ms), "note": note,
            "hint": hint}


def default_runner(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def default_post(url, data: bytes, headers: dict, timeout: int = TIMEOUT_S) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ------------------------------------------------------------------ 两条后端

def ocr_local(png_path, python=None, runner=None, engine_note="") -> dict:
    """跑本地 OCR 子进程。返回面板用的 dict（失败也带一句可操作的话）。"""
    runner = runner or default_runner
    python = python or (candidate_pythons() or [None])[0]
    if not python:
        return make_result(False, backend=BACKEND_LOCAL,
                           hint="✗ 没找到装了 rapidocr 的解释器（`CAPSPLUS_OCR_PYTHON` 可指定）")
    argv = [python, str(helper_path()), str(png_path)]
    t0 = time.perf_counter()
    try:
        done = runner(argv, 120)
    except subprocess.TimeoutExpired:
        return make_result(False, backend=BACKEND_LOCAL, ms=int((time.perf_counter() - t0) * 1000),
                           hint="✗ 本地 OCR 超时（120s）—— 第一次跑要下模型，检查网络后重试")
    except OSError as exc:
        return make_result(False, backend=BACKEND_LOCAL, hint=f"✗ 起不了本地 OCR：{exc}")
    parsed = parse_helper_output(getattr(done, "stdout", "") or "")
    if not parsed.get("ok"):
        detail = (parsed.get("hint") or (getattr(done, "stderr", "") or "").strip()[-200:]
                  or "没给原因")
        return make_result(False, backend=BACKEND_LOCAL, hint=f"✗ 本地 OCR 失败：{detail}")
    # note 里带上**跑的是哪一份**：助手脚本与 venv 解释器都可配了（2026-10-21），
    # 装了两份 rapidocr 时，用户得能从面板上看出来用的是哪个。
    note = parsed.get("engine") or engine_note or "rapidocr"
    which = parsed.get("python") or python
    if which:
        note = f"{note}（{which}）"
    return make_result(True, text=parsed.get("text", ""), backend=BACKEND_LOCAL,
                       ms=int(parsed.get("ms") or (time.perf_counter() - t0) * 1000),
                       note=note)


def baidu_token(creds, post=None, cache_path=None, now=None) -> tuple:
    """取百度 token（带缓存）。返回 `(token, 说明)`。"""
    post = post or default_post
    now = now or time.time
    cache = Path(cache_path) if cache_path else (runtime_dir() / "baidu-token.json")
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("expires_at", 0) > now() + 60 and cached.get("token"):
            return cached["token"], "缓存"
    except (OSError, ValueError):
        pass
    query = urllib.parse.urlencode({"grant_type": "client_credentials",
                                    "client_id": creds.get("api_key", ""),
                                    "client_secret": creds.get("secret_key", "")})
    url = f"{BAIDU_TOKEN_URL}?{query}"
    raw = post(url, b"", {"Content-Type": "application/x-www-form-urlencoded"})
    payload = json.loads(raw.decode("utf-8"))
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(payload.get("error_description") or payload.get("error") or "拿不到 token")
    expires = int(payload.get("expires_in") or 2592000)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"token": token, "expires_at": now() + expires}),
                         encoding="utf-8")
        cache.chmod(0o600)
    except OSError:
        pass
    return token, "新取"


def ocr_cloud(png_path, creds=None, post=None, cache_path=None, now=None) -> dict:
    """百度 OCR（accurate_basic）。"""
    post = post or default_post
    creds = creds if creds is not None else ocr_credentials()
    t0 = time.perf_counter()
    if not (creds.get("api_key") and creds.get("secret_key")):
        return make_result(False, backend=BACKEND_CLOUD,
                           hint=f"✗ 云 OCR 没凭据：{creds.get('env_file')} 里缺 "
                                "BAIDU_OCR_API_KEY / BAIDU_OCR_SECRET_KEY")
    try:
        token, how = baidu_token(creds, post=post, cache_path=cache_path, now=now)
    except Exception as exc:
        return make_result(False, backend=BACKEND_CLOUD,
                           hint=f"✗ 取百度 token 失败：{exc}（检查网络或凭据）")
    try:
        image = base64.b64encode(Path(png_path).read_bytes())
    except OSError as exc:
        return make_result(False, backend=BACKEND_CLOUD, hint=f"✗ 读不了 {png_path}：{exc}")
    body = urllib.parse.urlencode({"image": image.decode("ascii")}).encode("ascii")
    try:
        raw = post(f"{BAIDU_OCR_URL}?access_token={token}", body,
                   {"Content-Type": "application/x-www-form-urlencoded"})
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return make_result(False, backend=BACKEND_CLOUD,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 百度 OCR 调用失败：{exc}（没网？额度用完了？）")
    ms = int((time.perf_counter() - t0) * 1000)
    if payload.get("error_code"):
        return make_result(False, backend=BACKEND_CLOUD, ms=ms,
                           hint=f"✗ 百度 OCR 报错 {payload.get('error_code')}："
                                f"{payload.get('error_msg')}")
    words = payload.get("words_result") or []
    text = "\n".join(w.get("words", "") for w in words)
    return make_result(True, text=text, backend=BACKEND_CLOUD, ms=ms,
                       note=f"baidu accurate_basic · token {how} · {len(words)} 行")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    return Path(base) / "capsplus-shot"


OCR_VISION_PROMPT = "把这张图里的全部文字原样抄出来。只输出识别到的文字，不要解释。"


def ocr_vision(png_path, ep, *, post=None, timeout=None) -> dict:
    """把图发给**能读图的 OpenAI 兼容服务**（配置里显式建了这种端点才走这条路）。

    为什么"只在显式建了才走"（2026-10-21）：**默认行为不许变** —— 本机那档实测**只吃文本**
    （`/v1/models` 里 `modalities.vision=false`，2026-10-19 实测），云端那档是百度的 REST 协议，
    两条都不是这个形状。只有用户在服务配置里显式写了一个 OpenAI 兼容地址，才当它是视觉端点。
    """
    post = post or default_post
    if not ep.base_url:
        return make_result(False, backend=ep.name,
                           hint=f"✗ 端点 {ep.name} 没填 base_url（服务配置的 "
                                f"`[ocr.{ep.name}]`）")
    key, _source = ep.resolved_api_key()
    try:
        image = base64.b64encode(Path(png_path).read_bytes()).decode("ascii")
    except OSError as exc:
        return make_result(False, backend=ep.name, hint=f"✗ 读不了 {png_path}：{exc}")
    body = {"model": ep.model or "vision", "temperature": 0, "stream": False,
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": OCR_VISION_PROMPT},
                                      {"type": "image_url",
                                       "image_url": {"url": f"data:image/png;base64,{image}"}}]}]}
    url = f"{ep.base_url.rstrip('/')}/chat/completions"
    t0 = time.perf_counter()
    try:
        raw = post(url, json.dumps(body).encode("utf-8"),
                   {"Content-Type": "application/json",
                    "Authorization": f"Bearer {key or 'not-needed'}"},
                   timeout or TIMEOUT_S)
    except Exception as exc:
        return make_result(False, backend=ep.name,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 视觉端点 {ep.name} 没应答：{exc}（{url}）—— "
                                "确认那台机器上的服务在跑、这个模型**能读图**")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return make_result(False, backend=ep.name,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 视觉端点 {ep.name} 回的不是 JSON：{exc}")
    if not isinstance(payload, dict):
        return make_result(False, backend=ep.name,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint="✗ 视觉端点回的不是对象")
    if payload.get("error"):
        err = payload["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        return make_result(False, backend=ep.name,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint=f"✗ 视觉端点报错：{message}")
    choices = payload.get("choices") or []
    content = ""
    if choices:
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
    if not content:
        return make_result(False, backend=ep.name,
                           ms=int((time.perf_counter() - t0) * 1000),
                           hint="✗ 视觉端点回了空内容 —— 这个模型多半**不读图**"
                                "（本机那档实测 modalities.vision=false）；换一个能读图的模型")
    return make_result(True, text=str(content).strip(), backend=ep.name,
                       ms=int((time.perf_counter() - t0) * 1000),
                       note=f"{ep.model or '未指定模型'}（视觉端点 {ep.name}）")


def ocr(png_path, backend=None, *, python=None, runner=None, creds=None, post=None,
        local_probe=None, cfg=None) -> dict:
    """统一入口：按后端跑一次 OCR，返回面板用的 dict。

    `backend` 可以是 `local` / `cloud`，**也可以是配置里的端点名**（自建远程端点）。
    """
    note = ""
    if backend is None:
        backend, note = resolve_backend(local_probe=local_probe, creds=creds, cfg=cfg)
        if backend is None:
            return make_result(False, hint=note)
    ep = endpoint_by_name(backend, cfg)
    if ep is not None:
        proto = endpoint_protocol(ep)
    else:
        # 配置里没这个端点（老用法：直接点名 local/cloud）⇒ 走老协议
        proto = ("script" if backend == BACKEND_LOCAL
                 else "baidu" if backend == BACKEND_CLOUD else "")
    if proto == "script":
        result = ocr_local(png_path, python=python, runner=runner)
        if note and result["ok"] and not result["note"]:
            result["note"] = note
        return result
    if proto == "baidu":
        return ocr_cloud(png_path, creds=creds, post=post)
    if proto == "openai":
        return ocr_vision(png_path, ep, post=post)
    return make_result(False, hint=f"✗ 不认识的 OCR 后端：{backend}")


PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"   # 本机直连 pypi 只有 ~80 kB/s（实测）


def install_local_plan(mirror: bool = True) -> dict:
    """「一条命令装本地 OCR」的计划（纯函数，判据直接断言 argv）。

    实测成本（2026-10-19）：下载 **136 MB**、装完占盘 **392 MB**、模型是**随包带的**
    （不用首次下载）；走清华镜像 ~40 秒，直连 pypi ~25 分钟（79 kB/s）。
    """
    venv = os.path.expanduser(LOCAL_VENV)
    pip = f"{venv}/bin/pip"
    argv = [pip, "install", "--no-input"]
    if mirror:
        argv += ["-i", PIP_MIRROR]
    argv.append("rapidocr-onnxruntime")
    return {"venv": venv, "python": f"{venv}/bin/python", "pip": pip, "argv": argv,
            "steps": [["python3", "-m", "venv", venv], argv]}


def install_local(runner=None, mirror: bool = True) -> tuple:
    """真装（`--install-local-ocr`）。返回 `(ok, 说明)`；已经装好就直接说清楚。"""
    runner = runner or default_runner
    plan = install_local_plan(mirror=mirror)
    python = plan["python"]
    if Path(python).exists() and local_available(python, runner=runner):
        return True, f"✓ 本地 OCR 已经装好了：{python}（不用重装）"
    messages = []
    try:
        if not Path(python).exists():
            done = runner(plan["steps"][0], 600)
            messages.append(f"建 venv rc={getattr(done, 'returncode', '?')}")
        done = runner(plan["argv"], 1800)
        if getattr(done, "returncode", 1) != 0:
            detail = (getattr(done, "stderr", "") or "").strip()[-300:]
            return False, f"✗ 装失败（rc={getattr(done, 'returncode', '?')}）：{detail}"
    except Exception as exc:
        return False, f"✗ 装本地 OCR 出错：{exc}"
    ok = local_available(python, runner=runner)
    note = "；".join(messages)
    return ok, (f"✓ 装好了：{python}（占盘约 392MB，模型随包带）{('｜' + note) if note else ''}"
                if ok else f"✗ 装完但 import 不到 rapidocr：{python}")


def describe() -> str:
    """一页现状（`capsplus-shot --ocr-status` 与排查用）。"""
    pythons = candidate_pythons()
    creds = ocr_credentials()
    backend, why = resolve_backend(local_probe=default_local_probe, creds=creds)
    lines = [f"模式：{mode()}", f"结论：{backend or '不可用'} —— {why}",
             f"本地候选解释器：{pythons or '（一个都没有）'}",
             f"助手脚本：{helper_path()}",
             f"百度凭据：{'有' if creds['api_key'] and creds['secret_key'] else '没有'}"
             f"（{creds['env_file']}）"]
    # 服务配置里那几个端点（2026-10-21 新增）：让人一眼看出"服务到底在哪"
    eps = ocr_endpoints()
    if eps:
        lines.append(f"服务配置（{_config_note()}）：")
        for ep in eps:
            lines.append(f"  · {ep.name}（{ep.kind}/{ep.style}/{endpoint_protocol(ep)}）"
                         f" {ep.base_url or '（没填地址）'} 模型 {ep.model or '—'}")
    return "\n".join(lines)


if __name__ == "__main__":                                    # pragma: no cover - 手查用
    print(describe())
    sys.exit(0)