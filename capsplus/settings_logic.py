# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""设置窗口的纯逻辑层：**刻意不 import gi**，所以能在没有显示服务器的环境跑单测。

这里只放"界面迟早要算的事"：
  - 覆盖层怎么增删（`set_override` / `delete_override`，返回新表，不改原表）
  - 段名 / 键名 / 动作怎么当场校验（红字文案直接用返回值）
  - keyd 宏的硬边界（单条 `macro(...)` 累计 ≤15 个按键事件）→ 黄字提醒
  - 「应用」按钮的整条流程与状态栏文案（`run_apply`，出错不抛到 GUI 外层）
  - 改动预览（`changed_bindings`：默认渲染 vs 覆盖渲染到底差哪几条）
  - 写盘前把原件快照到 state 目录（焚化炉不充当备份，所以这里不叫删除）
  - **服务页**（2026-10-21）：翻译 / OCR / 语音识别 的"服务在哪"可配置 ——
    表单值 ↔ 配置的往返、校验、脱敏状态、连通性探测（见文件末尾那一段）

界面（`settings/settings_app.py`）只负责摆控件 + 调这些函数。
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path

from . import apply as cap_apply
from . import services as SV
from .env import keyd_cli_hint
from .mapping_overrides import (
    ALLOWED_SECTIONS, OverrideError, save_overrides, toml_table, validate_action,
)

# keyd 宏硬边界（2026-10-15 用虚拟键盘实测）：一条 macro 累计 **≤15 个按键事件**，
# 超了不是截断而是**整条静默不发**，比写错字更难查，所以界面必须当场黄字提醒。
MACRO_EVENT_LIMIT = 15
_MOD_PREFIXES = ("C-", "S-", "A-", "M-", "G-")

# 「动作」下拉里的常用项（界面把这一列并进"自定义"可编辑框，用户仍可手输任意合法动作）
COMMON_ACTIONS = [
    # 光标 / 选中
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "S-up", "S-down", "S-left", "S-right", "S-home", "S-end", "S-pageup", "S-pagedown",
    "C-left", "C-right", "C-home", "C-end", "S-C-left", "S-C-right", "S-C-home", "S-C-end",
    # 编辑
    "enter", "backspace", "delete", "space", "tab", "esc",
    "C-c", "C-x", "C-v", "C-z", "C-a",
    # 其它
    "noop", "layer(capl)", "macro(up up)",
]


# ---------------------------------------------------------------------------
# 编辑条（2026-10-15 治本方案 A）
#   表格只读，编辑控件搬到表格下方。这些函数负责"第 N 行到底是谁"这件事 ——
#   刻意接收**表格行的序列**而不是控件，所以没有显示服务器也能把"编辑条填错行"判红。
# ---------------------------------------------------------------------------
def editor_binding(rows, index: int | None) -> dict | None:
    """编辑条要显示的第 `index` 行 -> `{"section","key","action","note","source"}`。

    `rows` 是表格行序列 `(section, key, action, note, source, ...)`。
    越界 / 没选中 / 空表返回 `None`（界面据此清空编辑条，不许留上一行的残影）。
    """
    items = [tuple(row) for row in (rows or ())]
    if index is None or not (0 <= index < len(items)):
        return None
    section, key, action, note, source = items[index][:5]
    return {"section": section, "key": key, "action": action,
            "note": note, "source": source}


def editor_target(section: str, key: str) -> str:
    """「这一格是谁」的一句写法：`capl / t`（段名用 keyd 的写法，不加引号）。

    编辑条和状态栏**两边都要显式写出来** —— 这是防"静默改错键"的那道保险。
    """
    return f"{(section or '').strip()} / {(key or '').strip()}"


def commit_status(section: str, key: str, action: str) -> str:
    """改完一格的蓝字：写的是覆盖层文件，**没说生效**。生效必须再点「应用」。"""
    return (f"已把 {editor_target(section, key)} 改成 {action}"
            f"（还没生效，点「应用」才写进 keyd）")


def restore_status(section: str, key: str) -> str:
    return (f"{editor_target(section, key)} 已恢复仓库默认（覆盖层里这条删了；"
            f"还没生效，点「应用」才写进 keyd）")


def desync_problem(claimed: str, selected: str) -> str:
    """编辑条认的格子 ≠ 表格当前选中的格子：拦住，宁可一个都不改。"""
    return (f"没改：编辑条显示的是 {claimed}，表格里选中的却是 {selected}。"
            f"先点一下真正要改的那一行 —— 认错行比改错值严重得多。")


# ---------------------------------------------------------------------------
# 宏长度
# ---------------------------------------------------------------------------
def macro_event_count(action: str) -> int:
    """一条 `macro(...)` 累计多少个**按键事件**（修饰键各算 1 个）。

    `macro(up up)` -> 2；`macro(S-up)` -> 2（按下 Shift + 按下 up）；
    `macro(end S-home backspace)` -> 1+2+1 = 4。非 macro 动作返回 0。
    """
    text = (action or "").strip()
    if not text.startswith("macro(") or not text.endswith(")"):
        return 0
    total = 0
    for token in text[len("macro("):-1].split():
        name = token
        extra = 0
        while len(name) > 2 and name[:2] in _MOD_PREFIXES:
            extra += 1
            name = name[2:]
        total += 1 + extra
    return total


def macro_warning(action: str, limit: int = MACRO_EVENT_LIMIT) -> str:
    """超限返回一句黄字（含具体数字），没超限返回 ""。"""
    count = macro_event_count(action)
    if not count or count <= limit:
        return ""
    return (f"这条宏有 {count} 个按键事件，超过 keyd 的 {limit} 个上限："
            f"实测**整条会被静默丢掉**（不是截断），请拆短或改用 pageup/pagedown")


