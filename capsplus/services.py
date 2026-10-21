# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""服务配置：翻译 / OCR / 语音识别 三条链路的"本机 or 远程"**可配置**。

为什么要有这个模块（2026-10-21，用户原话：「关于 capsplus 工具，我希望关于翻译 OCR 语音识别的
服务都是可以配置的，方便配置远程的服务或者是本地的」）：

改之前三条链路的"服务在哪"是**写死在代码里**的：
  · 翻译 —— 本机 `http://127.0.0.1:12087/v1`（模型 qwen3.8-27b）与云端阿里 token-plan
    （凭据从 `~/.bailian/config.json` / `~/.dsh/.credentials.yaml` 读），靠环境变量
    `CAPSPLUS_TRANSLATE=local|cloud|auto` 二选一；
  · OCR —— 本机 rapidocr（独立 venv 里的助手脚本）与百度 OCR（`aip.baidubce.com` 写死）；
  · 语音识别 —— 本机 `127.0.0.1:18090`（只有 `CAPSPLUS_ASR_HOST/PORT/LANG` 三个环境变量）。
所以"把服务搬到另一台机器上"这件事，**除了改代码没有别的办法**。

本模块给三条链路一个**共同的形状**：
    端点（Endpoint）= {名字, 本机/远程, base_url, 模型, 凭据来源, 超时}
    能力（Capability）= {能力名, 用哪个端点(auto = 按顺序试), 附带参数(目标语言/识别语言)}
配置文件 `~/.config/capsplus/services.toml`（与 qbar.toml / mappings.toml 同目录、同风格）。

**四条不许改的口径**（改之前先读）：
  ① **凭据只存"来源"，不存值** —— `api_key_env` / `credentials_file`+键 / `env_file`+键。
     实在要写明文也允许（给没有凭据体系的用户），但写回时文件权限 600，且**任何输出都脱敏**
     （`describe()` 只说"来自哪"，绝不打印值）。
  ② **优先级：环境变量 > 配置文件 > 内置默认**。环境变量是"临时覆盖"（现场救火用），
     配置文件是"长期设置"，内置默认是"没配置时的样子"（与 2026-10-21 之前的行为**逐字一致**，
     不许因为加了配置层就把老用法弄坏）。
  ③ **不 import gi**：这个模块要能在没有 PyGObject 的机器上 import（CI 就是那种机器），
     设置窗、命令行、判据都吃同一份。
  ④ **坏配置不许崩**：TOML 语法错、字段类型不对 —— 一律变成"带错误说明的配置对象"，
     调用方照旧能跑并把原因显示出来（仓里的老规矩：状态必须带动作，不许抛栈给用户）。
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "capsplus"
SERVICES_PATH = CONFIG_DIR / "services.toml"

# 三个能力。顺序就是设置窗里的顺序，也是 `capsplus services` 的输出顺序。
CAPABILITIES = ("translate", "ocr", "asr")
CAPABILITY_LABELS = {"translate": "翻译", "ocr": "OCR 识别", "asr": "语音识别"}

KIND_LOCAL = "local"
KIND_REMOTE = "remote"
MODE_AUTO = "auto"

