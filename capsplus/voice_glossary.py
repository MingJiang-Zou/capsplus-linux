# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""术语替换表：把识别结果里**系统性的专名错字**改回来（纯函数，不碰 gi / 网络 / 麦克风）。

为什么是"替换表"而不是"热词"（2026-10-20 定，别再去试热词）：本机跑的是
sherpa-onnx **SenseVoice** int8（`~/voice-svc/daemon.py`），它的 `OfflineRecognizer`
构造参数里**没有** hotwords / contextual biasing 这一类能力 —— SenseVoice 是非自回归
CTC-ish 结构，热词要靠 `sherpa-onnx` 的 transducer + `hotwords-file` 那条线，
本机这个模型不是那条线。**没有的东西不许硬上**，所以改用一层可配置的确定性后处理：
错→对，整词匹配，改完即止。

三件事决定了匹配规则（都在 `tests/test_voice_glossary.py` 里钉着）：

1. **一次左到右扫描，每个位置取最长匹配，替换后从匹配末尾继续** —— 不许级联。
   `A→B` 与 `B→C` 同时存在时，"A" 只能变成 "B"，不能一路滑到 "C"（否则两个互逆的条目能互相喂）。
2. **ASCII 键要求词边界**：`JSQ` 不许命中 `JSQX` 或 `xJSQy`。中文键没有词边界可言
   （中文不写空格），所以改用另一条：**中文键不得短于 2 个字** —— 单字键会命中一切，
   等于把这份表变成一台随机改写机。
3. **表是数据不是代码**：内置表在 `capsplus/data/voice_glossary.tsv`，用户自己那份放
   `~/.local/share/capsplus/voice_glossary.tsv`（XDG 数据目录；本仓**不许**往
   `~/.config/capsplus/` 写东西）。同名的键以用户表为准。

⚠ 已知的边界（如实写在这里，别当成 bug 报）：中文没有分词，所以 2 字键仍可能命中一个更长词的一部分
（把 `帮晒` 写进表里，"帮忙晒太阳" 也会被改）。这是"不引分词库"的自觉取舍 ——
表里只放**系统性专名错字**（专名不会长在别的词里），别放常用词。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 指向别的表（绝对路径）；`off` / `0` / `none` / 空串 = 关掉替换。
ENV = "CAPSPLUS_VOICE_GLOSSARY"
OFF_VALUES = ("0", "off", "none", "no", "false")
BUILTIN_RELPATH = ("capsplus", "data", "voice_glossary.tsv")
# 用户自己那份。**不是** ~/.config/capsplus：那个目录在本项目里是禁碰区。
USER_SUBPATH = ("capsplus", "voice_glossary.tsv")

# 键长度下限。中文键 1 个字 = 命中一切；ASCII 键 1 个字符同理（`a` 会命中所有 a）。
MIN_KEY_CHARS = 2

_ASCII_WORD = re.compile(r"[A-Za-z0-9_]")


