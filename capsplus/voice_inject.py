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

    clipboard  wl-copy  →  ydotool Ctrl+V      （默认：所有能粘贴的控件都吃这套）
    primary    wl-copy --primary  →  ydotool 中键（X11 传统的"选中即粘贴"，光标得在输入区）
    manual     什么都不注入，文本留在弹窗里，用户自己复制粘贴（零风险兜底）

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


@dataclass
class Probe:
    """一次探测的全部结论。`verified=False` 时**任何注入都不许跑**。"""
    wl_copy: str | None = None
    wl_copy_primary: bool = False
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

    def report_lines(self) -> list[str]:
        """给人看的探测结果（`capsplus-voice status` 直接用）。"""
        out = [f"wl-copy   : {self.wl_copy or '没装（wl-clipboard）'}"
               + ("" if self.wl_copy_primary else "  ← 不认 --primary"),
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
    """一条命令。`stdin` 走管道 —— 把用户的文本当命令行参数传会撞 ARG_MAX 与转义坑。"""
    argv: list[str]
    stdin: bytes | None = None
    label: str = ""

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
            if not p.wl_copy_primary:
                p.problems.append("这个 wl-copy 不认 --primary")
        except (OSError, subprocess.TimeoutExpired) as exc:
            p.problems.append(f"wl-copy --help 跑不了：{type(exc).__name__}")

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
            Step([probe.wl_copy], stdin=payload, label="写剪贴板"),
            Step([probe.ydotool, "key",
                  *key_seq("KEY_LEFTCTRL", True, probe.keycodes),
                  *key_seq("KEY_V", True, probe.keycodes),
                  *key_seq("KEY_V", False, probe.keycodes),
                  *key_seq("KEY_LEFTCTRL", False, probe.keycodes)], label="Ctrl+V"),
        ]
    return [                                    # primary：主选区 + 中键点一下
        Step([probe.wl_copy, "--primary"], stdin=payload, label="写主选区"),
        Step([probe.ydotool, "click", CLICK_MIDDLE_DOWN, CLICK_MIDDLE_UP], label="中键"),
    ]


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
                        commands=[" ".join(argv)], steps=1, keep_window=True)


def inject(text: str, backend: str | None = None, probe: Probe | None = None,
           runner=None, sleep=None, degrade: bool = True) -> InjectResult:
    """执行上屏。任何一步失败都归成结构化结果，**不抛异常**。

    `runner` 注入的是 `subprocess.run` 等价物 —— 单测必须注入假的，
    否则就会往**明江正在用的屏幕**上打键（红线）。
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
                            steps=0, keep_window=True)
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
    ran: list[str] = []
    for i, step in enumerate(steps, 1):
        ran.append(" ".join(step.argv))
        try:
            proc = run(step.argv, input=step.stdin, capture_output=True,
                       text=False, timeout=STEP_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return InjectResult(backend=name, code="step_failed",
                                message=f"第 {i}/{len(steps)} 步「{step.label}」没跑起来："
                                        f"{type(exc).__name__}: {exc}",
                                hint=f"单独试一次：{step.describe()}",
                                commands=ran, steps=i - 1)
        rc = getattr(proc, "returncode", 0)
        if rc != 0:
            err = (getattr(proc, "stderr", b"") or b"").decode("utf-8", "replace").strip()
            return InjectResult(backend=name, code="step_failed",
                                message=f"第 {i}/{len(steps)} 步「{step.label}」退出码 {rc}："
                                        f"{err[:160]}",
                                hint=_backend_hint(name, i),
                                commands=ran, steps=i - 1)
        if sleep is not None and i < len(steps):
            sleep(0.06)         # 剪贴板服务与按键之间留一口气（daemon 侧 fork 是异步的）
    return InjectResult(ok=True, backend=name, steps=len(steps), commands=ran)


def _backend_hint(name: str, step_no: int) -> str:
    if step_no == 1:
        return "第一步是写剪贴板：`echo 测试 | wl-copy` 单独跑一下看报什么"
    if name == "clipboard":
        return "粘贴那步是 ydotool：确认 `ydotoold` 在跑、目标窗口有键盘焦点"
    return "中键那步是 ydotool click：光标得停在输入区里，且目标控件接受主选区粘贴"
