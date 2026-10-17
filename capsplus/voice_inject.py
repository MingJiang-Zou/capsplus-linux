# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""上屏（把识别出来的中文送到光标处）—— 可插拔后端 + **运行时探测**。

为什么不能"直接打字"（本机 2026-10-17 实测，别再来一遍）：

| 路子 | 本机结果 | 怎么验的 |
| --- | --- | --- |
| `zwp_virtual_keyboard` / `zwp_input_method` | mutter **一个都没导出** | `nmongo`/`objdump -T libmutter-18.so.0` 数协议名，命中 0 |
| `wtype` | 在 GNOME 上必死（依赖上面两个协议） | 同上 |
| `libei` | 导出符号里 **keysym 数量 = 0**，只有 evdev 键码 | `nm -D /usr/lib/.../libei.so` |
| `ydotool type` / `dotool` | **打不出汉字**（逐字符映射到键盘布局） | 实测 |

⇒ 中文只有两条活路：**①剪贴板/主选区 + 键鼠事件**，**②输入法 commit 通道**（ibus 那条，
本仓没做，属于后续工作）。逐字打字方案一律不许实现。这里实现的是第①条，三种落法：

    clipboard  备份 → wl-copy → ydotool Ctrl+V → 条件还原（默认：所有能粘贴的控件都吃这套）
    primary    备份 → wl-copy --primary → ydotool 中键 → 条件还原（X11 传统的"选中即粘贴"，光标得在输入区）
    manual     什么都不注入，文本留在弹窗里，用户自己复制粘贴（零风险兜底，**也完全不碰剪贴板**）

上屏的完整时序（2026-10-17 补上剪贴板备份 / 条件还原，别再退回"只写不备"）：

    snapshot_clipboard() → wl-copy <文本> → ydotool Ctrl+V → 等 CLIPBOARD_SETTLE_MS
                         → **条件还原**（只有"剪贴板里还是我们写的那段"才还原）→ 释放快照

两条不讨喜但必须坚持的边界：

* **快照只在内存**。`Snapshot.data` 里可能是密码、银行卡号、刚截的那张图 —— 它不写进任何文件、
  不进 `voice.log`、不进事件帧；对外只报**类型与字节数**。判据：
  `tests/test_voice_clipboard.py::TestSnapshotNeverHitsDisk`。
* **还原是有条件的**。到点时如果剪贴板已经被用户 / 别的应用换了内容，**就不还原** ——
  把人家刚复制的东西冲掉，比不还原更糟。这条反过来也要能红，见
  `TestConditionalRestore`（"还是我们的" 与 "已被别人改" 两个方向各一条）。

探测为什么长这样（2026-10-17 实测）：`ydotool` **先连 daemon 再解析参数** ——
把 `YDOTOOL_SOCKET` 指到一个不存在的 socket 后，`key abc` / `key ctrl+a` / `key 30:1 30:0`
全都报同一句 "failed to connect socket"（rc=2），**参数根本没被解析过**。
所以想"跑一下 `ydotool key --help` 看语法"是行不通的。改成两条能落地的探测：
  ① 读**已安装的那个 ydotool 二进制自己的用法文本**（`Syntax: <keycode>:<pressed>` /
     `0x40 - Mouse down`）⇒ 判断这台机上的 ydotool 到底认键码还是认键名；
  ② 键码值从 `/usr/include/linux/input-event-codes.h` 现读（`#define KEY_LEFTCTRL 29`），
     不写死数字 —— 头文件里没有的键，直接判"未验证"，**不许假装能成功**。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

BACKENDS = ("clipboard", "primary", "manual")
DEFAULT_BACKEND = "clipboard"
DEFAULT_KEYCODE_HEADER = "/usr/include/linux/input-event-codes.h"
STEP_TIMEOUT_S = 5.0

# 需要的键码：Ctrl+V 粘贴 + 中键点一下
KEYCODE_NAMES = ("KEY_LEFTCTRL", "KEY_V")
CLICK_MIDDLE_DOWN = "0x42"       # 0x02 MIDDLE | 0x40 down   （用法文本里写的位掩码）
CLICK_MIDDLE_UP = "0x82"         # 0x02 MIDDLE | 0x80 up

# 二进制用法文本里的**语法标记**：命中哪条就说明它支持哪种写法
SYNTAX_MARKERS = {
    "keycode": b"Syntax: <keycode>:<pressed>",
    "click-bitmask": b"0x40 - Mouse down",
    "keysym-name": b"ctrl+a",           # 键名式写法（本项目实测的 1.0.4 里没有）
}

# ── 剪贴板备份 / 条件还原 ─────────────────────────────────────────────────────
RESTORE_ENV = "CAPSPLUS_VOICE_RESTORE_CLIPBOARD"      # =0 关掉还原（默认开）

# 粘贴之后到"允许还原"之间的缓冲。**不能是 0**：`wl-copy` 退出码 0 只表示"我挂上了选区所有权"，
# 目标应用收到 Ctrl+V 之后还要**异步**向源端要一次数据（offer → data_offer → 读管道）。
# 在这一步之前回写，等于把自己刚给出去的东西抽走，应用粘到的是空或旧内容。
# 也不能太长（早先对明江说过 800ms，那个数没有依据）：这段等待里他自己复制了别的东西，
# "还是我们写的吗"就判否 ⇒ 白等一场不还原。300ms 是按"够用就好"取的，真机要调只动这一处。
CLIPBOARD_SETTLE_MS = 300

