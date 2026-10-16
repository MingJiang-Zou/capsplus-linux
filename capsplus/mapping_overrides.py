# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""用户键位覆盖层：`~/.config/capsplus/mappings.toml`（只存被改过的键）。

设计取舍：**仓库默认映射仍是唯一真相源**（`capsplus/mapping_data.py`），
用户覆盖层只放"我改过的那几条"，渲染时 `默认 + 覆盖` 合并、覆盖最后生效。
好处有三：① 没改过的键跟着仓库升级走，不会被老的整份副本钉死；
② 「恢复默认」= 删掉那一条，不需要记住原值；③ 没有覆盖层时渲染结果**逐字节**不变。

段名与 keyd 一致，但 **TOML 里带 `+` 的段名必须加引号**（裸键只允许 A-Za-z0-9_-）：

    [capl]
    e = "up"
    t = "pageup"

    ["capl+alt"]          # ← 增强层要写成这样，裸写 [capl+alt] 是语法错误
    t = "up"

合并规则：
  - 默认里有的键 -> 换掉动作（顺序、分组注释都不动，diff 最小）
  - 默认里没有的键 -> 追加到该段末尾（keyd 里就是新增一条绑定）
  - 未知的段 / 非法动作 -> 收进 `errors`，**该条不生效**（宁可少改，不要写出坏配置）
  - 语法坏掉的文件 -> `error` 记原因，当没有覆盖层用（Qbar / apply 照常能跑）
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import mapping_data as MD

# 覆盖层允许改的段：不含 [main]（那里是 Caps 的 overload 声明，改错就没法进层了）
ALLOWED_SECTIONS = [MD.PRIMARY_LAYER, "capl+alt"]

CONFIG_DIR = Path.home() / ".config/capsplus"
OVERRIDES_PATH = CONFIG_DIR / "mappings.toml"

# keyd 里的动作词（`keyd.rvaiya list-keys` 之外的合法动作）
ACTION_WORDS = frozenset({
    "noop", "oneshot", "swap", "clear", "layer", "overload", "overloadt",
    "overloadi", "overloadt2", "timeout", "togglemouse", "toggle", "macro",
    "command", "exec", "setlayout", "scrollt", "unicode", "lettermod", "plain",
})
# 允许重复的修饰前缀（keyd: C-c / S-up / M-left / A-…）
_MOD_PREFIXES = ("C-", "S-", "A-", "M-", "G-")
_ACTION_FORMS = ("macro(", "command(", "layer(", "overload(", "togglemouse(",
                 "oneshot(", "swap(", "setlayout(", "scrollt(", "unicode(",
                 "lettermod(", "plain(", "timeout(", "overloadt(", "overloadi(")
_LAYER_NAME_RE = re.compile(r"^[a-z0-9_+.-]+$")


_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def toml_table(section: str) -> str:
    """段名写成合法 TOML 表头。

    `capl+alt` 里的 `+` **不是** TOML 裸键字符（裸键只允许 A-Za-z0-9_-），
    裸写 `[capl+alt]` 会被 tomllib 直接报语法错、整份覆盖层当没有用 ——
    所以非裸键必须加引号：`["capl+alt"]`。
    """
    return section if _BARE_KEY_RE.match(section) else f'"{section}"'


class OverrideError(ValueError):
    """覆盖层有问题（语法错或某条非法）——apply 会带着提示拒绝执行。"""