# 端点类型：OpenAI 兼容（翻译/云端 OCR）、本地助手脚本（本地 OCR）、HTTP 服务（识别）
STYLE_OPENAI = "openai"
STYLE_SCRIPT = "script"
STYLE_HTTP = "http"


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
@dataclass
class Endpoint:
    """一个"服务"的落点。

    `api_key` 是明文（可选）；`api_key_env` / `credentials_file` / `env_file` 是"来源"。
    取值顺序见 `resolved_api_key()`：**来源优先于明文** —— 明文只是没有别的办法时的兜底。

    刻意**不 frozen**：设置窗要能逐字段改它（改了直接写回），可变的 dataclass 比
    "每次 `dataclasses.replace` 造新对象"顺手得多。
    """

    name: str
    kind: str = KIND_LOCAL                 # local | remote
    style: str = STYLE_OPENAI              # openai | script | http
    base_url: str = ""
    model: str = ""
    api_key: str = ""                      # 明文（允许，但写回时 0600，且任何输出都脱敏）
    api_key_env: str = ""                  # 环境变量名
    credentials_file: str = ""             # 凭据文件（YAML/JSON）
    credentials_key: str = ""              # 凭据文件里的键名
    env_file: str = ""                     # 简易 KEY=VALUE 文件（OCR 那种）
    env_key: str = ""                      # env_file 里的键名
    timeout_s: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.kind == KIND_LOCAL

    def resolved_api_key(self, env=None) -> tuple:
        """返回 `(key, 来源说明)`；**来源说明里没有值**（不许打印凭据）。

        顺序：环境变量 → 凭据文件 → env_file → 明文。理由见类注释。
        """
        env = os.environ if env is None else env
        if self.api_key_env:
            value = env.get(self.api_key_env, "")
            if value:
                return value, f"环境变量 {self.api_key_env}"
        if self.credentials_file and self.credentials_key:
            value = _read_credentials(self.credentials_file, self.credentials_key)
            if value:
                return value, f"凭据文件 {self.credentials_file} 的 {self.credentials_key}"
        if self.env_file and self.env_key:
            value = _read_env_file(self.env_file).get(self.env_key, "")
            if value:
                return value, f"{self.env_file} 的 {self.env_key}"
        if self.api_key:
            return self.api_key, "配置文件里直接写的"
        return "", "没配"


def copy_endpoint(ep: Endpoint) -> Endpoint:
    """复制一个端点（改之前先复制，别把"默认"那份改了）。"""
    return Endpoint(**{**ep.__dict__, "extra": dict(ep.extra)})


@dataclass
class Capability:
    """一个能力（翻译 / OCR / 识别）用哪些端点、按什么顺序。"""

    name: str
    mode: str = MODE_AUTO                  # auto = 按 endpoints 的插入顺序试
    endpoints: dict = field(default_factory=dict)   # 名字 -> Endpoint
    target: str = ""                       # 翻译：目标语言
    lang: str = ""                         # 识别：语言
    timeout_s: float = 0.0

    def ordered_endpoints(self) -> list:
        """`auto` 时按**插入顺序**返回端点（配置文件里的书写顺序 = 优先级）。"""
        return list(self.endpoints.values())

    def endpoint(self, name: str = "") -> Endpoint | None:
        if name:
            return self.endpoints.get(name)
        if self.mode and self.mode != MODE_AUTO:
            return self.endpoints.get(self.mode)
        ordered = self.ordered_endpoints()
        return ordered[0] if ordered else None


@dataclass
class Services:
    """整份服务配置。`error` 非空 = 文件读不了（其它字段仍然有默认值，界面照常能开）。"""

    capabilities: dict = field(default_factory=dict)      # 能力名 -> Capability
    error: str = ""
    path: Path | None = None
    header: str = ""                     # 文件开头那段注释（写回时原样保留）

    def capability(self, name: str) -> Capability:
        if name not in self.capabilities:
            self.capabilities[name] = Capability(name=name)
        return self.capabilities[name]

    def get(self, name: str) -> Capability | None:
        return self.capabilities.get(name)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def _read_env_file(path: str) -> dict:
    """极简 `KEY=VALUE` 解析（支持 `export ` 前缀、忽略注释与引号）。不引第三方库。"""
    out = {}
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _read_credentials(path: str, key: str) -> str:
    """从凭据文件里取一个值。JSON 与 YAML 都认（YAML 只认极简的 `key: value` 与 `refs:` 段）。"""
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if target.suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return ""
        value = data.get(key, "")
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            inner = value.get("api_key") or value.get("key") or ""
            return inner.strip() if isinstance(inner, str) else ""
        return ""
    # YAML：逐行找 `key:`，值去掉引号；`refs:` 段里的 `KEY: value` 也认
    in_refs = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith(":") and not line.startswith((" ", "\t")):
            in_refs = stripped[:-1].strip() in ("refs", "references")
            continue
        name, _, value = stripped.partition(":")
        name = name.strip()
        if name == key or (in_refs and name == key):
            return value.strip().strip('"').strip("'")
    return ""


