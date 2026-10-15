"""Capslock+ 键位映射的唯一真相源。

搬自参考文件 /home/zmj/.dsh/refs/capslock-v3.3.0/keyd-capslock-plus.conf
（该文件又逐条对照官方 Capslock+ v3.3.0 源码 lib/lib_keysSet.ahk 的
keySchemeInit_capslox() 与 lib/lib_keysFunction.ahk 写成）。

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

# 短按 Caps = 原大小写切换（官方源码 line23 press_caps=toggleCapsLock），与原版一致
MAIN_SECTION = "main"

# 段名 -> 动作名。render 只用它排版；改段名要连 render 的断言一起改
PRIMARY_LAYER = "capl"
COMPOSITE_LAYERS = ["capl+alt"]


def _macro(*keys: str) -> str:
    """宏动作值：macro(a b c)"""
    return "macro(" + " ".join(keys) + ")"


# 按键名。keyd 的键名表见 `keyd.rvaiya list-keys`；这里只把用到的拎出来当常量，
# 免得宏里手写错字（漏写一个 up 就是少走一行）。
UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
S_UP, S_DOWN, S_LEFT, S_RIGHT = "S-up", "S-down", "S-left", "S-right"
C_LEFT, C_RIGHT = "C-left", "C-right"
S_C_LEFT, S_C_RIGHT, S_C_HOME, S_C_END = "S-C-left", "S-C-right", "S-C-home", "S-C-end"
HOME, END, ENTER, BACKSPACE, DELETE = "home", "end", "enter", "backspace", "delete"

# 长按重复次数：10 / 30 这两个数字来自原版源码的重复计数，别随手改
N_SHORT = 10
N_LONG = 30


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
    ("y", _rep(S_UP, N_SHORT)),
    ("n", _rep(S_DOWN, N_SHORT)),
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
    ("t", _rep(UP, N_LONG)),
    ("b", _rep(DOWN, N_LONG)),
    "---- 增强版选中 ----",
    ("i", _macro(S_UP, S_UP, S_UP)),
    ("k", _macro(S_DOWN, S_DOWN, S_DOWN)),
    ("j", _macro(S_LEFT, S_LEFT, S_LEFT, S_LEFT, S_LEFT)),
    ("l", _macro(S_RIGHT, S_RIGHT, S_RIGHT, S_RIGHT, S_RIGHT)),
    ("h", _macro(S_C_LEFT, S_C_LEFT, S_C_LEFT)),
    ("u", _macro(S_C_HOME)),
    ("o", _macro(S_C_END)),
    ("y", _rep(S_UP, N_LONG)),
    ("n", _rep(S_DOWN, N_LONG)),
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

# [ids] 段：* = 所有键盘
IDS = "*"

# 由映射生成的键表（README 用），(键, 说明)；说明写人话，不写 keyd 语法
KEY_TABLE_NOTES = {
    "e/d/s/f": "光标 上/下/左/右",
    "a/g": "按词左/右跳",
    "t/b": "大段上/下滚（10 行 / 增强 30 行）",
    "i/k/j/l": "选中 上/下/左/右",
    "h": "选中到词首",
    "u/o": "选中到行首/行尾",
    "y/n": "大段选中（10 行 / 增强 30 行）",
    "dot": "选中到词尾",
    "comma": "选中当前词",
    "w/r": "删词 / 删字符",
    "backspace": "删整行",
    "slash": "删到行尾",
    "p/semicolon": "行首 / 行尾",
    "c/x/v": "复制 / 剪切 / 粘贴（系统剪贴板）",
    "space": "回车",
    "enter": "到行尾并回车",
}

# 增强层（Caps+Alt）里与基础层不同、且值得写进 README 的条目
KEY_TABLE_NOTES_ALT = {
    "e/d/s/f": "光标大步移动（3 行 / 5 字符）",
    "a/g": "按词跳 3 次",
    "t/b": "滚 30 行",
    "i/k": "选中上/下 3 行",
    "j/l": "选中左/右 5 字符",
    "h": "选中左 3 个词",
    "u/o": "选中到文首/文尾（C-home / C-end）",
    "y/n": "选中 30 行",
    "dot": "选中到词尾 ×3",
    "comma": "选中整行（home 起、S-end 止）",
    "r": "删到词尾",
    "w": "删到词首",
    "backspace": "全选删除（等价清空全文）",
    "slash": "删到文尾",
    "p/semicolon": "文首 / 文尾",
}