@dataclass
class Overrides:
    """一次读取的结果。`values[段][键] = 动作`；`errors` 里的条目**不生效**。"""
    values: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    path: Path = OVERRIDES_PATH
    error: str = ""            # 整个文件读不了 / 语法坏掉（区别于单条非法）

    @property
    def empty(self) -> bool:
        return not any(self.values.values())

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.values.values())

    def get(self, section: str, key: str) -> str | None:
        """这一条是用户改过的吗？是就返回用户的动作，否则 None（= 用仓库默认）。"""
        return self.values.get(section, {}).get(key)

    def is_overridden(self, section: str, key: str) -> bool:
        return self.get(section, key) is not None


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def action_keys(action: str, known_keys: set[str] | None = None) -> list[str]:
    """把一条动作里出现的**按键名**挑出来（修饰前缀剥掉；动作词与层名不算）。

    例：`S-C-home` -> ["home"]；`macro(up up)` -> ["up","up"]；
        `command(/bin/true)` -> []（括号里的东西不是键名）。

    传了 `known_keys` 时语义变成**只返回不认识的键名**（`validate_action` 要的是
    "有没有非法项"），认识的键返回 []。
    """
    text = action.strip()
    if not text:
        return []
    if any(text.startswith(form) and text.endswith(")") for form in _ACTION_FORMS):
        inner = text[text.index("(") + 1:-1]
        out: list[str] = []
        for token in inner.split():
            out += action_keys(token, known_keys)
        return out
    # 剩下的应当是一个"键"：剥修饰前缀后必须是个键名
    name = text
    while len(name) > 2 and name[:2] in _MOD_PREFIXES:
        name = name[2:]
    if not name or name.lower() in ACTION_WORDS:
        return []
    if known_keys is not None:
        # 给了键名表就只报**不认识**的（validate_action 要的是"有没有非法项"，
        # 不是"出现了哪些键"）；这里曾经两条分支都 return [name]，把 up/pageup 也当成
        # 不认识，导致合法覆盖全被拦下 —— 2026-10-15 单测抓到的真 bug。
        return [] if name in known_keys else [name]
    return [name]


def validate_action(action: str, known_keys: set[str] | None = None) -> str | None:
    """合法返回 None，否则返回一句中文原因（给界面直接显示）。

    注意顺序：先看**函数式动作**（`macro(...)` 里天然带空格），再看裸动作。
    """
    text = (action or "").strip()
    if not text:
        return "动作不能为空"
    if "(" in text or ")" in text:
        if not text.endswith(")"):
            return f"括号没闭合: {text!r}"
        head = text[:text.index("(")]
        if head == "macro":
            inner = text[len("macro("):-1].strip()
            if not inner:
                return "macro() 里没有内容"
        elif head not in ("command", "layer", "overload", "overloadt", "overloadi",
                          "oneshot", "swap", "setlayout", "scrollt", "unicode",
                          "lettermod", "plain", "timeout", "togglemouse"):
            return f"不认识的函数式动作: {head}("
        return None
    if any(ch.isspace() for ch in text):
        return f"动作不能含空格（要连按就用 macro(up up)）: {text!r}"
    unknown = action_keys(text, known_keys)
    if unknown and known_keys is not None:
        return f"不认识的键名: {unknown[0]}（见 keyd.rvaiya list-keys）"
    return None


def layer_rows(known_keys: set[str] | None = None) -> list[tuple[str, str]]:
    """覆盖层能改的 (段, 键) 全表 —— 界面下拉用，顺序与渲染一致。"""
    rows: list[tuple[str, str]] = []
    for section, entries in MD.SECTIONS:
        for entry in entries:
            if isinstance(entry, str):
                continue
            rows.append((section, entry[0]))
    return rows


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def load_overrides(path: Path | None = None,
                   known_keys: set[str] | None = None) -> Overrides:
    """读覆盖层。任何问题都**不抛异常**，而是记进 `error` / `errors`。"""
    import tomllib

    target = Path(path) if path else OVERRIDES_PATH
    result = Overrides(path=target)
    if not target.exists():
        return result
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        result.error = f"不是 UTF-8 文本: {exc}"
        return result
    except OSError as exc:
        result.error = f"读不了 {target}: {exc}"
        return result
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        hint = ""
        # 用户最容易踩的坑：照 keyd 的写法裸敲 [capl+alt]（+ 不是 TOML 裸键字符）
        if re.search(r"(?m)^\[[^\]\"]*\+[^\]\"]*\]$", text):
            hint = '（带 + 的段名要加引号，如 ["capl+alt"]）'
        result.error = f"TOML 语法错误: {exc}{hint}"
        return result

    if not isinstance(raw, dict):
        result.error = "顶层不是一个表"
        return result

    for section, rows in raw.items():
        if section not in ALLOWED_SECTIONS:
            result.errors.append(f"段 [{section}] 不支持（只能改 {' / '.join(ALLOWED_SECTIONS)}）")
            continue
        if not isinstance(rows, dict):
            result.errors.append(f"[{section}] 不是一个表")
            continue
        table: dict[str, str] = {}
        for key, action in rows.items():
            if not isinstance(action, str):
                result.errors.append(f"[{section}] {key} 的值必须是字符串动作")
                continue
            reason = validate_action(action, known_keys)
            if reason:
                result.errors.append(f"[{section}] {key} = {action!r}: {reason}")
                continue
            table[str(key)] = action.strip()
        if table:
            result.values[section] = table
    return result