# 快照能装多大。剪贴板里一张 4K 截图的 PNG 也就几 MB，超上限的多半是异常大的图或文件流；
# 宁可这次不还原（覆盖原有内容），也不要在常驻 worker 的内存里囤一块天大的东西。
SNAPSHOT_MAX_BYTES = 64 << 20
# 回读核对的上限：核对一次要把快照再读回来，图太大时这份成本比它换来的把握更贵 ——
# 超过就只发还原命令、不回读，摘要里如实写"未回读核对"。
VERIFY_MAX_BYTES = 8 << 20

# 备份时只认这几种类型，按优先级从上到下（一次只还原**一种**：wl-copy 没有"一次挂多种 MIME"
# 的命令行用法，硬凑第二种就是把第一份内容也丢了）。挑**最不可再生**的那种：图片 / 文件列表
# 掉了就真没了，网页正文复制一下就能再拷。
SNAPSHOT_MIME_PRIORITY = ("image/png", "text/uri-list", "text/html",
                          "text/plain;charset=utf-8", "text/plain")
# 判断"剪贴板里还是不是我们写的那段"时读的候选类型（wl-copy 写纯文本会同时注册这两种）
TEXT_MIME_CANDIDATES = ("text/plain;charset=utf-8", "text/plain")

# 时序判据钉在这四个段名上（检查器与反例具型在 `tests/test_voice_clipboard.py`）
STAGE_SNAPSHOT = "snapshot"
STAGE_WRITE = "write"
STAGE_PASTE = "paste"
STAGE_RESTORE = "restore"
KNOWN_STAGES = (STAGE_SNAPSHOT, STAGE_WRITE, STAGE_PASTE, STAGE_RESTORE)


@dataclass
class Probe:
    """一次探测的全部结论。`verified=False` 时**任何注入都不许跑**。"""
    wl_copy: str | None = None
    wl_copy_primary: bool = False
    wl_paste: str | None = None                  # 备份剪贴板要用它；缺了**只是不还原**，不影响注入
    wl_copy_clear: bool = False                  # `--help` 里有没有 `-c, --clear`（原剪贴板为空时要用）
    ydotool: str | None = None
    ydotool_subcommands: tuple[str, ...] = ()
    ydotool_daemon: bool | None = None         # None = 探测本身没跑成
    syntax: tuple[str, ...] = ()               # ("keycode","click-bitmask") 之类
    keycodes: dict = field(default_factory=dict)
    keycodes_source: str | None = None
    problems: list[str] = field(default_factory=list)
    ran: bool = False                           # 是否真的跑过外部命令（测试里必须是 False）

    @property
    def verified(self) -> bool:
        return bool(self.wl_copy and self.wl_copy_primary and self.ydotool
                    and "keycode" in self.syntax and "click-bitmask" in self.syntax
                    and set(KEYCODE_NAMES) <= set(self.keycodes)
                    and self.ydotool_daemon is True)

    @property
    def clipboard_backup(self) -> bool:
        """能不能备份。**故意不进 `verified`**：wl-paste 缺失 / 不认 --clear 只该让"还原"这件事
        做不成，不该把整条上屏链路判死（那是回退到"覆盖且不还原"的老行为，代价更大）。"""
        return bool(self.wl_paste)

    def report_lines(self) -> list[str]:
        """给人看的探测结果（`capsplus-voice status` 直接用）。"""
        out = [f"wl-copy   : {self.wl_copy or '没装（wl-clipboard）'}"
               + ("" if self.wl_copy_primary else "  ← 不认 --primary"),
               f"wl-paste  : {self.wl_paste or '没装（⇒ 备份不了，上屏会覆盖剪贴板且不还原）'}"
               + ("" if not self.wl_paste else
                  f"  ← 还原能力：--clear {'有' if self.wl_copy_clear else '没探到'}"),
               f"ydotool   : {self.ydotool or '没装'}",
               f"  用法语法 : {' / '.join(self.syntax) or '没探到（读不到二进制里的用法文本）'}",
               f"  键码来源 : {self.keycodes_source or '没读到 input-event-codes.h'}"
               + (f" → {self.keycodes}" if self.keycodes else ""),
               f"  daemon   : {self._daemon_text()}"]
        if self.problems:
            out.append("  未验证原因: " + "; ".join(self.problems))
        return out

    def _daemon_text(self) -> str:
        if self.ydotool_daemon is True:
            return "ydotoold 在跑（探测命令 rc=0）"
        if self.ydotool_daemon is False:
            return "ydotoold 不在跑 ⇒ 注入用不了"
        return "没探（探测被跳过或失败）"


@dataclass
class Step:
    """一条命令。`stdin` 走管道 —— 把用户的文本当命令行参数传会撞 ARG_MAX 与转义坑。

    `stage` 是**时序判据**看的段名（snapshot / write / paste / restore）；`describe()` 只拼
    argv，绝不拼 stdin —— 快照字节走 stdin，摘要里出现它就等于把用户剪贴板内容写进日志。
    """
    argv: list[str]
    stdin: bytes | None = None
    label: str = ""
    stage: str = ""

    def describe(self) -> str:
        return f"{self.label or self.argv[0]}: " + " ".join(self.argv)


