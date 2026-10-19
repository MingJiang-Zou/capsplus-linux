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

后端选择（`resolve_backend()` 是纯函数，判据直接调它）：
  `CAPSPLUS_OCR=local|cloud|auto`（默认 auto）。`auto` = 本地探得到就用本地（离线、不花钱），
  探不到就用云端（要是连 key 都没有 → **明确说两边都不可用 + 怎么装**，绝不假装成功）。

返回给面板的永远是同一个 dict（见 `shot_annotate.normalize_result` 的约定）：
  `{"ok": bool, "text": str, "backend": "local"|"cloud", "ms": int, "note": str, "hint": str}`
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

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


# ------------------------------------------------------------------ 纯逻辑（判据直接调）

def mode() -> str:
    value = (os.environ.get(MODE_ENV) or "auto").strip().lower()
    return value if value in BACKENDS + ("auto",) else "auto"


def helper_path() -> Path:
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


def ocr_credentials(env_file=None) -> dict:
    """百度 OCR 的两把钥匙：进程环境优先，其次 `~/.dsh/skills/ocr/.env`。"""
    path = Path(os.path.expanduser(env_file or os.environ.get(OCR_ENV_FILE) or DEFAULT_OCR_ENV))
    values = {}
    try:
        values = parse_env_file(path.read_text(encoding="utf-8"))
    except OSError:
        values = {}
    return {"api_key": os.environ.get("BAIDU_OCR_API_KEY") or values.get("BAIDU_OCR_API_KEY", ""),
            "secret_key": (os.environ.get("BAIDU_OCR_SECRET_KEY")
                           or values.get("BAIDU_OCR_SECRET_KEY", "")),
            "env_file": str(path)}


def candidate_pythons(exists=None) -> list:
    """能用来跑本地 OCR 的解释器候选（只保留真的存在的）。"""
    exists = exists or (lambda p: Path(p).exists())
    override = os.environ.get(PYTHON_ENV)
    out = []
    if override:
        out.append(override)
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


def resolve_backend(local_probe=None, creds=None) -> tuple:
    """返回 `(backend | None, 说明)`。三方都不可用时给**可操作**的说明。"""
    mode_ = mode()
    creds = creds if creds is not None else ocr_credentials()
    local_probe = local_probe or default_local_probe
    local_py = local_probe() if mode_ in ("auto", BACKEND_LOCAL) else None
    has_cloud = bool(creds.get("api_key") and creds.get("secret_key"))

    if mode_ == BACKEND_LOCAL:
        if local_py:
            return BACKEND_LOCAL, f"本地 OCR（{local_py}）"
        return None, ("✗ 你指定了本地 OCR，但探不到装了 rapidocr 的解释器 —— "
                      f"装一份：`python3 -m venv ~/.local/share/capsplus/ocr-venv && "
                      f"~/.local/share/capsplus/ocr-venv/bin/pip install rapidocr-onnxruntime`"
                      f"（或用 {PYTHON_ENV} 指一个）")
    if mode_ == BACKEND_CLOUD:
        if has_cloud:
            return BACKEND_CLOUD, "百度 OCR（accurate_basic，1000 次/月免费）"
        return None, f"✗ 你指定了云 OCR，但 {creds.get('env_file')} 里没有百度凭据"
    # auto
    if local_py:
        return BACKEND_LOCAL, f"本地 OCR（{local_py}）"
    if has_cloud:
        return BACKEND_CLOUD, "百度 OCR（本地没装，自动走云）"
    return None, ("✗ 两条 OCR 都不能用：本地没装 rapidocr（`python3 -m venv "
                  "~/.local/share/capsplus/ocr-venv && ~/.local/share/capsplus/ocr-venv/bin/pip "
                  "install rapidocr-onnxruntime`），云端也没凭据"
                  f"（{creds.get('env_file')} 里缺 BAIDU_OCR_API_KEY/SECRET_KEY）")


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
    return make_result(True, text=parsed.get("text", ""), backend=BACKEND_LOCAL,
                       ms=int(parsed.get("ms") or (time.perf_counter() - t0) * 1000),
                       note=parsed.get("engine") or engine_note or "rapidocr")


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


def ocr(png_path, backend=None, *, python=None, runner=None, creds=None, post=None,
        local_probe=None) -> dict:
    """统一入口：按后端跑一次 OCR，返回面板用的 dict。"""
    note = ""
    if backend is None:
        backend, note = resolve_backend(local_probe=local_probe, creds=creds)
        if backend is None:
            return make_result(False, hint=note)
    if backend == BACKEND_LOCAL:
        result = ocr_local(png_path, python=python, runner=runner)
        if note and result["ok"] and not result["note"]:
            result["note"] = note
        return result
    if backend == BACKEND_CLOUD:
        return ocr_cloud(png_path, creds=creds, post=post)
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
             f"百度凭据：{'有' if creds['api_key'] and creds['secret_key'] else '没有'}"
             f"（{creds['env_file']}）"]
    return "\n".join(lines)


if __name__ == "__main__":                                    # pragma: no cover - 手查用
    print(describe())
    sys.exit(0)