# ---------------------------------------------------------------------------
# 校验（返回值一律是"一句中文"，"" = 没问题）
# ---------------------------------------------------------------------------
def validate_section(section: str) -> str:
    text = (section or "").strip()
    if not text:
        return "段名不能为空"
    if text not in ALLOWED_SECTIONS:
        return (f"只允许改 {' / '.join(f'[{s}]' for s in ALLOWED_SECTIONS)}；"
                f"[main] 是 Caps 的 overload 声明，改错就进不了层")
    return ""


def validate_key_name(key: str, known_keys: set[str] | None = None) -> str:
    text = (key or "").strip()
    if not text:
        return "键名不能为空"
    if any(ch.isspace() for ch in text):
        return f"键名不能含空格: {text!r}"
    if known_keys is not None and text not in known_keys:
        return f"不认识的键名: {text}（见 {keyd_cli_hint()} list-keys）"
    return ""


def action_problem(action: str, key: str, section: str,
                   known_keys: set[str] | None = None) -> str:
    """一次把"段/键/动作"三样都问一遍，返回第一句毛病（"" = 可以写盘）。"""
    for problem in (validate_section(section), validate_key_name(key, known_keys),
                    validate_action(action, known_keys) or ""):
        if problem:
            return problem
    return ""


# ---------------------------------------------------------------------------
# 覆盖层的增与删（都返回**新表**，原表不动 —— 界面取消时不必回滚）
# ---------------------------------------------------------------------------
def copy_values(values: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {section: dict(table) for section, table in values.items()}


def set_override(values: dict[str, dict[str, str]], section: str, key: str, action: str,
                 known_keys: set[str] | None = None) -> dict[str, dict[str, str]]:
    """新增/改掉一条绑定。非法就抛 ValueError（消息就是界面要显的中文）。"""
    problem = action_problem(action, key, section, known_keys)
    if problem:
        raise ValueError(problem)
    out = copy_values(values)
    out.setdefault(section.strip(), {})[key.strip()] = action.strip()
    return out


def delete_override(values: dict[str, dict[str, str]], section: str,
                    key: str) -> dict[str, dict[str, str]]:
    """「恢复默认」= 把这一条从覆盖层删掉（默认值本来就还在仓库里）。"""
    out = copy_values(values)
    table = out.get(section.strip())
    if not table:
        return out
    table.pop(key.strip(), None)
    if not table:
        del out[section.strip()]
    return out


# ---------------------------------------------------------------------------
# 渲染 diff：默认 vs 默认+覆盖（界面「预览改动」用，绝不碰系统）
# ---------------------------------------------------------------------------
def changed_bindings(before_text: str, after_text: str) -> list[str]:
    """两份 keyd 文本之间**真正变了哪几条**（`[段] 键: 旧 -> 新`）。

    刻意用逐行段头判断而不是 `split("[capl+alt]")`：渲染出的头部注释里也写着
    `[capl]` / `[capl+alt]` 字串，按字串切会切错位置（这条踩过）。
    """
    return [f"[{section}] {key}: {old or '（新增）'} -> {new}"
            for section, key, old, new in binding_deltas(before_text, after_text)]


def binding_deltas(before_text: str, after_text: str):
    """把两份文本解析成 `{(段,键): 动作}` 再比，返回 (段, 键, 旧值, 新值) 列表。"""
    before, after = _binding_map(before_text), _binding_map(after_text)
    out = []
    for pair, new in after.items():
        old = before.get(pair)
        if old != new:
            out.append((pair[0], pair[1], old or "", new))
    for pair, old in before.items():
        if pair not in after:
            out.append((pair[0], pair[1], old, "（删掉）"))
    return out


def _binding_map(text: str) -> dict[tuple[str, str], str]:
    """逐行找段头（`[` 开头且 `]` 结尾的裸行），注释行一律跳过。"""
    table: dict[tuple[str, str], str] = {}
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().strip('"')
            continue
        if "=" not in line:
            continue
        key, _, action = line.partition("=")
        table[(section, key.strip())] = action.strip()
    return table


# ---------------------------------------------------------------------------
# 应用（「应用」按钮的整条流程；任何异常都收成状态栏文本，不抛给 GUI）
# ---------------------------------------------------------------------------
def run_apply(dry_run: bool = False, overrides_path: Path | None = None,
              known_keys: set[str] | None = None) -> tuple[bool, str]:
    """返回 `(是否成功, 状态栏文案)`。文案里带上 OverrideError / journal 错误行。

    dry_run=True 只渲染 + 归一化自检，**不碰 /etc/keyd**（界面自检、单测都走这条）。
    `overrides_path` 指定覆盖层时用**它自己渲染**再走 apply：界面把配置指到临时目录做演练，
    绝不能出现"写的是临时文件、渲染的是 ~/.config 那份"这种两张皮。
    """
    text = None
    if overrides_path is not None:
        from .mapping_overrides import load_and_merge
        from .render import render
        data, overrides = load_and_merge(Path(overrides_path), known_keys)
        if overrides.error:
            return False, f"覆盖层有问题，没有应用：{overrides.path} —— {overrides.error}"
        if overrides.errors:
            return False, ("覆盖层有问题，没有应用：\n  " + "\n  ".join(overrides.errors))
        text = render(data)
    try:
        result = cap_apply.apply(dry_run=dry_run, text=text)
    except OverrideError as exc:
        return False, f"覆盖层有问题，没有应用：\n{exc}"
    except cap_apply.ApplyError as exc:
        # ApplyError 里已经写了"回滚到哪个备份 + 错误行"，原样显出来最有用
        return False, f"应用失败：\n{exc}"
    except Exception as exc:                              # 兜底：按钮不许把窗口带崩
        return False, f"没预料的失败：{type(exc).__name__}: {exc}"
    if dry_run:
        return True, result.log or "试渲染通过，未改动系统"
    tail = result.log.strip().splitlines()[-1] if result.log.strip() else ""
    backup = Path(result.backup).name if result.backup else "无"
    return True, f"已应用并 reload；备份 {backup}" + (f"\n最近日志：{tail}" if tail else "")


# ---------------------------------------------------------------------------
# 写盘前的原件快照（备份目录 = ~/.local/state/capsplus/config-backup/<时间戳>/）
# ---------------------------------------------------------------------------
def snapshot_config(path: Path, backup_dir: Path, keep: int = 20) -> Path | None:
    """把要覆盖的原件存一份带时间戳的副本。文件不存在返回 None。

    超出 `keep` 份的旧副本 **mv 进焚化炉**（规矩：不许 rm）。
    """
    source = Path(path)
    if not source.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = Path(backup_dir) / stamp
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / source.name
    shutil.copy2(source, target)
    _prune_snapshots(Path(backup_dir), keep)
    return target


def _prune_snapshots(backup_dir: Path, keep: int) -> None:
    copies = sorted(p for p in backup_dir.iterdir() if p.is_dir()) if backup_dir.is_dir() else []
    stale = copies[:-keep] if keep > 0 else copies
    if not stale:
        return
    grave = cap_apply.attic_dir()
    grave.mkdir(parents=True, exist_ok=True)
    for directory in stale:
        shutil.move(str(directory), str(grave / directory.name))


def save_overrides_with_snapshot(values: dict[str, dict[str, str]], path: Path,
                                 backup_dir: Path) -> Path:
    """覆盖 mappings.toml 前先快照原件，再写（`save_overrides` 自己是原子写）。"""
    snapshot_config(path, backup_dir)
    return save_overrides(values, path)


def override_toml_hint(section: str) -> str:
    """界面上显示 keyd 的段名、写盘走 TOML 的段名 —— 只有两者**不一样**时才值得提示。

    `[capl]` 本来就两边相同，跟着说一句"必须加引号"是纯噪音（实测截图里就刺眼）。
    """
    quoted = toml_table(section)
    if quoted == section:
        return ""
    return f'界面里的 [{section}] 写进文件是 [{quoted}]（带 + 的段名 TOML 必须加引号）'


# ---------------------------------------------------------------------------
# 服务页（2026-10-21）：翻译 / OCR / 语音识别 的"服务在哪"可配置
#
#   需求原话（用户）：「关于 capsplus 工具，我希望关于翻译 OCR 语音识别 的服务都是可以配置的，
#   方便配置远程的服务或者是本地的。」
#
#   这一段是「读配置 → 表单值 → 校验 → 写回」的纯逻辑，刻意不 import gi：
#   判据就测这一层（`tests/test_services.py` / `tests/test_settings_logic.py`），
#   界面（`settings/settings_app.py`）与命令行（`capsplus services`）吃的是**同一份**。
#
#   为什么"探测连通性"也放在这里（而不是界面里各写一份）：
#   CLI 与界面都要"测一下连不连得上"，两处各写一份探测逻辑，过几个月就变成
#   "界面说通了、命令行说不通"——这正是本仓「一份真相源」的老规矩要挡的事。
#
#   **凭据纪律**（与 `capsplus/services.py` 模块头第 ① 条同源）：
#   凭据只存"来源"（环境变量名 / 凭据文件+键 / env 文件+键），明文只是兜底；
#   **任何输出都不许出现凭据值** —— 表单值里根本没有明文 key 这一项，
#   界面显示的是"已配置（来自 环境变量 XXX）"，输入框是空的（`visibility=False`）。
# ---------------------------------------------------------------------------

SERVICE_KINDS = ("local", "remote")
SERVICE_STYLES = ("openai", "script", "http")
KIND_LABELS = {"local": "本机", "remote": "远程"}
STYLE_LABELS = {"openai": "OpenAI 兼容", "script": "本机脚本", "http": "HTTP 服务"}

# 端点表单的字段（顺序 = 界面里输入框的顺序）。**没有 api_key 这一项** ——
# 明文 key 是"只写不读"的：界面上只看得见"已配置（来自 X）"。
ENDPOINT_FIELDS = ("name", "kind", "style", "base_url", "model",
                   "api_key_env", "credentials_file", "credentials_key",
                   "env_file", "env_key", "timeout_s")

# TOML 裸键能用的字符：端点名字会写进 `[translate.local]` 这种表头，
# 名字里带空格/点号就写出一份**读不回来**的配置（这是真坑，不是洁癖）。
_BARE_KEY_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

# 探测的默认超时（秒）。端点自己写了 timeout_s 就用它的。
DEFAULT_PROBE_TIMEOUT_S = 8.0
# 最小 chat 的上限（token）：探活只要"能出字"，不要烧用户的额度。
PROBE_MAX_TOKENS = 8


def _to_float(value, default: float = 0.0) -> float:
    """把表单里的字符串安全地变成数字（写坏了就退 0，不抛给界面）。"""
    try:
        return float(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 表单值 ↔ 配置
# ---------------------------------------------------------------------------
def credential_state(ep) -> str:
    """凭据状态（一行，**永远不含值**）：`已配置（来自 环境变量 XXX）` / `没配`。

    为什么要单列一个函数：界面、命令行、状态页都要这句话，
    各写各的迟早有一处把 key 值打出来 —— 而"打出来"就是泄露。
    """
    key, source = ep.resolved_api_key()
    if not key:
        return "没配"
    return f"已配置（来自 {source}）"


def credential_source_short(ep) -> str:
    """凭据来源的**短版**（给表格那一列用）：只说来源的种类，不带路径。

    为什么要有短版：表格列宽有限，而"已配置（来自 凭据文件 ~/.dsh/.credentials.yaml 的
    AL_TOKEN_PLAN_API_KEY）"有 60 来个字符 —— 塞进列里只会被截断，
    **被截断的列等于没有**（实测截图里就是这样：那一列看着有字，其实少了一半）。
    完整那句话放在编辑条下方的状态行里（那里会换行，放得下）。
    """
    key, source = ep.resolved_api_key()
    if not key:
        return "没配"
    if source.startswith("环境变量"):
        return f"环境变量 {ep.api_key_env}"
    if source.startswith("凭据文件"):
        return "凭据文件"
    if ep.env_file and source.startswith(ep.env_file):
        return "env 文件"
    return "明文"


def endpoint_form_values(ep) -> dict:
    """端点 → 表单值。

    **明文 key 故意不在返回值里**：界面上只能看到 `has_plain_key` / `key_state`
    两个"状态"，看不到值。要换 key 就重新填，要清掉就勾「清掉明文 key」。
    """
    values = {name: getattr(ep, name) for name in ENDPOINT_FIELDS}
    values["timeout_s"] = ep.timeout_s
    values["has_plain_key"] = bool(ep.api_key)
    values["key_state"] = credential_state(ep)
    return values


def endpoint_problems(values, other_names=()) -> list[str]:
    """端点表单的全部毛病（中文，界面直接显式显示）。没问题返回 []。

    `other_names` = 同一栏里**别的**端点的名字（用来抓重名）。
    """
    out: list[str] = []
    name = str(values.get("name") or "").strip()
    kind = str(values.get("kind") or "").strip().lower()
    style = str(values.get("style") or "").strip().lower()
    base_url = str(values.get("base_url") or "").strip()
    cred_file = str(values.get("credentials_file") or "").strip()
    cred_key = str(values.get("credentials_key") or "").strip()
    env_file = str(values.get("env_file") or "").strip()
    env_key = str(values.get("env_key") or "").strip()
    if not name:
        out.append("名字不能为空（它是配置文件里的表名，如 [translate.local]）")
    elif any(ch.isspace() for ch in name):
        out.append(f"名字不能含空格: {name!r}")
    elif not set(name) <= _BARE_KEY_OK:
        out.append("名字只能用字母、数字、下划线、短横线（TOML 裸键的规矩）")
    elif name in tuple(other_names):
        out.append(f"名字重复：{name}（同一栏里两个端点不能同名）")
    if kind not in SERVICE_KINDS:
        out.append(f"本机/远程只能填 {' 或 '.join(SERVICE_KINDS)}")
    if style not in SERVICE_STYLES:
        out.append(f"类型只能填 {' / '.join(SERVICE_STYLES)}")
    if style != "script":
        # script 型的 base_url 是脚本名/路径，不是网址（本地 OCR 就是它）
        if not base_url:
            out.append("地址不能为空")
        elif not base_url.startswith(("http://", "https://")):
            out.append(f"地址要以 http:// 或 https:// 开头（现在写的是 {base_url}）")
    if cred_file and not cred_key:
        out.append("填了凭据文件就要同时写它里面的键名")
    if cred_key and not cred_file:
        out.append("写键名就要同时写凭据文件路径")
    if env_file and not env_key:
        out.append("填了 env 文件就要同时写它里面的键名")
    if env_key and not env_file:
        out.append("写键名就要同时写 env 文件路径")
    raw_timeout = values.get("timeout_s", 0)
    try:
        timeout = float(str(raw_timeout).strip() or 0)
    except (TypeError, ValueError):
        out.append(f"超时必须是秒数（数字）：{raw_timeout!r}")
    else:
        if timeout < 0:
            out.append("超时不许是负数")
    return out


def endpoint_from_form(values, old=None) -> SV.Endpoint:
    """表单值 → 端点（**新对象**，不动 old）。

    明文 key 的三条规矩（界面上那个输入框永远是空的，只有"已配置（来自 X）"）：
      · 填了新值 → 换掉；
      · 留空 → **保持原样**（不许因为界面上看不见就把已存的 key 抹了）；
      · 勾了「清掉明文 key」→ 真的删掉。
    """
    ep = SV.Endpoint(
        name=str(values.get("name") or "").strip(),
        kind=str(values.get("kind") or SV.KIND_LOCAL).strip().lower(),
        style=str(values.get("style") or SV.STYLE_OPENAI).strip().lower(),
        base_url=str(values.get("base_url") or "").strip(),
        model=str(values.get("model") or "").strip(),
        api_key_env=str(values.get("api_key_env") or "").strip(),
        credentials_file=str(values.get("credentials_file") or "").strip(),
        credentials_key=str(values.get("credentials_key") or "").strip(),
        env_file=str(values.get("env_file") or "").strip(),
        env_key=str(values.get("env_key") or "").strip(),
        timeout_s=_to_float(values.get("timeout_s")),
    )
    plain = str(values.get("api_key") or "")
    if values.get("clear_key"):
        ep.api_key = ""
    elif plain:
        ep.api_key = plain
    elif old is not None:
        ep.api_key = old.api_key
    return ep


def capability_form_values(cap) -> dict:
    """能力 → 表单值（端点按书写顺序 = auto 模式的优先级顺序）。"""
    return {
        "name": cap.name,
        "mode": cap.mode,
        "target": cap.target,
        "lang": cap.lang,
        "endpoints": [endpoint_form_values(ep) for ep in cap.ordered_endpoints()],
    }


def capability_problems(values) -> list[str]:
    """一个能力（翻译 / OCR / 识别）的全部毛病。没问题返回 []。"""
    out: list[str] = []
    name = str(values.get("name") or "")
    mode = str(values.get("mode") or "").strip()
    endpoints = list(values.get("endpoints") or [])
    names = [str(item.get("name") or "").strip() for item in endpoints]
    if not mode:
        out.append("要选一个用法：auto（按顺序挨个试）或直接指定某个端点")
    elif mode != SV.MODE_AUTO and mode not in names:
        have = "、".join(n for n in names if n) or "（一个都没有）"
        out.append(f"指定的端点「{mode}」不在这一栏里（现有：{have}）")
    if name == "translate" and not str(values.get("target") or "").strip():
        out.append("翻译要写目标语言（如 zh / en）")
    if name == "asr" and not str(values.get("lang") or "").strip():
        out.append("语音识别要写识别语言（如 zh / en）")
    for index, item in enumerate(endpoints):
        others = [n for i, n in enumerate(names) if i != index]
        label = names[index] or f"第 {index + 1} 个"
        for problem in endpoint_problems(item, others):
            out.append(f"端点 {label}：{problem}")
    return out


def capability_from_form(values, old=None, olds=None) -> SV.Capability:
    """表单值 → 能力（端点顺序 = 表单顺序 = auto 的优先级）。

    `olds` = 与 `endpoints` **按位置**对应的旧端点列表。
    为什么按位置而不是按名字：用户给端点**改名**时，旧端点还在那一行上，
    按名字找就找不到它 → 它那份明文 key 被悄悄丢掉（界面上又看不见 key，
    丢了等于静默删凭据，这条坑值得写在代码里）。
    """
    old_list = list(olds) if olds is not None else []
    old_by_name = {ep.name: ep for ep in (old.ordered_endpoints() if old else [])}
    cap = SV.Capability(name=str(values.get("name") or ""))
    cap.mode = str(values.get("mode") or SV.MODE_AUTO).strip()
    cap.target = str(values.get("target") or "").strip()
    cap.lang = str(values.get("lang") or "").strip()
    for index, item in enumerate(values.get("endpoints") or []):
        previous = old_list[index] if index < len(old_list) else \
            old_by_name.get(str(item.get("name") or "").strip())
        ep = endpoint_from_form(item, previous)
        cap.endpoints[ep.name] = ep
    return cap


def copy_services(services: SV.Services) -> SV.Services:
    """整份配置的深拷贝（界面改一份、留一份原样 —— 取消时不必回滚）。"""
    out = SV.Services(path=services.path, header=services.header, error=services.error)
    for name in SV.CAPABILITIES:
        cap = services.get(name)
        if cap is None:
            continue
        new_cap = SV.Capability(name=cap.name, mode=cap.mode, target=cap.target,
                                lang=cap.lang, timeout_s=cap.timeout_s)
        for ep in cap.ordered_endpoints():
            new_cap.endpoints[ep.name] = SV.copy_endpoint(ep)
        out.capabilities[name] = new_cap
    return out


def services_form_values(services: SV.Services) -> dict:
    """整份配置 → 表单值（给界面回填；顺序 = `services.CAPABILITIES`）。"""
    out = {}
    for name in SV.CAPABILITIES:
        cap = services.get(name)
        if cap is not None:
            out[name] = capability_form_values(cap)
    return out


def services_problems(services: SV.Services) -> list[str]:
    """整份配置的全部毛病（写盘前先问一遍）。没问题返回 []。"""
    out: list[str] = []
    for name in SV.CAPABILITIES:
        cap = services.get(name)
        if cap is None:
            continue
        label = SV.CAPABILITY_LABELS.get(name, name)
        for problem in capability_problems(capability_form_values(cap)):
            out.append(f"{label}：{problem}")
    return out


# ---------------------------------------------------------------------------
# 改内存态（界面用；**返回新对象**，原配置不动）
# ---------------------------------------------------------------------------
def set_endpoint(services: SV.Services, capability_name: str, index: int | None,
                  values: dict) -> tuple[SV.Services | None, str]:
    """把一个端点的表单值写进整份配置。返回 `(新配置, "")` 或 `(None, 毛病一句)`。

    `index` = 这一栏端点列表里的第几个（从 0 起）；`None` = 新增一个。
    旧端点按**位置**找（改名时也要保住它的明文 key，见 `capability_from_form` 的说明）。
    """
    cap = services.get(capability_name)
    if cap is None:
        return None, f"没有「{capability_name}」这一栏"
    items = [endpoint_form_values(ep) for ep in cap.ordered_endpoints()]
    if index is None:
        items.append(dict(values))
    else:
        if not (0 <= index < len(items)):
            return None, f"这一栏里没有第 {index + 1} 个端点（先在表里点一行，或点「新增」）"
        items[index] = dict(values)
    others = [str(item.get("name") or "").strip()
              for i, item in enumerate(items) if i != index]
    problems = endpoint_problems(values, others)
    if problems:
        return None, "；".join(problems)
    olds = cap.ordered_endpoints()
    new_cap = capability_from_form({"name": capability_name, "mode": cap.mode,
                                    "target": cap.target, "lang": cap.lang,
                                    "endpoints": items}, olds=olds)
    out = copy_services(services)
    out.capabilities[capability_name] = new_cap
    return out, ""


def add_endpoint(services: SV.Services, capability_name: str,
                 values: dict | None = None) -> tuple[SV.Services | None, str]:
    """新增一个端点（默认名字按已有端点往后排，不重名）。"""
    cap = services.get(capability_name)
    if cap is None:
        return None, f"没有「{capability_name}」这一栏"
    existing = {ep.name for ep in cap.ordered_endpoints()}
    base = dict(values or {})
    if not str(base.get("name") or "").strip():
        index = len(existing) + 1
        while f"ep{index}" in existing:
            index += 1
        base["name"] = f"ep{index}"
    base.setdefault("kind", SV.KIND_LOCAL)
    base.setdefault("style", SV.STYLE_OPENAI)
    base.setdefault("timeout_s", 0)
    others = [ep.name for ep in cap.ordered_endpoints()]
    # 只查重名：**不查地址** —— 新增的端点本来就是空的（用户接着填），
    # 完整的校验在「保存这一条」和「保存服务配置」那两步。
    name_problems = [p for p in endpoint_problems(base, others) if "名字" in p]
    if name_problems:
        return None, "；".join(name_problems)
    items = [endpoint_form_values(ep) for ep in cap.ordered_endpoints()] + [base]
    new_cap = capability_from_form({"name": capability_name, "mode": cap.mode,
                                    "target": cap.target, "lang": cap.lang,
                                    "endpoints": items},
                                   olds=cap.ordered_endpoints())
    out = copy_services(services)
    out.capabilities[capability_name] = new_cap
    return out, ""


def delete_endpoint(services: SV.Services, capability_name: str,
                    name: str) -> tuple[SV.Services | None, str]:
    """删掉一个端点（按名字）。找不到就报错，不静默当成功。"""
    cap = services.get(capability_name)
    if cap is None:
        return None, f"没有「{capability_name}」这一栏"
    if name not in cap.endpoints:
        return None, f"这一栏里没有名为「{name}」的端点"
    items = [endpoint_form_values(ep) for ep in cap.ordered_endpoints()
             if ep.name != name]
    new_cap = capability_from_form({"name": capability_name, "mode": cap.mode,
                                    "target": cap.target, "lang": cap.lang,
                                    "endpoints": items})
    if new_cap.mode != SV.MODE_AUTO and new_cap.mode == name:
        new_cap.mode = SV.MODE_AUTO       # 删掉的正好是被指定的那个 → 退回 auto
    out = copy_services(services)
    out.capabilities[capability_name] = new_cap
    return out, ""


def set_capability_mode(services: SV.Services, capability_name: str, mode: str,
                        target: str | None = None, lang: str | None = None) -> tuple[SV.Services | None, str]:
    """改这一栏的用法（auto / 直接指定某个端点）与语言参数。

    `target` / `lang` 缺省 = **沿用原来的**；显式传 `""` = 真的清空
    （清空翻译的目标语言要报错，所以这两个"缺省"必须分得开 —— 用 `None` 当哨兵）。
    """
    cap = services.get(capability_name)
    if cap is None:
        return None, f"没有「{capability_name}」这一栏"
    mode = (mode or "").strip()
    if mode != SV.MODE_AUTO and mode not in cap.endpoints:
        return None, (f"指定的端点「{mode}」不在这一栏里"
                      f"（现有：{'、'.join(cap.endpoints) or '（一个都没有）'}）")
    values = {"name": capability_name, "mode": mode,
              "target": cap.target if target is None else target,
              "lang": cap.lang if lang is None else lang,
              "endpoints": [endpoint_form_values(ep) for ep in cap.ordered_endpoints()]}
    problems = capability_problems(values)
    if problems:
        return None, "；".join(problems)
    new_cap = capability_from_form(values, olds=cap.ordered_endpoints())
    out = copy_services(services)
    out.capabilities[capability_name] = new_cap
    return out, ""


def save_services_form(services: SV.Services, path=None) -> tuple[bool, str]:
    """校验整份配置 → 写盘。返回 `(ok, 说明)`。

    写盘走 `capsplus.services.save_services`（原子写 + 0600 + 回读自证），
    这一层只负责"有没有毛病"——**有毛病就一个字都不写**。
    """
    problems = services_problems(services)
    if problems:
        return False, "没有写盘，先改这几处：\n  " + "\n  ".join(problems)
    ok, text = SV.save_services(services, path)
    return ok, text


# ---------------------------------------------------------------------------
# 连通性探测（CLI 与界面共用这一份；**绝不打印凭据**）
# ---------------------------------------------------------------------------
def redact(text: str, secret: str) -> str:
    """把凭据值从一段文本里抹掉（防服务端把请求头回显出来）。"""
    if not secret:
        return text or ""
    return (text or "").replace(secret, "***")


def _default_get(url: str, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b""


def _default_post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b""


def _http_hint(status: int, url: str) -> str:
    """一个 HTTP 状态码对应的一句"下一步动作"（状态必须带动作，老规矩）。"""
    if status in (401, 403):
        return ("对面拒了凭据：检查凭据来源（环境变量名 / 凭据文件里的键）还对不对"
                f" —— `curl -sS -o /dev/null -w '%{{http_code}}' {url}`")
    if status == 404:
        return (f"地址不对（404）：`{url}` 这个路径没有东西 —— "
                "OpenAI 兼容服务的地址一般要带 /v1")
    if status == 400:
        return (f"对面嫌请求不对（400）：多半是**模型名**或地址写错了 —— "
                f"`curl -sS {url}` 看它要什么")
    if 500 <= status < 600:
        return f"对面自己报错了（HTTP {status}）：看服务端日志，客户端这边改配置没用"
    return f"HTTP {status}：`curl -sS {url}` 看它到底答了什么"


def _openai_model_count(body: bytes) -> tuple[int, bool]:
    """`/models` 的回包里有几个模型。第二个返回值 = 是不是 OpenAI 形状的回包。

    为什么还要判形状：百度那类 REST 地址对任何路径都回 200 + 一个错误 JSON ——
    只看状态码会把"根本不是 OpenAI 兼容服务"说成"通了"。
    """
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return 0, False
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return 0, False
    return len(data["data"]), True


def _parse_openai_reply(body: bytes) -> tuple[bool, str]:
    """读 OpenAI 兼容响应。返回 `(有没有正文, 正文或原因)`。

    **优先用 `capsplus.shot_translate.parse_completion`** —— 那是本仓认定的
    "怎么读 OpenAI 响应"的唯一一份实现，而且它把"content 为空"的原因说得很准
    （推理模型把 token 全花在 reasoning_content 里，2026-10-20 真机踩过）。
    那个模块正在被别人改，所以拿不到时退回下面这段最小实现 ——
    判据（`tests/test_services.py`）不依赖它，离线也能跑。
    """
    try:
        from . import shot_translate

        return shot_translate.parse_completion(body)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False, "回的不是 JSON"
    if not isinstance(data, dict):
        return False, "回的不是对象"
    if data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        return False, f"服务端报错：{message}"
    choices = data.get("choices") or []
    if not choices:
        return False, "响应里没有 choices（这个地址可能不是 OpenAI 兼容服务）"
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        return False, f"响应里没有正文（content 为空，finish_reason={choices[0].get('finish_reason')}）"
    return True, str(content).strip()


def _probe_openai(ep, get, post, timeout: float, key: str) -> tuple[bool, str, str]:
    """OpenAI 兼容端点：**先 /models 再最小 chat**。

    为什么两步都做（2026-10-20 实测教训）："端点活着"≠"能出字" ——
    推理模型把 token 全花在 thinking 上，`content` 是空串，而 HTTP 仍是 200。
    只探 /models 会把一个坏端点说成好的（用户按 F3 才发现翻译没反应）。
    同一条教训的反面也要守住：**答了 200 但 content 空，不许报"通了"**。

    请求体与真翻译同形状（含 `chat_template_kwargs: {"enable_thinking": false}`）——
    少了这个开关，本机那个推理模型回的就是空 content（实测）。
    对面不认这个可选字段（400）时去掉重试一次，与真翻译同一套退路。
    """
    base = ep.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    parts = []
    ok = False
    try:
        status, body = get(f"{base}/models", timeout)
        if 200 <= status < 300:
            count, shape = _openai_model_count(body)
            parts.append(f"/models 通了（{count} 个模型）" if shape else
                         "/models 回的不是 OpenAI 格式（这个地址大概不是 OpenAI 兼容服务）")
        else:
            parts.append(f"/models 答了 HTTP {status}")
    except Exception as exc:                       # noqa: BLE001 —— 探测不许把界面带崩
        parts.append(f"/models 连不上（{type(exc).__name__}）")

    def chat_body(with_no_think: bool) -> bytes:
        body = {
            "model": ep.model or "default",
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": PROBE_MAX_TOKENS,
            "stream": False,
        }
        if with_no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return json.dumps(body).encode("utf-8")

    try:
        payload = chat_body(True)
        status, body = post(f"{base}/chat/completions", payload, headers, timeout)
        if status == 400:
            # 对面不认这个可选字段 —— 去掉重试一次（与真翻译同一套退路）
            payload = chat_body(False)
            status, body = post(f"{base}/chat/completions", payload, headers, timeout)
            parts.append("（对面不认关思考那个字段，已去掉重试）")
        if 200 <= status < 300:
            parsed_ok, text = _parse_openai_reply(body)
            if parsed_ok:
                snippet = redact(text.strip().replace("\n", " "), key)[:40]
                parts.append(f"能出字（{snippet or '（空）'}）")
                ok = True
            else:
                parts.append(f"答了但没出字：{text}")
        else:
            parts.append(f"chat 答了 HTTP {status}")
    except Exception as exc:                       # noqa: BLE001
        parts.append(f"chat 连不上（{type(exc).__name__}）")

    if ok:
        hint = ""
    else:
        hint = (f"这个端点没通：先 `curl -s {base}/models` 看一眼地址对不对，"
                "再确认「类型」选的是 openai、模型名没写错；"
                "答 401 就是凭据不对（看那一栏的凭据来源）。")
    return ok, "；".join(parts), hint


def _probe_http(ep, get, timeout: float, key: str) -> tuple[bool, str, str]:
    """自家 HTTP 协议端点（本机识别服务：`GET /health`）。"""
    base = ep.base_url.rstrip("/")
    url = f"{base}/health"
    try:
        status, body = get(url, timeout)
    except Exception as exc:                       # noqa: BLE001
        return False, f"连不上（{type(exc).__name__}）", \
            (f"服务没在跑：`systemctl --user status voice-svc`，"
             f"再 `curl -s {url}` 应回 ok=true")
    if not (200 <= status < 300):
        return False, f"HTTP {status}", _http_hint(status, url)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False, "回的不是 JSON", f"服务回了非 JSON：`curl -sv {url}` 看一眼真实返回"
    ok = bool(data.get("ok"))
    loaded = data.get("model_loaded")
    summary = f"ok={ok} model_loaded={loaded}"
    if ok:
        return True, summary, ""
    return False, summary, f"服务活着但没就绪：`systemctl --user restart voice-svc`，" \
                           f"再 `curl -s {url}`"


def _probe_png() -> bytes:
    """一张 32×32 的白 PNG（纯标准库拼的）—— 探活用，识别结果必然是空。

    为什么自己拼而不是找张图：探活要的是"脚本能跑起来"，不是"识别得准"；
    仓里不该为了探活塞一张二进制图片。
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    width = height = 32
    raw = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _probe_script(ep, runner, timeout: float) -> tuple[bool, str, str]:
    """本机脚本端点（本地 OCR 的 rapidocr 助手）：**真跑一次**。

    跑的是仓里的助手脚本，解释器要找"装了 rapidocr 的那个" ——
    这个知识在 `capsplus/shot_ocr.py` 里（候选 venv 路径），别在这里抄一份。
    """
    script = Path(ep.base_url).expanduser()
    if not script.exists():
        script = Path(__file__).resolve().parent / ep.base_url
    if not script.exists():
        return False, f"找不到助手脚本 {ep.base_url}", \
            (f"把「地址」填成脚本的真实路径（仓内的是 "
             f"{Path(__file__).resolve().parent / 'shot_ocr_local.py'}）")
    try:
        from . import shot_ocr
        candidates = shot_ocr.candidate_pythons()
    except Exception:                               # noqa: BLE001 —— OCR 模块不该拖垮探测
        candidates = []
    if not candidates:
        return False, "找不到能跑本地 OCR 的解释器", \
            ("装本地 OCR：`capsplus shot --install-local-ocr`，"
             "或用 CAPSPLUS_OCR_PYTHON 指一个装了 rapidocr 的 python")
    runner = runner or (lambda argv, timeout_s: subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_s))
    picture = Path("/tmp") / f"capsplus-probe-{int(time.time() * 1000)}.png"
    picture.write_bytes(_probe_png())
    try:
        for python in candidates:
            try:
                done = runner([str(python), str(script), str(picture)], timeout)
            except subprocess.TimeoutExpired:
                return False, f"{python} 跑超时（{int(timeout)}s）", \
                    "本机 OCR 第一次跑要加载模型（几秒），超时太短就调大「超时」"
            except Exception as exc:                # noqa: BLE001
                return False, f"{python} 跑不起来（{type(exc).__name__}）", \
                    f"手动跑一次看看：`{python} {script} {picture}`"
            if getattr(done, "returncode", 1) != 0:
                continue
            text = (getattr(done, "stdout", "") or "").strip().splitlines()
            payload = {}
            for line in reversed(text):
                if line.startswith("{"):
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        payload = {}
                    break
            if payload.get("ok"):
                return True, f"脚本跑通了（{payload.get('engine', '引擎')}，" \
                             f"{payload.get('ms', '?')} ms，识别出 " \
                             f"{len(payload.get('text', ''))} 个字）", ""
        return False, "助手脚本没跑通", \
            ("`capsplus shot --install-local-ocr` 装一下本地 OCR，"
             "或把「地址」指到别的助手脚本")
    finally:
        picture.unlink(missing_ok=True)


def probe_endpoint(ep, *, get=None, post=None, runner=None, timeout=None) -> dict:
    """真跑一次连通性测试。返回 `{"ok", "ms", "summary", "hint"}`。

    三种端点类型各走各的路（这正是 `services.py` 里 `style` 字段的由来）：
      · openai —— 先 `GET {地址}/models`，再 `POST {地址}/chat/completions` 最小 chat；
      · http   —— `GET {地址}/health`（本机识别服务那套协议）；
      · script —— 真跑一次助手脚本（本地 OCR 的 rapidocr）。

    `get` / `post` / `runner` 可注入 —— 判据要能离线跑（不真连网）。
    **绝不打印凭据**：请求头里带着 key，输出里只出现状态码与脱敏后的摘要。
    """
    started = time.perf_counter()
    get = get or _default_get
    post = post or _default_post
    limit = float(timeout) if timeout else float(ep.timeout_s or 0) or DEFAULT_PROBE_TIMEOUT_S
    key, _source = ep.resolved_api_key()
    style = (ep.style or SV.STYLE_OPENAI).strip().lower()
    if style == SV.STYLE_HTTP:
        ok, summary, hint = _probe_http(ep, get, limit, key)
    elif style == SV.STYLE_SCRIPT:
        ok, summary, hint = _probe_script(ep, runner, limit)
    else:
        ok, summary, hint = _probe_openai(ep, get, post, limit, key)
    return {
        "ok": bool(ok),
        "ms": int((time.perf_counter() - started) * 1000),
        "summary": redact(summary, key),
        "hint": hint,
    }


def probe_capability(services: SV.Services, capability_name: str, *,
                     get=None, post=None, runner=None, timeout=None) -> list[dict]:
    """测一栏的所有端点（按书写顺序 = auto 的优先级）。返回逐端点的结果列表。"""
    cap = services.get(capability_name)
    if cap is None:
        return []
    out = []
    for ep in cap.ordered_endpoints():
        result = probe_endpoint(ep, get=get, post=post, runner=runner, timeout=timeout)
        result["endpoint"] = ep.name
        result["capability"] = capability_name
        out.append(result)
    return out