@dataclass
class InjectResult:
    ok: bool = False
    backend: str = ""
    code: str = ""
    message: str = ""
    hint: str = ""
    commands: list[str] = field(default_factory=list)      # 实际跑过的 argv（不含文本内容）
    steps: int = 0
    keep_window: bool = False                               # manual 模式：弹窗别关
    stages: list[str] = field(default_factory=list)         # 实际执行过的段序（时序判据的证据）
    clipboard_note: str = ""                                # 备份情况一句话：**只含类型与字节数**
    restore_job: RestoreJob | None = None                   # 待还原的作业，交给 worker 的主循环定时器

    def describe(self) -> str:
        """一行结论。三种"没真上屏"的情况必须和成功区分开，不许混报。"""
        if self.code == "manual":
            return "manual: 没注入，文本留在弹窗里"
        if self.code == "needs_manual_paste":
            return f"{self.backend}: 没自动上屏（文本已进剪贴板，自己按 Ctrl+V）"
        if self.ok:
            return f"{self.backend}: 已上屏（{self.steps} 步）"
        return f"{self.backend}: 失败 {self.code or '?'} — {self.message[:120]}"


def backend_from_env() -> str:
    """`CAPSPLUS_VOICE_BACKEND`，认不出来就当默认值（但 `plan()` 会拒绝非法名）。"""
    name = (os.environ.get("CAPSPLUS_VOICE_BACKEND") or "").strip().lower()
    return name if name in BACKENDS else DEFAULT_BACKEND


# ── 探测 ─────────────────────────────────────────────────────────────────────
def read_keycodes(header: str | None = None) -> tuple[dict, str | None]:
    """从内核头文件现读键码。读不到返回 ({}, None) —— **不许退化成写死的数字**。"""
    path = header or os.environ.get("CAPSPLUS_KEYCODE_HEADER") or DEFAULT_KEYCODE_HEADER
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return {}, None
    found = {}
    for name in KEYCODE_NAMES:
        m = re.search(rf"^#define\s+{name}\s+(\d+)", text, re.M)
        if m:
            found[name] = int(m.group(1))
    return found, path


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def read_usage_markers(binary_path: str, reader=None) -> tuple[str, ...]:
    """在**已安装的那个二进制**的只读段里找用法文本，返回它声明支持的语法名。

    这是探测里最关键的一步：`ydotool` 的 1.0.4 把键名支持砍了（只认裸键码），
    而别的版本/发行版可能不一样 —— 所以问二进制本身，不问网上的文档。
    """
    read = reader or _read_bytes
    try:
        blob = read(binary_path)
    except (OSError, ValueError):
        return ()
    return tuple(name for name, marker in SYNTAX_MARKERS.items()
                 if isinstance(marker, bytes) and marker in blob)


def run_probe(which=None, help_runner=None, binary_reader=None,
              daemon_probe=None, header: str | None = None) -> Probe:
    """跑一遍探测。任何一步失败都只是记进 `problems`，**不抛异常**。

    四个注入口（`which` / `help_runner` / `binary_reader` / `daemon_probe`）的用意：
    单测必须能**完全不碰真系统**地把探测逻辑验一遍（判据 `tests/test_voice_inject.py`
    断言假探针跑出来的 `Probe.ran is False`）；真机上一条注入都不给，走默认实现。
    默认的 `help_runner` / `binary_reader` / `daemon_probe` 一旦真跑了外部命令就把
    `p.ran` 置真 —— 于是"这条用例有没有碰真系统"在测试里是可断言的，不是靠人记得。
    """
    p = Probe()
    which = which or shutil.which

    def _help(argv):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=STEP_TIMEOUT_S)
        p.ran = True
        return (proc.returncode or 0), ((proc.stdout or "") + (proc.stderr or ""))

    def _read_binary(path):
        p.ran = True
        return _read_bytes(path)

    def _daemon():
        rc, note = _daemon_liveness(p.ydotool)
        p.ran = True
        return rc, note

    help_runner = help_runner or _help
    binary_reader = binary_reader or _read_binary
    daemon_probe = daemon_probe or _daemon

    p.wl_copy = which("wl-copy")
    if not p.wl_copy:
        p.problems.append("没装 wl-copy（wl-clipboard）")
    else:
        try:
            _rc, blob = help_runner([p.wl_copy, "--help"])
            p.wl_copy_primary = "--primary" in blob
            # `--clear` 决定"原本剪贴板是空的"这一种情况能不能真还原成空。本机 wl-clipboard 2.2.1
            # 实测有（`-c, --clear  Instead of copying, clear the clipboard.`），但**不许假设别的
            # 机器也有**：探不到就走"不还原 + 说明"那条退路（见 inject 里的 clipboard_note）。
            p.wl_copy_clear = "--clear" in blob
            if not p.wl_copy_primary:
                p.problems.append("这个 wl-copy 不认 --primary")
        except (OSError, subprocess.TimeoutExpired) as exc:
            p.problems.append(f"wl-copy --help 跑不了：{type(exc).__name__}")
    p.wl_paste = which("wl-paste")        # 读不回原物 ⇒ 只是不还原，不进 verified

    p.keycodes, p.keycodes_source = read_keycodes(header)

    p.ydotool = which("ydotool")
    if not p.ydotool:
        p.problems.append("没装 ydotool")
        return p

    try:
        _rc, blob = help_runner([p.ydotool, "help"])
        p.ydotool_subcommands = tuple(w for w in ("key", "click", "type")
                                      if re.search(rf"^\s+{w}\s*$", blob, re.M))
        if "key" not in p.ydotool_subcommands:
            p.problems.append("`ydotool help` 的输出里没有 key 子命令")
    except (OSError, subprocess.TimeoutExpired) as exc:
        p.problems.append(f"ydotool help 跑不了：{type(exc).__name__}")

    p.syntax = read_usage_markers(p.ydotool, reader=binary_reader)
    if not p.syntax:
        p.problems.append("读不到 ydotool 的用法文本（判定不了它认键码还是键名）")
    elif "keysym-name" in p.syntax and "keycode" not in p.syntax:
        p.problems.append("这版 ydotool 只认键名（ctrl+a 式），本模块的键码序列不适用")

    missing = [n for n in KEYCODE_NAMES if n not in p.keycodes]
    if missing:
        p.problems.append(f"键码表里缺 {'/'.join(missing)}（头文件 {p.keycodes_source or '没读到'}）")

    # daemon 在不在：**空参数**跑 `ydotool key` —— 0 个键码 = 0 个事件，不会往用户屏幕打任何东西
    alive, _note = daemon_probe()
    p.ydotool_daemon = alive
    if alive is False:
        p.problems.append("ydotoold 没在跑（需要 root 起 daemon，或改回 manual 后端）")
    return p


