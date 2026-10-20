#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""刷新 `README.md` 里那两张键位表 —— **README 手工维护，生成器只碰生成区**。

契约（2026-10-20 事故复盘后重写，旧版行为见下）：
  * `README.md` 是**唯一真相源**，由人手写。两处键位表用
    `<!-- BEGIN GENERATED: <名> -->` … `<!-- END GENERATED: <名> -->` 夹住（**两个标记各自独占一行**），
    生成器**只重写夹住的那几行**，正文一个字节都不碰（结构上不可能被覆盖）。
  * 三道闸，任何一道不过就**非零退出、一个字节都不写**：
      A. 两个生成区必须各出现一次、BEGIN 在 END 之前 —— 标记缺了或被改坏就拒绝，
         绝不在"找不到地方"的情况下猜着写；
      B. README 的**正文**（生成区之外）不许有未提交的改动（对 `git show HEAD:README.md` 比）——
         生成器只在干净树上跑，这样生成的 diff 里只剩"键表变了"一件事，事后查得清；
      C. 拼装完再自证一次：正文逐字节等于原文。结构上不可能变，仍然验一遍 —— 闸门自己也要能被验。
  * 不能判定时（没有 git / 解压出来的副本里没有 HEAD）B 跳过并打印一行提示；
    A 与 C 是结构性的，不依赖 git，正文照样安全。