# ---------------------------------------------------------------------------
# 合并（渲染用）
# ---------------------------------------------------------------------------
class MergedData:
    """与 `capsplus.mapping_data` 同形的只读视图（render 只读 IDS/MAIN/SECTIONS）。"""

    def __init__(self, sections, main, ids):
        self.SECTIONS = sections
        self.MAIN = main
        self.IDS = ids


def merge_overrides(overrides: Overrides) -> MergedData:
    """`仓库默认 + 用户覆盖` 合成渲染数据。覆盖最后生效、位置不动、新键追加在段尾。"""
    sections = []
    for section, rows in MD.SECTIONS:
        table = overrides.values.get(section, {})
        new_rows = []
        for entry in rows:
            if isinstance(entry, str):
                new_rows.append(entry)
                continue
            key, action = entry
            new_rows.append((key, table.get(key, action)))
        for key, action in table.items():          # 默认里没有的键：追加（保持文件顺序）
            if not any(not isinstance(e, str) and e[0] == key for e in rows):
                new_rows.append((key, action))
        sections.append((section, new_rows))
    return MergedData(sections, list(MD.MAIN), MD.IDS)


def load_and_merge(path: Path | None = None,
                   known_keys: set[str] | None = None) -> tuple[MergedData, Overrides]:
    """apply / render 的入口：读覆盖层并合成。"""
    overrides = load_overrides(path, known_keys)
    return merge_overrides(overrides), overrides


# ---------------------------------------------------------------------------
# 写（界面用；写前先备份成 .bak 是调用方的事）
# ---------------------------------------------------------------------------
HEADER = """# capsplus 用户键位覆盖层
#
# 这里**只放你改过的键**：没写进来的键跟着仓库默认（capsplus/mapping_data.py）走。
# 段名与 keyd 配置一致，但 TOML 里带 + 的段名必须加引号：
#   [capl]        = 按住 Caps 的基础层
#   ["capl+alt"]  = 再加 Alt 的增强层（裸写 [capl+alt] 是语法错误）
# 改完要跑 `capsplus apply`（或界面上的「应用」）才会生效；出错会自动回滚上一版。
#
# 恢复默认 = 把这一行删掉（界面里的「恢复默认」按钮帮你删）。
#
"""


def render_overrides(values: dict[str, dict[str, str]]) -> str:
    """把覆盖表渲染成 TOML 文本（顺序固定为 ALLOWED_SECTIONS，便于人读与 diff）。"""
    out = [HEADER.rstrip("\n"), ""]
    for section in ALLOWED_SECTIONS:
        table = values.get(section) or {}
        if not table:
            continue
        out.append(f"[{toml_table(section)}]")
        for key, action in table.items():
            out.append(f'{key} = "{action}"')
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def save_overrides(values: dict[str, dict[str, str]], path: Path | None = None) -> Path:
    """原子写：先写 .tmp 再 replace（中途失败不会留下半份配置）。"""
    target = Path(path) if path else OVERRIDES_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render_overrides(values), encoding="utf-8")
    tmp.replace(target)
    return target


def load_known_keys() -> set[str] | None:
    """`keyd.rvaiya list-keys` 的输出（校验键名用）。取不到就返回 None = 不校验键名。"""
    import subprocess
    binary = shutil.which("keyd.rvaiya")
    if not binary:
        return None
    try:
        proc = subprocess.run([binary, "list-keys"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    keys = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return keys or None