def _daemon_liveness(binary: str) -> tuple[bool | None, str]:
    try:
        proc = subprocess.run([binary, "key"], capture_output=True, text=True,
                              timeout=STEP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}"
    blob = (proc.stderr or "") + (proc.stdout or "")
    if proc.returncode == 0:
        return True, "daemon 可达"
    if "ydotoold" in blob or "socket" in blob:
        return False, blob.strip()[:120]
    return None, blob.strip()[:120]


# ── 命令序列（纯函数，测试就靠它）────────────────────────────────────────────
def key_seq(name: str, press: bool, keycodes: dict) -> list[str]:
    """`KEY_LEFTCTRL` 按下 → `['29:1']`。键码表里没有就抛 KeyError（调用方当"未验证"）。"""
    if name not in keycodes:
        raise KeyError(f"键码表里没有 {name}（探测没通过，不许猜数字）")
    return [f"{keycodes[name]}:{1 if press else 0}"]


def plan(backend: str, probe: Probe, text: str = "") -> list[Step]:
    """后端 → 命令序列。**探测没过就返回空序列**，调用方看到空序列必须报"未验证"。

    纯函数：不跑任何东西，所以单测能把三种后端的序列逐字钉死。

    这里只管**中段**（write → paste）。前后的两段各有自己的出口，不在这里捏在一起：
    快照要先真读到东西才谈得上还原（`snapshot_clipboard`），还原要等粘贴落地并由主循环
    计时（`restore_steps` + `voice_worker.arm_clipboard_restore`）。四段的**顺序**由
    `Step.stage` 记进账，判据见 `tests/test_voice_clipboard.py::TestSequenceOrdering`。
    """
    if backend not in BACKENDS:
        raise ValueError(f"未知上屏后端 {backend!r}，只认 {' / '.join(BACKENDS)}")
    payload = text.encode("utf-8")
    if backend == "manual":
        return []
    if not probe.verified:
        return []
    if backend == "clipboard":
        return [
            Step([probe.wl_copy], stdin=payload, label="写剪贴板", stage=STAGE_WRITE),
            Step([probe.ydotool, "key",
                  *key_seq("KEY_LEFTCTRL", True, probe.keycodes),
                  *key_seq("KEY_V", True, probe.keycodes),
                  *key_seq("KEY_V", False, probe.keycodes),
                  *key_seq("KEY_LEFTCTRL", False, probe.keycodes)],
                 label="Ctrl+V", stage=STAGE_PASTE),
        ]
    return [                                    # primary：主选区 + 中键点一下
        Step([probe.wl_copy, "--primary"], stdin=payload, label="写主选区", stage=STAGE_WRITE),
        Step([probe.ydotool, "click", CLICK_MIDDLE_DOWN, CLICK_MIDDLE_UP],
             label="中键", stage=STAGE_PASTE),
    ]


# ── 剪贴板：备份 → （中段注入）→ 条件还原 ──────────────────────────────────────
# 这一节的**全部**剪贴板内容都只活在内存里：`Snapshot.data` / `RestoreJob` 不外传、不落盘、
# 不进日志、不进事件帧；对外只有"类型 + 字节数"。改这一节之前先看
# `tests/test_voice_clipboard.py::TestSnapshotNeverHitsDisk`，那条判据就是钉这个的。
def restore_from_env() -> bool:
    """`CAPSPLUS_VOICE_RESTORE_CLIPBOARD=0` 关掉还原；其它值（包括写错）都按默认**开**。"""
    return (os.environ.get(RESTORE_ENV) or "").strip() != "0"