旧版为什么不这么写（2026-10-20 实测的伤害）：它拿 `README.template.md` **整份覆盖** README，
而 README 一直在被手写（历史提交只改 README、不改模板）⇒ 跑一次生成就把 README 里手工加过的
整节删掉（实测删了「安装：一条命令，一份实现」共 105 行，靠 git 才捞回来），
而且模板仍缺那几节 ⇒ 下一个人再跑一次还会再删一次。那份模板是"第二份真相"，已退役，本脚本不再读它。
"""

import difflib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus.render import render_key_table  # noqa: E402

TARGET = REPO / "README.md"

# 生成区：标记名 → 渲染哪一层（键表不手写，改映射即改文档）
REGIONS = (("key-table:capl", "capl"), ("key-table:capl+alt", "capl+alt"))
BEGIN = "<!-- BEGIN GENERATED: {name} -->"
END = "<!-- END GENERATED: {name} -->"
PLACEHOLDER = "@@GENERATED@@"          # 比正文时把生成区折成它


def _locate(text: str, name: str) -> tuple:
    """找生成区里**内容**的起止下标 —— 内容是 `BEGIN` 那行之后到 `END` 那行之前（含收尾换行）。

    布局是硬契约（`BEGIN` 与 `END` 都独占一行），破了就抛 `ValueError`：
        2026-10-20 第一版按"标记字符串之后"定位，**把 BEGIN 后的换行当成了区内容**，
        于是拼装时那个换行被吞掉、表格粘到注释行后面（markdown 里那张表就废了）。
        ⚠ 那个 bug **护栏 C 抓不到**（换行在生成区里面，正文确实一字节没变），
        是判据 ①「干净树必须逐字节 no-op」把它抓出来的 —— 所以两道都得留着。
    """
    begin, end = BEGIN.format(name=name), END.format(name=name)
    nb, ne = text.count(begin), text.count(end)
    if nb != 1 or ne != 1:
        raise ValueError(f"生成区标记 {name} 必须各出现一次（现在 BEGIN×{nb} / END×{ne}）")
    b0, ei = text.index(begin), text.index(end)
    if ei < b0:
        raise ValueError(f"生成区 {name} 的 END 在 BEGIN 前面")
    if b0 and text[b0 - 1] != "\n":
        raise ValueError(f"生成区 {name} 的 BEGIN 标记必须独占一行（它前面不是换行）")
    if ei and text[ei - 1] != "\n":
        raise ValueError(f"生成区 {name} 的 END 标记必须独占一行（它前面不是换行）")
    bi = b0 + len(begin)
    if text[bi:bi + 1] != "\n":
        raise ValueError(f"生成区 {name} 的 BEGIN 标记后面必须是换行（标记要独占一行）")
    return bi + 1, ei


def _body(text: str) -> str:
    """只留"正文"：每个生成区的内容折成 `PLACEHOLDER`（比正文时用，不看键表）。"""
    out, cut = [], 0
    for name, _ in REGIONS:
        bi, ei = _locate(text, name)
        out.append(text[cut:bi] + PLACEHOLDER)
        cut = ei
    out.append(text[cut:])
    return "".join(out)


def _splice(text: str, renders: dict) -> str:
    """把每个生成区的内容换成刚渲染出来的表（表后面补上那个换行，布局与原文一致）。"""
    out, cut = [], 0
    for name, _ in REGIONS:
        bi, ei = _locate(text, name)
        out.append(text[cut:bi] + renders[name] + "\n")
        cut = ei
    out.append(text[cut:])
    return "".join(out)


def _refuse(reason: str, *detail: str) -> int:
    print(f"✗ 拒绝生成：{reason}", file=sys.stderr)
    print("  （一个字节都没写。）", file=sys.stderr)
    for line in detail:
        print(line, file=sys.stderr)
    return 1


def _head_readme() -> str:
    """`HEAD:README.md` 的内容；不是 git 工作副本（或还没有 HEAD）时返回 `None`。"""
    try:
        proc = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:README.md"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _body_diff(old: str, new: str) -> list:
    """正文的差异行（护栏 B 的报告用）。"""
    diff = difflib.unified_diff(_body(old).splitlines(), _body(new).splitlines(),
                                "HEAD:README.md", "README.md", lineterm="", n=0)
    return [f"    {ln}" for ln in list(diff)[2:]]


def main() -> int:
    old = TARGET.read_text(encoding="utf-8")

    # 闸 A：生成区必须找得到、唯一、有序
    try:
        renders = {name: render_key_table(layer) for name, layer in REGIONS}
        for name, _ in REGIONS:
            _locate(old, name)
    except ValueError as exc:
        return _refuse(str(exc), "  标记长这样（键表夹在中间）：",
                       "    <!-- BEGIN GENERATED: key-table:capl -->", "    …", "    <!-- END GENERATED: key-table:capl -->")

    # 闸 B：正文不许有未提交的改动 —— 生成器只在干净树上跑
    head = _head_readme()
    if head is None:
        print("· 提示：不是 git 工作副本（或还没有 HEAD），跳过「正文未提交改动」这道闸；"
              "生成区之外本来就不会被写，正文照样是安全的。", file=sys.stderr)
    else:
        try:
            dirty = _body_diff(head, old)
        except ValueError:
            return _refuse("HEAD 里的 README 还没有生成区标记（旧格式），没法比正文 —— "
                           "先把 README 的改动 commit 掉再跑。")
        if dirty:
            return _refuse(
                f"README 的正文有未提交的改动（{len(dirty)} 行差异）。",
                "  生成器只在干净树上跑：否则「键表变了」和「谁手写了正文」会混成同一个 diff，事后查不清。",
                "  下面是你手写的部分，我没动它 —— 该 commit / stash 的是它们：",
                *dirty[:60])

    new = _splice(old, renders)

    # 闸 C：自证正文逐字节没变（结构上不可能变，仍然验一遍）
    if _body(new) != _body(old):
        return _refuse("拼装后正文变了（这不该发生，生成器有 bug）。")

    if new == old:
        print("· 无变化：README.md 的键位表已是最新（逐字节不变，没写盘）。")
        return 0

    tmp = TARGET.with_name(TARGET.name + ".gen-tmp")
    try:
        tmp.write_text(new, encoding="utf-8")
        tmp.chmod(TARGET.stat().st_mode & 0o777)
        tmp.replace(TARGET)                     # 同目录 rename，原子
    finally:
        if tmp.exists():
            tmp.unlink()
    print(f"已刷新 README.md 的键位表（{len(new.splitlines())} 行；只动了生成区，正文一字节没碰）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())