class Glossary:
    """一份生效的表。`pairs` 已经是"键长降序"的排法（最长优先的扫描要用）。"""

    def __init__(self, pairs: list[tuple[str, str]], sources: list[str] | None = None):
        merged: dict[str, str] = {}
        for wrong, right in pairs:
            if len(wrong) < MIN_KEY_CHARS or wrong == right:
                continue                       # 规则 2：太短的键直接丢，不做"猜他想干什么"
            merged[wrong] = right
        # 最长优先：同一位置上有 `JSQ` 与 `JSQX` 时先试长的，短键不许抢走长键的地盘。
        self.pairs = sorted(merged.items(), key=lambda kv: (-len(kv[0]), kv[0]))
        self.sources = list(sources or [])

    def __len__(self) -> int:
        return len(self.pairs)

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def apply(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """返回（改完的文本，本次命中的条目）。**命中的是词不是全文**，可以进日志。"""
        if not text or not self.pairs:
            return text, []
        out: list[str] = []
        hits: list[tuple[str, str]] = []
        i = 0
        n = len(text)
        while i < n:
            for wrong, right in self.pairs:
                if not text.startswith(wrong, i):
                    continue
                end = i + len(wrong)
                if _needs_ascii_boundary(wrong) and not _bounded(text, i, end):
                    continue
                out.append(right)
                hits.append((wrong, right))
                i = end
                break
            else:
                out.append(text[i])
                i += 1
        return "".join(out), hits


def _needs_ascii_boundary(key: str) -> bool:
    """键里有 ASCII 字母/数字才要词边界；纯中文键走"长度 ≥2"那条。"""
    return bool(_ASCII_WORD.search(key))


def _bounded(text: str, start: int, end: int) -> bool:
    """左右紧邻的字符都不是 ASCII 词字符 —— `JSQ` 不命中 `JSQX`，也不命中 `xJSQ`。"""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not (before and _ASCII_WORD.match(before)) and not (after and _ASCII_WORD.match(after))


def parse_tsv(content: str) -> list[tuple[str, str]]:
    """`错误<TAB>正确` 每行一条。`#` 开头与空行忽略；列数不对的行也忽略（表坏了不该炸链路）。

    分隔符**只认 TAB**：中文表里随时会出现全角/半角空格，拿空格当分隔符等于埋雷。
    """
    pairs: list[tuple[str, str]] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            continue
        wrong, _, right = line.partition("\t")
        wrong, right = wrong.strip(), right.strip()
        if wrong and right:
            pairs.append((wrong, right))
    return pairs


def user_path() -> Path:
    """用户那份表的位置（`XDG_DATA_HOME` 认的话跟着它走）。"""
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base).joinpath(*USER_SUBPATH)


def builtin_path() -> Path:
    """内置表：跟着本文件走（仓内 `capsplus/data/`），打包时不用另外注册路径。"""
    return Path(__file__).resolve().parent.joinpath(*BUILTIN_RELPATH[1:])


def _read(path: Path) -> list[tuple[str, str]]:
    try:
        return parse_tsv(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []


def disabled() -> bool:
    value = (os.environ.get(ENV) or "").strip()
    return value.lower() in OFF_VALUES


def load() -> Glossary:
    """生效的表 = 内置表 + 用户表（用户表里的同名键覆盖内置）。

    `CAPSPLUS_VOICE_GLOSSARY=<路径>` 时空表也要读那个文件 —— **文件不存在就当空表**，
    不许因此让识别整条链路失败（这一步在 `voice_asr.recognize()` 的成功路径上）。
    """
    if disabled():
        return Glossary([], ["（CAPSPLUS_VOICE_GLOSSARY 关掉了）"])
    sources: list[str] = []
    pairs: list[tuple[str, str]] = []
    override = (os.environ.get(ENV) or "").strip()
    if override:
        path = Path(override).expanduser()
        pairs += _read(path)
        sources.append(str(path))
        return Glossary(pairs, sources)
    built_in = builtin_path()
    pairs += _read(built_in)
    if built_in.exists():
        sources.append(str(built_in))
    user = user_path()
    if user.exists():
        pairs += _read(user)                  # 后读的覆盖先读的（dict 后写赢）
        sources.append(str(user))
    return Glossary(pairs, sources)


def apply(text: str, glossary: Glossary | None = None) -> tuple[str, list[tuple[str, str]]]:
    """便捷入口：`apply(text)` 每次现读表（表很小，一行几微秒；省得缓存与文件改动对不上）。"""
    return (glossary or load()).apply(text)


def describe(glossary: Glossary | None = None) -> str:
    """一行状态（给 `capsplus-voice status` 用）：生效条数与来源，**不含任何用户内容**。"""
    if disabled():
        return f"关（{ENV}）"
    g = glossary or load()
    if not g:
        return f"空表 —— 要加就编辑 {user_path()}"
    return f"{len(g)} 条（{' + '.join(g.sources) or '无来源'}）"