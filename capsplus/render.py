"""keyd 配置渲染器：把 mapping_data 的常量渲染成 /etc/keyd/*.conf 文本。

硬规矩（踩过的坑，别删）：
  macro(...) 行**行尾不能有任何注释**。keyd 的宏行解析器会吃掉行尾剩余内容，
  把注释当成宏 token，整行报 `invalid key or action`（2026-10-15 实测）。
  所以这里的 render 对 macro 行只输出 `键 = 值`，注释一律单独成行。
"""

from . import mapping_data as MD

HEADER = [
    "Capslock+ v3.3.0 -> Ubuntu 26.04 / keyd.rvaiya 2.5.0",
    "本文件由 capsplus render 生成，请勿手改（改动请改 capsplus/mapping_data.py）",
    "依据: 官方源码 lib/lib_keysSet.ahk 的 keySchemeInit_capslox() + lib/lib_keysFunction.ahk",
    "",
    "语义: 短按 Caps = 原大小写切换; 按住 Caps = [capl] 层; 再加 Alt = [capl+alt] 增强层",
    "注意: macro(...) 行尾不许写注释, 否则解析报 invalid key or action（本渲染器已保证）",
    "回滚: capsplus apply 出错会自动回滚; 手工回滚见 ~/.local/state/capsplus/keyd-conf-backup/",
]


def _format_binding(key: str, action: str) -> str:
    """渲染一条绑定。macro 行绝不带行尾注释 —— 这里是那条硬规矩的唯一落点。"""
    return f"{key} = {action}"


def render(mapping_data=MD) -> str:
    """渲染完整 keyd 配置文本（结尾带换行）。"""
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


def render_key_table(which: str = "capl", mapping_data=MD) -> str:
    """给 README 用的 markdown 键表（由映射数据生成，绝不手写 —— 改映射即改文档）。"""
    if which == "capl":
        return "\n".join(_key_table(mapping_data.KEY_TABLE_NOTES, "Caps+"))
    return "\n".join(_key_table(mapping_data.KEY_TABLE_NOTES_ALT, "Caps+Alt+"))