def copy_argv(wl_copy: str, primary: bool = False, mime: str | None = None,
              clear: bool = False) -> list[str]:
    """`wl-copy` 的 argv 只有一个拼法：主选区一律带 `--primary`，要指定类型才带 `--type`。"""
    argv = [wl_copy]
    if primary:
        argv.append("--primary")
    if clear:
        argv.append("--clear")
    if mime:
        argv += ["--type", mime]
    return argv


def paste_argv(wl_paste: str, primary: bool = False, *flags: str) -> list[str]:
    """`wl-paste` 的 argv。读内容时必须 `--no-newline`，否则回读到的是**加过一个换行的**内容，
    比对和还原都会被这个换行带偏。"""
    argv = [wl_paste]
    if primary:
        argv.append("--primary")
    return argv + list(flags)


def choose_mime(types, priority=SNAPSHOT_MIME_PRIORITY) -> str | None:
    """从 `wl-paste --list-types` 的清单里挑**一种**要备份的类型（纯函数，规则钉在这）。

    规则三条，都有判据：
      1. 按 `priority` 从上到下取**精确命中**的那个；
      2. 精确没命中时按"主型"（去掉 `;charset=…` 那段、小写）再比一次 —— MIME 参数各家写法不一；
      3. 都不在优先级表里 ⇒ 返回 None，意思是**有东西但备份不了**，调用方必须如实说"会覆盖且不还原"，
         不许挑一个"差不多"的凑（挑错了还原回来的是错的东西，比不还原更糟）。
    空清单返回 None 且 `empty` 由调用方判（那是"原本剪贴板是空的"，和"读不了"是两回事）。
    """
    seen = [t.strip() for t in (types or ()) if t and t.strip()]
    if not seen:
        return None
    exact = set(seen)
    for want in priority:
        if want in exact:
            return want
    for want in priority:
        base = want.split(";")[0].strip().lower()
        for got in seen:
            if got.split(";")[0].strip().lower() == base:
                return got
    return None


def mime_is_plain_text(mime: str) -> bool:
    """`text/plain`（带不带 charset 参数都算）。还原它的时候**不加 `--type`**，见 `restore_steps`。"""
    return mime.split(";")[0].strip().lower() == "text/plain"


@dataclass
class Snapshot:
    """一份剪贴板的内存快照。`data` 是用户原来的东西 —— 不许写盘、不许进日志、不许进事件帧。"""
    types: tuple[str, ...] = ()
    mime: str | None = None
    data: bytes | None = None
    empty: bool = False
    primary: bool = False
    problem: str = ""

    @property
    def backed_up(self) -> bool:
        """真拿到了可还原的东西（**或**确认原本就是空的）。"""
        return not self.problem and (self.empty or self.data is not None)

    def describe(self) -> str:
        """给人看的一句话：**只有类型和字节数**。"""
        if self.problem:
            return self.problem
        if self.empty:
            return "原本为空"
        if self.data is None:
            return "没读回内容"
        return f"{self.mime} {len(self.data)} 字节"


@dataclass
class RestoreJob:
    """"过一会儿条件还原"这件事的全部现场。生命周期只在 worker 内存里，随还原完成/进程退出消失。"""
    snapshot: Snapshot
    ours: list[bytes] = field(default_factory=list)   # 我们写进去过的文本（可能累积多条）
    backend: str = "clipboard"
    primary: bool = False
    wl_copy: str = ""
    wl_paste: str = ""
    delay_ms: int = CLIPBOARD_SETTLE_MS

    def describe(self) -> str:
        return f"待还原 {self.snapshot.describe()}"


def not_restorable_note(snapshot: Snapshot, probe: Probe) -> str:
    """「这次覆盖掉的东西取不回来」要说清楚是**为什么**，并且给一条避开它的路。

    三种成因分开说，别糊成一句"备份失败"：
      * 类型在但不在可备份清单 ⇒ 点名那个类型（判据 5 要求用户看得见是哪个类型）；
      * 原本为空但这台机不认 `--clear` ⇒ 还原成"空"这一步做不到；
      * 其余（读不回、超上限、没装 wl-paste）⇒ 把 `problem` 原样带出来。
    """
    manual = "不想被覆盖就设 CAPSPLUS_VOICE_BACKEND=manual（文本只留在弹窗里）"
    if snapshot.types and not snapshot.mime:
        rest = tuple(t for t in snapshot.types if t not in SNAPSHOT_MIME_PRIORITY) or snapshot.types
        return (f"剪贴板里有 {'/'.join(rest)} 无法备份，本次会覆盖且不还原；{manual}")
    if snapshot.empty and not probe.wl_copy_clear:
        return ("原剪贴板是空的，但这个 wl-copy 的 --help 里没有 --clear ⇒ 还原成空这一步做不到，"
                "本次会覆盖且不还原（写一个空串不算清空：别的应用会读到一条空文本）。"
                f"{manual}")
    return (f"剪贴板备份没成功（{snapshot.problem or '没读回内容'}），本次会覆盖且不还原；{manual}")


