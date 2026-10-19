# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""Capslock+ 键位映射的唯一真相源。

本表译自官方 Capslock+ v3.3.0（https://github.com/wo52616111/capslock-plus，GPL-2.0）
源码 lib/lib_keysSet.ahk 的 keySchemeInit_capslox() 与 lib/lib_keysFunction.ahk；
翻译过程中另有一份手抄的 keyd 参考配置用来做等价验收，位置由环境变量
CAPSPLUS_REF_CONF 指（各人机器路径不同，仓里不写死；没有就跳过那条验收用例）。

设计取舍：用 Python 常量而不是 TOML —— 注释可以直接贴在被注释的那一行旁边，
而 `tomllib` 是只读的、无法把注释带回来，渲染时还得再维护一份注释表（=第二份真相）。
用 Python 常量则「数据 + 注释」同处一地，renderer 只管排版。

结构约定：
    SECTIONS = [(段名, [ (键, 值), ... ]), ...]   段的先后顺序有意义，必须是 list
渲染规则（见 render.py）：
    - 段落里独立成项的字符串 = 注释行（原样输出）
    - (键, 值) 元组 = 绑定行
    - 值以 "macro(" 开头的绑定行，渲染时**绝不加行尾注释**（keyd 解析器会吃掉行尾剩余内容，
      把注释当成宏 token 报 `invalid key or action` —— 2026-10-15 实测踩过）