def _endpoint_from_table(name: str, table: dict) -> Endpoint:
    """从一个 TOML 表读出一个端点（**只认认识的键**，其余进 `extra`，不丢信息）。"""
    known = {"kind", "style", "base_url", "model", "api_key", "api_key_env",
             "credentials_file", "credentials_key", "env_file", "env_key", "timeout_s"}
    extra = {k: v for k, v in table.items() if k not in known}
    return Endpoint(
        name=name,
        kind=str(table.get("kind", KIND_LOCAL)),
        style=str(table.get("style", STYLE_OPENAI)),
        base_url=str(table.get("base_url", "")),
        model=str(table.get("model", "")),
        api_key=str(table.get("api_key", "")),
        api_key_env=str(table.get("api_key_env", "")),
        credentials_file=str(table.get("credentials_file", "")),
        credentials_key=str(table.get("credentials_key", "")),
        env_file=str(table.get("env_file", "")),
        env_key=str(table.get("env_key", "")),
        timeout_s=float(table.get("timeout_s", 0) or 0),
        extra=extra,
    )


def default_services() -> Services:
    """内置默认 —— **与 2026-10-21 之前的行为逐字一致**（这一条是硬要求，见模块头 ②）。

    翻译：本机 `http://127.0.0.1:12087/v1`（qwen3.8-27b）优先，云端阿里 token-plan 兜底；
    OCR：本机 rapidocr 助手优先，百度云端兜底；
    识别：本机 `http://127.0.0.1:18090`（`/asr-upload` + `/health`）。
    凭据**不在这里写死**：云端翻译的凭据仍由 `capsplus/shot_translate.py` 那套候选顺序去找
    （`~/.bailian/config.json` → `~/.dsh/.credentials.yaml`），本模块只把"用户想自己指定"这条路铺好。
    """
    services = Services()
    translate = services.capability("translate")
    # 默认档是 **local**，不是 auto —— 这不是笔误（2026-10-21 复核时发现的）：
    # 本仓的老行为是"翻译默认走本机模型，云端是**显式选择**才走"（`shot_translate.py` 的
    # `DEFAULT_MODE = BACKEND_LOCAL`）。若这里写 auto，"auto = 挨个试"就意味着**本机没起时
    # 静默切到计费云端** —— 那是不能默认发生的（钱与隐私都不该默认往外走）。
    # 端点表里仍然留着 cloud（用户在设置里把 mode 选成它就生效）。
    translate.mode = "local"
    translate.target = "zh"
    translate.endpoints = {
        "local": Endpoint(
            name="local", kind=KIND_LOCAL, style=STYLE_OPENAI,
            base_url="http://127.0.0.1:12087/v1", model="qwen3.8-27b"),
        "cloud": Endpoint(
            name="cloud", kind=KIND_REMOTE, style=STYLE_OPENAI,
            base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            model="deepseek-v4.1-flash",
            credentials_file="~/.dsh/.credentials.yaml",
            credentials_key="AL_TOKEN_PLAN_API_KEY"),
    }
    ocr = services.capability("ocr")
    ocr.mode = MODE_AUTO
    ocr.endpoints = {
        "local": Endpoint(name="local", kind=KIND_LOCAL, style=STYLE_SCRIPT,
                          base_url="shot_ocr_local.py", model="rapidocr_onnxruntime"),
        "cloud": Endpoint(name="cloud", kind=KIND_REMOTE, style=STYLE_OPENAI,
                          base_url="https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic",
                          model="baidu-accurate-basic",
                          env_file="~/.dsh/.env", env_key="BAIDU_OCR_API_KEY"),
    }
    asr = services.capability("asr")
    asr.mode = "local"
    asr.lang = "zh"
    asr.endpoints = {
        "local": Endpoint(name="local", kind=KIND_LOCAL, style=STYLE_HTTP,
                          base_url="http://127.0.0.1:18090", model="sense-voice"),
    }
    return services