def snapshot_clipboard(probe: Probe, run, primary: bool = False,
                       max_bytes: int = SNAPSHOT_MAX_BYTES) -> Snapshot:
    """把剪贴板当前内容读进内存（两步：先问类型，再按选中的那种读字节）。

    任何一步不成都抛 —— 回来一个带 `problem` 的 Snapshot，调用方据此决定**不还原**并如实说明。
    `run` 是注入点：单测必须给假的（真跑就是往明江的剪贴板上伸手）。
    """
    if not probe.clipboard_backup:
        # 走 Probe.clipboard_backup 而不是裸摸 wl_paste：那句"故意不进 verified"的口径要有人用着
        return Snapshot(problem="没探到 wl-paste（wl-clipboard），备份不了")
    argv = paste_argv(probe.wl_paste, primary, "--list-types")
    try:
        proc = run(argv, capture_output=True, text=False, timeout=STEP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return Snapshot(problem=f"问类型没跑起来 {type(exc).__name__}")
    # stderr 一律不进摘要：wl-clipboard 的报错里可能带上它经手的内容片段，隐私优先。
    if getattr(proc, "returncode", 0) != 0:
        return Snapshot(problem=f"问类型退出码 {getattr(proc, 'returncode', '?')}")
    raw = getattr(proc, "stdout", b"") or b""
    # 类型清单必须**解码成 str** 再往下走：`runner` 统一按 `text=False` 收字节，早期版本把 bytes
    # 直接喂给 `choose_mime`，于是"类型永远不在优先级表里" ⇒ 每一次都走"备份不了"那条降级。
    # 判据：`tests/test_voice_clipboard.py::TestSnapshotReading`（假剪贴板给的就是 bytes）。
    types = tuple(dict.fromkeys(line.strip().decode("utf-8", "replace")
                                for line in raw.split(b"\n") if line.strip()))
    if not types:
        # 清单是空的 = 原本**没东西**。这算"备份成功"（备份的结果就是"空"），还原动作是 `--clear`；
        # 这台机认不认 --clear 由 `inject` 那一层判（不认就走"不还原 + 说明"，见 not_restorable_note）。
        return Snapshot(empty=True, primary=primary)
    mime = choose_mime(types)
    if not mime:
        return Snapshot(types=types, primary=primary, problem="类型不在可备份清单里")
    got = paste_argv(probe.wl_paste, primary, "--no-newline", "--type", mime)
    try:
        proc = run(got, capture_output=True, text=False, timeout=STEP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return Snapshot(types=types, mime=mime, primary=primary,
                        problem=f"读 {mime} 没跑起来 {type(exc).__name__}")
    if getattr(proc, "returncode", 0) != 0:
        return Snapshot(types=types, mime=mime, primary=primary,
                        problem=f"读 {mime} 退出码 {getattr(proc, 'returncode', '?')}")
    data = getattr(proc, "stdout", b"") or b""
    if len(data) > max_bytes:
        # 直接丢掉这段字节：宁可这次不还原，也不在常驻 worker 里囤一块离谱大的东西。
        return Snapshot(types=types, mime=mime, primary=primary,
                        problem=f"{mime} 有 {len(data)} 字节，超过上限 {max_bytes}，不备份")
    return Snapshot(types=types, mime=mime, data=data, primary=primary)


def restore_steps(job: RestoreJob) -> list[Step]:
    """还原那一步的命令（纯函数）。**空剪贴板 ⇒ 清空，不是写一个空串**（判据 4）。"""
    snap = job.snapshot
    if snap.empty:
        return [Step(copy_argv(job.wl_copy, job.primary, clear=True),
                     stage=STAGE_RESTORE, label="清空剪贴板")]
    mime = snap.mime or ""
    # text/plain 故意**不加 --type**：wl-copy 对纯文本会一次注册齐 text/plain;charset=utf-8、
    # TEXT、UTF8_STRING 几种目标，加了 --type 只剩一种，别的应用挑目标时会粘不到。
    # 非文本（image/png、text/uri-list、text/html）必须加：不加 wl-copy 会按二进制猜成
    # application/octet-stream，还原回来的是一张"不是图"的字节流。
    return [Step(copy_argv(job.wl_copy, job.primary, None if mime_is_plain_text(mime) else mime),
                 stdin=snap.data, stage=STAGE_RESTORE, label="还原剪贴板")]


def check_still_ours(job: RestoreJob, run) -> bool | None:
    """到点了：剪贴板里**还是不是我们写的那段**？True / False / None（读不了，按"改过了"处理）。

    为什么这条判断不能省：还原 = 往剪贴板上盖写。用户在这 CLIPBOARD_SETTLE_MS 里自己复制了
    别的东西（密码、图片、一段长文），我们一还原就把**他的新东西**冲掉了 —— 那比不还原糟得多。
    判据 ①"还是我们的"⇒ 还原发生；②"已被别人改"⇒ 不许还原且状态里说明。两个方向各一条。
    """
    payload = job.ours or []
    for mime in TEXT_MIME_CANDIDATES:
        argv = paste_argv(job.wl_paste, job.primary, "--no-newline", "--type", mime)
        try:
            proc = run(argv, capture_output=True, text=False, timeout=STEP_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        rc = getattr(proc, "returncode", 0)
        if rc != 0:
            continue           # 这种类型这台机不给读，换下一种候选
        return (getattr(proc, "stdout", b"") or b"") in payload
    # 两种文本目标都读不出来 ⇒ 现在里面没有文本（多半是图 / 文件列表）⇒ 别人改过了
    return False


@dataclass
class RestoreResult:
    did: bool = False
    code: str = ""
    message: str = ""
    commands: list[str] = field(default_factory=list)
    stage: str = STAGE_RESTORE

    def describe(self) -> str:
        """一行摘要，进 `capsplus-voice status`：**只有动作与类型/字节数**。"""
        return (f"已还原（{self.message}）" if self.did
                else f"未还原 — {self.code}: {self.message}")


def perform_restore(job: RestoreJob, run, verify: bool = True) -> RestoreResult:
    """条件还原的全部动作：查"还是我们的吗" → 还原 → 回读核对。任何一步不成都抛。"""
    snap = job.snapshot
    if not snap.backed_up:
        # 备份本来就没成 —— 这里**不许**假装能还原（判据 5），也不许顺手写点什么上去。
        return RestoreResult(code="no_backup",
                             message=f"没备份成（{snap.problem or '没读回内容'}），未还原")
    still = check_still_ours(job, run)
    if still is not True:
        if still is False:
            return RestoreResult(code="not_ours", message="剪贴板已被别处更新，未还原")
        return RestoreResult(code="check_failed",
                             message="读不回剪贴板现值，保守起见未还原")
    ran: list[str] = []
    for step in restore_steps(job):
        ran.append(" ".join(step.argv))
        try:
            proc = run(step.argv, input=step.stdin, capture_output=True, text=False,
                       timeout=STEP_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return RestoreResult(code="step_failed",
                                 message=f"还原那步没跑起来 {type(exc).__name__}", commands=ran)
        rc = getattr(proc, "returncode", 0)
        if rc != 0:
            # 同 snapshot_clipboard：不拼 stderr，摘要里绝可能出现用户内容片段。
            return RestoreResult(code="step_failed", message=f"还原命令退出码 {rc}", commands=ran)
    if snap.empty:
        return RestoreResult(did=True, code="cleared",
                             message="原剪贴板为空 ⇒ 已按 --clear 清空", commands=ran)
    if verify and len(snap.data or b"") <= VERIFY_MAX_BYTES:
        back = None
        for mime in dict.fromkeys([snap.mime] + list(TEXT_MIME_CANDIDATES)):
            if not mime:
                continue
            argv = paste_argv(job.wl_paste, job.primary, "--no-newline", "--type", mime)
            try:
                proc = run(argv, capture_output=True, text=False, timeout=STEP_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                break
            if getattr(proc, "returncode", 0) == 0:
                back = getattr(proc, "stdout", b"") or b""
                break
        if back != snap.data:
            return RestoreResult(
                code="verify_mismatch", commands=ran,
                message="还原命令发了但回读不一致（Wayland 要求发数据源的客户端有键盘焦点，"
                        "多半是 worker 没拿到）")
        return RestoreResult(did=True, code="restored", commands=ran,
                             message=f"{snap.mime} {len(snap.data or b'')} 字节（已回读核对）")
    return RestoreResult(did=True, code="restored_unverified", commands=ran,
                         message=f"{snap.mime} {len(snap.data or b'')} 字节（太大，未回读核对）")


def unverified_reason(probe: Probe) -> str:
    """一句"为什么还不能注入"+下一步（没验证就如实说没验证，不许假装能成功）。"""
    why = "; ".join(probe.problems) or "探测没过"
    return (f"{why}\n        能立刻用的两条路：① `CAPSPLUS_VOICE_BACKEND=manual`"
            "（文本留在弹窗里，自己 Ctrl+V）；② root 起 daemon：`sudo ydotoold`"
            "（/dev/uinput 需要写权限），再 `capsplus-voice status` 复检")


def degraded_clipboard_write(text: str, backend: str, probe: Probe, run) -> InjectResult | None:
    """按键注入用不了时的**半条路**：只把文本写进剪贴板，然后如实报"请自己粘贴"。

    为什么值得做（2026-10-17）：`ydotoold` 不在跑是常态，但剪贴板那一步本来就可用 ——
    写进去用户按一次 Ctrl+V 就完事，比什么也没发生强。
    **注意 `ok=False`**：这不是"上屏成功"，我们没按下任何键，不许把它当成功报。
    """
    if not probe.wl_copy or backend not in ("clipboard", "primary"):
        return None
    argv = [probe.wl_copy]                     # 降级一律写剪贴板：配的提示就是 Ctrl+V
    try:
        proc = run(argv, input=text.encode("utf-8"), capture_output=True,
                   text=False, timeout=STEP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if getattr(proc, "returncode", 0) != 0:
        return None
    return InjectResult(ok=False, backend=backend, code="needs_manual_paste",
                        message="按键注入用不了，文本已写进剪贴板",
                        hint="把光标放到要输入的位置，按 Ctrl+V",
                        commands=[" ".join(argv)], steps=1, keep_window=True,
                        stages=[STAGE_WRITE],
                        clipboard_note="降级：文本留在剪贴板里等你自己粘贴，**不做还原**"
                                       "（还原等于把你还没粘出去的东西抽走）")


def inject(text: str, backend: str | None = None, probe: Probe | None = None,
           runner=None, sleep=None, degrade: bool = True,
           restore: bool | None = None) -> InjectResult:
    """执行上屏。任何一步失败都归成结构化结果，**不抛异常**。

    `runner` 注入的是 `subprocess.run` 等价物 —— 单测必须注入假的，
    否则就会往**明江正在用的屏幕**上打键（红线），也就会真去读他的剪贴板。

    `restore=None` 时按 `CAPSPLUS_VOICE_RESTORE_CLIPBOARD` 定（默认开）。快照在这里**只读进内存**；
    **还原不在这里做** —— `CLIPBOARD_SETTLE_MS` 那段等待由主循环计时，到点才在后台线程跑
    `perform_restore`。这里交出去的是那份现场（`result.restore_job`）。
    """
    name = backend or backend_from_env()
    if name not in BACKENDS:
        return InjectResult(backend=name, code="bad_backend",
                            message=f"未知后端 {name!r}",
                            hint=f"改成 {' / '.join(BACKENDS)} 之一（CAPSPLUS_VOICE_BACKEND）")
    probe = probe or run_probe()
    if name == "manual":
        return InjectResult(ok=True, backend="manual", code="manual",
                            message="文本留在弹窗里，由你自己粘贴",
                            hint="弹窗里的字可直接 Ctrl+A / Ctrl+C 复制",
                            steps=0, keep_window=True, stages=[],
                            clipboard_note="manual 后端：剪贴板一个字节都不动")
    run = runner or subprocess.run
    if not probe.verified:
        if degrade:
            half = degraded_clipboard_write(text, name, probe, run)
            if half is not None:
                return half
        return InjectResult(backend=name, code="unverified",
                            message=unverified_reason(probe),
                            hint="`capsplus-voice status` 会打印完整探测结论")
    steps = plan(name, probe, text)
    if not steps:
        return InjectResult(backend=name, code="unverified", message=unverified_reason(probe),
                            hint="")

    # ── 第 1 段：快照（必须在写剪贴板**之前**，这是时序判据盯的第一件事）
    want_restore = restore_from_env() if restore is None else bool(restore)
    primary = name == "primary"
    stages: list[str] = []
    job: RestoreJob | None = None
    note: str
    if not want_restore:
        note = f"还原开关关着（{RESTORE_ENV}=0），本次会覆盖且不还原"
    else:
        snapshot = snapshot_clipboard(probe, run, primary=primary)
        stages.append(STAGE_SNAPSHOT)
        if snapshot.backed_up and (not snapshot.empty or probe.wl_copy_clear):
            job = RestoreJob(snapshot=snapshot, ours=[text.encode("utf-8")], backend=name,
                             primary=primary, wl_copy=probe.wl_copy or "",
                             wl_paste=probe.wl_paste or "", delay_ms=CLIPBOARD_SETTLE_MS)
            note = f"已备份 {snapshot.describe()}，{CLIPBOARD_SETTLE_MS}ms 后条件还原"
        else:
            note = not_restorable_note(snapshot, probe)

    def aborted() -> str:
        """注入半途失败时剪贴板归谁：留着**我们的文本**让用户自己粘，此时**不许**还原。"""
        if job is None:
            return note
        return ("注入没走完：你要的文本还留在剪贴板里，这次**不还原**"
                f"（备份的 {job.snapshot.describe()} 已丢弃）")

    # ── 第 2、3 段：写剪贴板 → 按键粘贴
    ran: list[str] = []
    for i, step in enumerate(steps, 1):
        ran.append(" ".join(step.argv))
        stages.append(step.stage or STAGE_WRITE)
        try:
            proc = run(step.argv, input=step.stdin, capture_output=True,
                       text=False, timeout=STEP_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return InjectResult(backend=name, code="step_failed",
                                message=f"第 {i}/{len(steps)} 步「{step.label}」没跑起来："
                                        f"{type(exc).__name__}: {exc}",
                                hint=f"单独试一次：{step.describe()}",
                                commands=ran, steps=i - 1, stages=stages, clipboard_note=aborted())
        rc = getattr(proc, "returncode", 0)
        if rc != 0:
            err = (getattr(proc, "stderr", b"") or b"").decode("utf-8", "replace").strip()
            return InjectResult(backend=name, code="step_failed",
                                message=f"第 {i}/{len(steps)} 步「{step.label}」退出码 {rc}："
                                        f"{err[:160]}",
                                hint=_backend_hint(name, i),
                                commands=ran, steps=i - 1, stages=stages, clipboard_note=aborted())
        if sleep is not None and i < len(steps):
            sleep(0.06)         # 剪贴板服务与按键之间留一口气（daemon 侧 fork 是异步的）
    # ── 第 4 段：还原不在这里发生（等 CLIPBOARD_SETTLE_MS，由调用方的主循环计时）
    return InjectResult(ok=True, backend=name, steps=len(steps), commands=ran,
                        stages=stages, clipboard_note=note, restore_job=job)


def _backend_hint(name: str, step_no: int) -> str:
    if step_no == 1:
        return "第一步是写剪贴板：`echo 测试 | wl-copy` 单独跑一下看报什么"
    if name == "clipboard":
        return "粘贴那步是 ydotool：确认 `ydotoold` 在跑、目标窗口有键盘焦点"
    return "中键那步是 ydotool click：光标得停在输入区里，且目标控件接受主选区粘贴"