"""

from .env import BINDIR_TOKEN

# 短按 Caps = 原大小写切换（官方源码 line23 press_caps=toggleCapsLock），与原版一致
MAIN_SECTION = "main"

# 段名 -> 动作名。render 只用它排版；改段名要连 render 的断言一起改
PRIMARY_LAYER = "capl"
COMPOSITE_LAYERS = ["capl+alt"]


def _macro(*keys: str) -> str:
    """宏动作值：macro(a b c)"""
    return "macro(" + " ".join(keys) + ")"


# 按键名。keyd 的键名表见 `<keyd> list-keys`（各发行版二进制名不同，见 capsplus/env.py）；
# 这里只把用到的拎出来当常量，
# 免得宏里手写错字（漏写一个 up 就是少走一行）。
UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
S_UP, S_DOWN, S_LEFT, S_RIGHT = "S-up", "S-down", "S-left", "S-right"
C_LEFT, C_RIGHT = "C-left", "C-right"
S_C_LEFT, S_C_RIGHT, S_C_HOME, S_C_END = "S-C-left", "S-C-right", "S-C-home", "S-C-end"
HOME, END, ENTER, BACKSPACE, DELETE = "home", "end", "enter", "backspace", "delete"

# keyd 宏长度硬边界（2026-10-15 用虚拟键盘实测，别凭感觉改）：
#   一条 macro(...) 累计发出的按键事件 ≤15 个才可靠 —— up×15 ✓ / up×16 ✗（整条丢）
#   / up×30 → 只发出 14 个 / up×40 → 8 个（乱发，数量不单调，像缓冲区溢出）。
# 所以原版的 30 次重复在本机不可实现，长步长改用翻页键（一页 ≈ 30 行，语义等价）。
N_SHORT = 10    # 原版 moveUp(10) → 10 个事件，安全
N_SELECT = 7    # 原版 selectUp(10) = 20 个事件（超界）→ 压到 7 行 = 14 个事件，实测可用


def _rep(action: str, n: int) -> str:
    return _macro(*([action] * n))


# ---------------------------------------------------------------------------
# [main]
# ---------------------------------------------------------------------------
MAIN = [
    ("capslock", f"overload({PRIMARY_LAYER}, capslock)"),
]

# ---------------------------------------------------------------------------
# [capl] 基础层
# ---------------------------------------------------------------------------
CAPL = [
    "---- 光标移动 ----",
    ("e", UP),
    ("d", DOWN),
    ("s", LEFT),
    ("f", RIGHT),
    ("a", C_LEFT),
    ("g", C_RIGHT),
    ("t", _rep(UP, N_SHORT)),
    ("b", _rep(DOWN, N_SHORT)),
    "---- 选中 ----",
    ("i", S_UP),
    ("k", S_DOWN),
    ("j", S_LEFT),
    ("l", S_RIGHT),
    ("h", S_C_LEFT),
    ("u", "S-home"),
    ("o", "S-end"),
    ("y", _rep(S_UP, N_SELECT)),
    ("n", _rep(S_DOWN, N_SELECT)),
    ("dot", S_C_RIGHT),
    ("comma", _macro(C_LEFT, S_C_RIGHT)),
    "---- 删除 ----",
    ("w", BACKSPACE),
    ("r", DELETE),
    # keyd 里 S-home 是"选中到行首"，先 end 再 S-home 正好吃掉整行
    ("backspace", _macro(END, "S-home", BACKSPACE)),
    ("slash", _macro("S-end", BACKSPACE)),
    "---- 行首/行尾 ----",
    ("p", HOME),
    ("semicolon", END),
    "---- 剪贴板(系统) ----",
    ("c", "C-c"),
    ("x", "C-x"),
    ("v", "C-v"),
    "---- Qbar ----",
    # 家目录不写死：@CAPSPLUS_BINDIR@ 在渲染时换成当前用户的 bin 目录（capsplus/env.py）
    ("q", f"command({BINDIR_TOKEN}/keyd-qbar-cmd)"),
    "---- 插入 ----",
    ("space", ENTER),
    ("enter", _macro(END, ENTER)),
]

# ---------------------------------------------------------------------------
# [capl+alt] 增强层（原版设计规律：Alt 加强 = 步长更大 / 粒度更粗）
# ---------------------------------------------------------------------------
CAPL_ALT = [
    "---- 增强版光标移动 ----",
    ("e", _macro(UP, UP, UP)),
    ("d", _macro(DOWN, DOWN, DOWN)),
    ("s", _macro(LEFT, LEFT, LEFT, LEFT, LEFT)),
    ("f", _macro(RIGHT, RIGHT, RIGHT, RIGHT, RIGHT)),
    ("a", _macro(C_LEFT, C_LEFT, C_LEFT)),
    ("g", _macro(C_RIGHT, C_RIGHT, C_RIGHT)),
    ("t", "pageup"),
    ("b", "pagedown"),
    "---- 增强版选中 ----",
    ("i", _macro(S_UP, S_UP, S_UP)),
    ("k", _macro(S_DOWN, S_DOWN, S_DOWN)),
    ("j", _macro(S_LEFT, S_LEFT, S_LEFT, S_LEFT, S_LEFT)),
    ("l", _macro(S_RIGHT, S_RIGHT, S_RIGHT, S_RIGHT, S_RIGHT)),
    ("h", _macro(S_C_LEFT, S_C_LEFT, S_C_LEFT)),
    ("u", _macro(S_C_HOME)),
    ("o", _macro(S_C_END)),
    ("y", "S-pageup"),
    ("n", "S-pagedown"),
    ("dot", _macro(S_C_RIGHT, S_C_RIGHT, S_C_RIGHT)),
    ("comma", _macro(HOME, "S-end")),
    "---- 增强版删除 ----",
    ("r", _macro(S_C_RIGHT, DELETE)),
    ("w", _macro(S_C_LEFT, DELETE)),
    ("backspace", _macro("C-a", DELETE)),
    ("slash", _macro(S_C_END, BACKSPACE)),
    "---- 增强版跳转 ----",
    ("p", _macro("C-home")),
    ("semicolon", _macro("C-end")),
]

# 渲染顺序 = 这个 list 的顺序。复合层 [capl+alt] 必须排在成员层 [capl] 之后
SECTIONS = [
    (PRIMARY_LAYER, CAPL),
    ("capl+alt", CAPL_ALT),
]

# ---------------------------------------------------------------------------
# 设置页的键位全表（"这个软件一共能配哪些键位" —— 与 SECTIONS 是两件事）
# ---------------------------------------------------------------------------
# SECTIONS 回答的是"仓库默认绑定了什么"（渲染的唯一真相源）；
# EDITABLE_KEYS 回答的是"设置页里能编辑哪些键位"。两者差出来的部分 = **空位**：
# 参考实现声明过、本仓默认没绑定的键（数字键 / F1-F12 / 字母 m、z / 一批符号）。
#
# 来源：官方 lib/lib_keysSet.ahk 的 keySchemeInit_capslox()（默认方案）里 keyset.caps_* 与
# keyset.caps_lalt_* 的**全部声明**（2026-10-19 逐行数出来的：capl 64 个、capl+alt 52 个）。
# AHK 名 -> keyd 名：backquote=grave、quote=apostrophe、leftSquareBracket=leftbrace、
# rightSquareBracket=rightbrace、ralt=rightalt；其余同名。顺序照参考实现的声明顺序，
# 所以设置页里空位那一块也是"字母 -> 数字 -> 符号 -> F 键"这个先后。
#
# 空位**不渲染**（render 只读 SECTIONS）：keyd 的层对没绑定的键是**放行** ——
# keyd 的 man 页「COMPOSITE LAYERS」一节原句："pressing control+alt+f1 preserves those modifiers,
# emitting exactly what was pressed, as there is no explicit binding for f1 on the composite
# layer"。也就是说按住 Caps 按数字键，本来就照原样出数字，默认行为一个字都没变；
# 用户在设置页给空位填了动作，才由覆盖层把这条追加到该段段尾（`merge_overrides` 的既有规则）。
_LETTERS = list("abcdefghijklmnopqrstuvwxyz")
# 顺序照参考实现：1..9 再 0（对应上游 winbind 的第 1..10 个槽位）
_DIGITS = list("1234567890")
# 参考实现 capl 层声明过的符号与特殊键（grave 排在字母与数字之间，这是上游的顺序）
_SYMBOLS_HEAD = ["grave"]
_SYMBOLS_TAIL = [
    "minus", "equal", "backspace", "tab", "leftbrace", "rightbrace", "backslash",
    "semicolon", "apostrophe", "enter", "comma", "dot", "slash", "space", "rightalt",
]
_FKEYS = [f"f{i}" for i in range(1, 13)]


def _editable_matrix(*, fkeys: bool) -> list[str]:
    """一层可编辑键位的声明序。F 键只有基础层有（上游的 caps_lalt_* 里没有 F 键）。"""
    keys = _LETTERS + _SYMBOLS_HEAD + _DIGITS + _SYMBOLS_TAIL
    return keys + _FKEYS if fkeys else keys


EDITABLE_KEYS = {
    PRIMARY_LAYER: _editable_matrix(fkeys=True),
    "capl+alt": _editable_matrix(fkeys=False),
}

# 设置页里空位那一行的"说明"列文案（动作列留空 —— 空位就是没有动作，别填假动作）
EMPTY_KEY_NOTE = ("空位：仓库默认没绑定（按住 Caps 时这个键按原样输出）；"
                  "在这行填个动作 → 「改这一格」就绑上了")

# [ids] 段：* = 所有键盘
IDS = "*"

# 由映射生成的键表（README 用），(键, 说明)；说明写人话，不写 keyd 语法
KEY_TABLE_NOTES = {
    "e/d/s/f": "光标 上/下/左/右",
    "a/g": "按词左/右跳",
    "t/b": "大段上/下滚（10 行 / 增强：翻页）",
    "i/k/j/l": "选中 上/下/左/右",
    "h": "选中到词首",
    "u/o": "选中到行首/行尾",
    "y/n": "大段选中（7 行 / 增强：选中翻页）",
    "dot": "选中到词尾",
    "comma": "选中当前词",
    "w/r": "删词 / 删字符",
    "backspace": "删整行",
    "slash": "删到行尾",
    "p/semicolon": "行首 / 行尾",
    "c/x/v": "复制 / 剪切 / 粘贴（系统剪贴板）",
    "q": "打开 Qbar（启动器）",
    "space": "回车",
    "enter": "到行尾并回车",
}

# 增强层（Caps+Alt）里与基础层不同、且值得写进 README 的条目
KEY_TABLE_NOTES_ALT = {
    "e/d/s/f": "光标大步移动（3 行 / 5 字符）",
    "a/g": "按词跳 3 次",
    "t/b": "上/下翻页（近似的 30 行）",
    "i/k": "选中上/下 3 行",
    "j/l": "选中左/右 5 字符",
    "h": "选中左 3 个词",
    "u/o": "选中到文首/文尾（C-home / C-end）",
    "y/n": "选中上/下翻页",
    "dot": "选中到词尾 ×3",
    "comma": "选中整行（home 起、S-end 止）",
    "r": "删到词尾",
    "w": "删到词首",
    "backspace": "全选删除（等价清空全文）",
    "slash": "删到文尾",
    "p/semicolon": "文首 / 文尾",
}