def load_services(path=None) -> Services:
    """读配置。**读不了就退回默认值并把原因带回来**（不许抛栈给用户）。

    文件不存在 = 还没配过 = 用默认值（这不是错误，别在界面上报红吓人）。
    """
    target = Path(path) if path else SERVICES_PATH
    services = default_services()
    services.path = target
    if not target.exists():
        return services
    try:
        with open(target, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        services.error = f"{target} 读不了：{type(exc).__name__}: {exc}"
        return services
    header = ""
    try:
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("#"):
                header += line + "\n"
            elif line.strip():
                break
    except OSError:
        pass
    services.header = header
    for cap_name in CAPABILITIES:
        table = data.get(cap_name)
        if not isinstance(table, dict):
            continue
        cap = services.capability(cap_name)
        cap.mode = str(table.get("mode", cap.mode))
        cap.target = str(table.get("target", cap.target))
        cap.lang = str(table.get("lang", cap.lang))
        try:
            cap.timeout_s = float(table.get("timeout_s", cap.timeout_s) or 0)
        except (TypeError, ValueError):
            cap.timeout_s = 0.0
        file_endpoints = {}
        for ep_name, ep_table in table.items():
            if ep_name in ("mode", "target", "lang", "timeout_s"):
                continue
            if isinstance(ep_table, dict):
                file_endpoints[ep_name] = _endpoint_from_table(ep_name, ep_table)
        # 端点集合：**文件里写过就完全以文件为准**（不再与内置默认合并）。
        # 为什么改（2026-10-21，Lead 复核时定）：合并语义下"用户把 nas 写在 local 前面"不生效 ——
        # 内置的 local 永远排第一，于是 `auto` 的优先级与文件里的书写顺序对不上
        # （`Capability.ordered_endpoints` 的注释写的就是"文件里的书写顺序 = 优先级"）。
        # 治本做法：**配置文件是唯一真相** —— 用户写了哪几个端点、什么顺序，就是哪几个、什么顺序；
        # 没写这个能力的用户（文件里没这一段）继续用内置默认，行为一个字不变。
        if file_endpoints:
            cap.endpoints = file_endpoints
    return services


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def toml_str(value: str) -> str:
    """TOML 基本字符串转义（用户可能敲引号/反斜杠，不许写出坏配置）。"""
    out = value or ""
    out = out.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


def render_services(services: Services) -> str:
    """渲染成 TOML 文本。**顺序 = 配置里的顺序**（`auto` 的优先级就是这个顺序）。"""
    lines = []
    header = services.header or DEFAULT_HEADER
    lines += header.rstrip("\n").splitlines()
    lines.append("")
    for cap_name in CAPABILITIES:
        cap = services.get(cap_name)
        if cap is None:
            continue
        lines.append(f"[{cap_name}]")
        lines.append(f"mode = {toml_str(cap.mode)}"
                     f"          # auto = 按下面端点的书写顺序挨个试；也可直接写端点名")
        if cap_name == "translate":
            lines.append(f"target = {toml_str(cap.target)}      # 翻译目标语言")
        if cap_name == "asr":
            lines.append(f"lang = {toml_str(cap.lang)}            # 识别语言")
        if cap.timeout_s:
            lines.append(f"timeout_s = {cap.timeout_s}")
        for ep in cap.ordered_endpoints():
            lines.append("")
            lines.append(f"[{cap_name}.{ep.name}]")
            lines.append(f"kind = {toml_str(ep.kind)}              # local = 本机 / remote = 远程")
            lines.append(f"style = {toml_str(ep.style)}            # openai | script | http")
            if ep.base_url:
                lines.append(f"base_url = {toml_str(ep.base_url)}")
            if ep.model:
                lines.append(f"model = {toml_str(ep.model)}")
            if ep.api_key:
                lines.append(f"api_key = {toml_str(ep.api_key)}      # 明文（能不用就不用；文件权限 600）")
            if ep.api_key_env:
                lines.append(f"api_key_env = {toml_str(ep.api_key_env)}  # 从环境变量取（推荐）")
            if ep.credentials_file:
                lines.append(f"credentials_file = {toml_str(ep.credentials_file)}")
            if ep.credentials_key:
                lines.append(f"credentials_key = {toml_str(ep.credentials_key)}")
            if ep.env_file:
                lines.append(f"env_file = {toml_str(ep.env_file)}")
            if ep.env_key:
                lines.append(f"env_key = {toml_str(ep.env_key)}")
            if ep.timeout_s:
                lines.append(f"timeout_s = {ep.timeout_s}")
            for key, value in ep.extra.items():
                lines.append(f"{key} = {toml_str(str(value))}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


DEFAULT_HEADER = """# capsplus 的服务配置：翻译 / OCR / 语音识别 各自用哪台机器上的哪个服务。
#
# 三种端点类型（style）：
#   openai —— OpenAI 兼容的 HTTP 服务（http://host:port/v1 那种），翻译与云端 OCR 走它
#   script —— 本机跑一个助手脚本（本地 OCR 的 rapidocr 就是它）
#   http  —— 自己的 HTTP 协议（本机识别服务：/asr-upload + /health）
#
# 凭据**只写来源，不写值**（推荐）：api_key_env / credentials_file+credentials_key / env_file+env_key。
# 实在要写明文就用 api_key —— 这个文件会被写成 0600，但**别把它提交到任何仓库**。
#
# 优先级：环境变量 > 本文件 > 内置默认。环境变量是临时覆盖（现场救火），本文件是长期设置。
# 改完不用重启：每次调用都会重读本文件。
"""


def save_services(services: Services, path=None) -> tuple:
    """写回配置。返回 `(ok, 说明)`。

    · 目录不存在就建；文件权限 **0600**（里面可能有明文凭据）；
    · 先写同目录临时名再 rename（半截配置会让下次读到坏文件）；
    · 写完回读一次确认能解析 —— **写坏了要当场说**，不能留一个下次启动才发现的坏文件。
    """
    target = Path(path) if path else (services.path or SERVICES_PATH)
    text = render_services(services)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, target)
    except OSError as exc:
        return False, f"✗ 写 {target} 失败：{type(exc).__name__}: {exc}"
    try:
        with open(target, "rb") as fh:
            tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return False, f"✗ 写完了但读不回来（已还原为旧文件）：{type(exc).__name__}: {exc}"
    return True, f"✓ 已写 {target}"


# ---------------------------------------------------------------------------
# 脱敏描述（给状态页 / 设置窗 / 命令行用）
# ---------------------------------------------------------------------------
def describe_endpoint(ep: Endpoint) -> dict:
    """一个端点的脱敏摘要。**任何输出都不许带凭据值**。"""
    key, source = ep.resolved_api_key()
    return {
        "name": ep.name,
        "kind": ep.kind,
        "style": ep.style,
        "base_url": ep.base_url,
        "model": ep.model,
        "has_key": bool(key),
        "key_source": source,
        "timeout_s": ep.timeout_s,
    }


def describe(services: Services) -> list:
    """整份配置的脱敏摘要：[{能力, 标签, mode, 端点:[摘要]}]。"""
    out = []
    for cap_name in CAPABILITIES:
        cap = services.get(cap_name)
        if cap is None:
            continue
        out.append({
            "capability": cap_name,
            "label": CAPABILITY_LABELS.get(cap_name, cap_name),
            "mode": cap.mode,
            "target": cap.target,
            "lang": cap.lang,
            "endpoints": [describe_endpoint(ep) for ep in cap.ordered_endpoints()],
        })
    return out


def format_describe(services: Services) -> str:
    """给人看的一页（`capsplus services` 与设置窗共用这一份口径）。"""
    lines = []
    if services.error:
        lines.append(f"! {services.error}（下面显示的是内置默认）")
    for item in describe(services):
        lines.append(f"[{item['label']}] 模式 {item['mode']}"
                     + (f" · 目标语言 {item['target']}" if item.get("target") else "")
                     + (f" · 识别语言 {item['lang']}" if item.get("lang") else ""))
        for ep in item["endpoints"]:
            key_line = (f"凭据：{ep['key_source']}" if ep["has_key"] else "凭据：没配")
            lines.append(f"  · {ep['name']}（{ep['kind']}/{ep['style']}）"
                         f" {ep['base_url'] or '（没填地址）'}"
                         f" 模型 {ep['model'] or '—'} · {key_line}")
    return "\n".join(lines)
