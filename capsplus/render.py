# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""keyd 配置渲染器：把 mapping_data 的常量渲染成 /etc/keyd/*.conf 文本。

硬规矩（踩过的坑，别删）：
  macro(...) 行**行尾不能有任何注释**。keyd 的宏行解析器会吃掉行尾剩余内容，
  把注释当成宏 token，整行报 `invalid key or action`（2026-10-15 实测）。
  所以这里的 render 对 macro 行只输出 `键 = 值`，注释一律单独成行。
"""

from . import mapping_data as MD
from .env import resolve_bindir_token

HEADER = [
    "Capslock+ v3.3.0 -> Linux keyd（本文件由 capsplus render 生成，请勿手改）",
    "要改键位：改 capsplus/mapping_data.py，或写用户覆盖层 ~/.config/capsplus/mappings.toml",
    "许可: GPL-2.0-only —— 本文件是衍生作品，再分发必须同样以 GPL-2.0 提供源码",
    "上游: CapsLock+ v3.3.0  https://github.com/wo52616111/capslock-plus （GPL-2.0, (c) wo52616111）",
    "      按键映射译自官方源码 lib/lib_keysSet.ahk 的 keySchemeInit_capslox() + lib/lib_keysFunction.ahk",
    "      本仓改了什么: 见 README「许可与上游」",
    "",
    "语义: 短按 Caps = 原大小写切换; 按住 Caps = [capl] 层; 再加 Alt = [capl+alt] 增强层",
    "注意: macro(...) 行尾不许写注释, 否则解析报 invalid key or action（本渲染器已保证）",
    "回滚: capsplus apply 出错会自动回滚; 手工回滚见 ~/.local/state/capsplus/keyd-conf-backup/",
]


def _format_binding(key: str, action: str) -> str:
    """渲染一条绑定。

    两条硬规矩的落点：
      1. macro 行绝不带行尾注释 —— 唯一落点在这里；
      2. `@CAPSPLUS_BINDIR@` 只在此刻换成真实目录（家目录不许写死在数据里），
         顺带把"路径含空格"这种 keyd 装不下的情况当场报出来。
    """
    return f"{key} = {resolve_bindir_token(action)}"


def render(mapping_data=None) -> str:
    """渲染完整 keyd 配置文本（结尾带换行）。

    `mapping_data` 省略 = 仓库默认（`capsplus/mapping_data.py`）；
    传 `mapping_overrides.merge_overrides()` 的结果 = 默认 + 用户覆盖层。
    """
    mapping_data = MD if mapping_data is None else mapping_data
    out = ["#" + "=" * 66]
    for line in HEADER:
        out.append(("# " + line).rstrip())
    out.append("#" + "=" * 66)

    # ---- [ids] ----
    out += ["", "[ids]", mapping_data.IDS]

    # ---- [main] ----
    out += ["", "[main]"]
    for entry in mapping_data.MAIN:
        if isinstance(entry, str):
            out.append("# " + entry)
        else:
            out.append(_format_binding(*entry))

    # ---- 各层 ----
    for section, rows in mapping_data.SECTIONS:
        out += ["", f"[{section}]"]
        for entry in rows:
            if isinstance(entry, str):
                # 分组注释单独成行（mapping_data 里存的是 "---- 光标移动 ----" 的裸名字）
                out.append(f"# ---- {entry.strip('- ')} ----")
            else:
                out.append(_format_binding(*entry))

    return "\n".join(out) + "\n"


def normalized_bindings(text: str) -> set[tuple[str, str, str]]:
    """归一化成「绑定集合」：去掉注释与空行、拆出 (段, 键, 值)。

    用于渲染等价性验收：与参考文件归一化后必须完全相等。
    同时断言：macro 行不带行尾注释（真出问题就在这里炸，而不是等 keyd 报错）。
    """
    bindings: set[tuple[str, str, str]] = set()
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            # [ids] 段里的 "*" 这种裸行：当成 (段, 行内容, "") 记进集合
            bindings.add((section, line, ""))
            continue
        key, _, action = line.partition("=")
        key, action = key.strip(), action.strip()
        if action.startswith("macro("):
            # 宏行的值必须正好是 macro(...) 且括号闭合，行尾不许有别的 token
            if not action.endswith(")") or " #" in action:
                raise ValueError(f"macro 行带行尾注释或多余内容: {raw!r}")
        bindings.add((section, key, action))
    return bindings


def _key_table(notes, prefix: str) -> list[str]:
    """键表直接用注释表生成：键写 "e/d"、说明写人话，一行一组，顺序就是人读的顺序。"""
    out = ["| 组合键 | 功能 |", "| --- | --- |"]
    for keys, note in notes.items():
        combo = " / ".join(f"`{prefix}{k}`" for k in keys.split("/"))
        out.append(f"| {combo} | {note} |")
    return out


def render_key_table(which: str = "capl", mapping_data=None) -> str:
    """给 README 用的 markdown 键表（由映射数据生成，绝不手写 —— 改映射即改文档）。"""
    mapping_data = MD if mapping_data is None else mapping_data
    if which == "capl":
        return "\n".join(_key_table(mapping_data.KEY_TABLE_NOTES, "Caps+"))
    return "\n".join(_key_table(mapping_data.KEY_TABLE_NOTES_ALT, "Caps+Alt+"))


def binding_notes(which: str = "capl") -> dict[str, str]:
    """键 -> 一句人话说明（设置界面表格用）。

    说明表是按 "e/d/s/f" 这种组合键写的，这里拆开倒排成单键；一个键出现在多组里
    （如 `p/semicolon`）时后面的组不覆盖前面的，保证结果稳定。
    """
    notes = MD.KEY_TABLE_NOTES if which == "capl" else MD.KEY_TABLE_NOTES_ALT
    out: dict[str, str] = {}
    for combo, note in notes.items():
        for key in combo.split("/"):
            out.setdefault(key.strip(), note)
    return out


def editable_keys(which: str) -> list[str]:
    """该层设置页要列出的**键位全表**（含空位），顺序照参考实现的声明顺序。

    空位 = 参考实现声明过、仓库默认没绑定的键（数字键 / F1-F12 / 字母 m、z 之类）。
    表外的键（覆盖层自己造的名字）不在这一列里，但 `binding_rows` 照样会把它列出来。
    """
    return list(MD.EDITABLE_KEYS.get(which, ()))


def binding_rows(which: str, mapping_data=None) -> list[tuple[str, str, str, str]]:
    """一个层的**设置页行表**：[(键, 动作, 说明, 来源)]，来源 = "默认" / "覆盖"。

    设置界面的「当前生效映射」表格直接用这个（不解析 Python 源码，也不解析 conf 文本）。

    行分三类，顺序也是这三类（2026-10-19 加空位，起因：用户要"设置页里能配数字键与 F1-F12"）：
      ① 仓库默认声明过的键 —— 位置不动；被覆盖层改过就换成覆盖值、来源变「覆盖」。
      ② 空位 —— 参考实现声明过、仓库默认没绑定的键（数字键 / F1-F12 / m、z 等）：
         动作是空串、说明写"空位"、来源算「默认」（它的状态确实来自仓库默认 = 没绑定）。
         keyd 的层对没绑定的键是**放行**（按住 Caps 按数字键本来就照原样出数字），
         所以空位**不渲染**、默认行为一个字都不变；用户在设置页给空位填了动作才真的生成绑定。
      ③ 覆盖层新加的键 —— 追加在段尾（`merge_overrides` 的既有规则，与渲染出来的位置一致）。

    动作与渲染出来的一致（`@CAPSPLUS_BINDIR@` 已换成真实目录）—— 界面上看到什么，
    keyd 配置里就是什么，不许出现"表格里是记号、配置里是路径"两套话。
    """
    mapping_data = MD if mapping_data is None else mapping_data
    sections = dict(getattr(mapping_data, "SECTIONS", MD.SECTIONS))
    rows = sections.get(which, [])
    notes = binding_notes(which)
    default = dict(MD.SECTIONS).get(which, [])
    default_map = {e[0]: e[1] for e in default if not isinstance(e, str)}
    declared: list[tuple[str, str, str, str]] = []
    appended: list[tuple[str, str, str, str]] = []      # 覆盖层新加的：仍在段尾
    present: set[str] = set()
    for entry in rows:
        if isinstance(entry, str):
            continue
        key, action = entry
        present.add(key)
        item = (key, resolve_bindir_token(action), notes.get(key, ""),
                "默认" if default_map.get(key) == action else "覆盖")
        (declared if key in default_map else appended).append(item)
    empty = [(key, "", MD.EMPTY_KEY_NOTE, "默认")
             for key in editable_keys(which) if key not in present]
    return declared + empty + appended
