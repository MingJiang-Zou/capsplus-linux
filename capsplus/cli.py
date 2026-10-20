# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""capsplus 总入口：装 / 起 / 停 / 查 / 修 / 开，全部收在这**一条命令**里。

为什么要有它（2026-10-20，明江原话：「不要这里开一个东西那边开一个东西，还没有统一的管理与启动器」）：
项目原先的入口是散的 —— `install.sh` 装 bin 与桌面入口、`tools/install-extension.sh` 装 GNOME 扩展、
`capsplus apply` 写 keyd 配置、托盘走 autostart、两个 systemd unit 靠人手 sed 渲染再 enable。
每一样单独看都对，合起来就是"半残状态随处可见，出问题不知道去哪看"。

所以本模块提供**一个真相源**（`capsplus status` / `capsplus doctor`）与**一组幂等动作**：
    capsplus                  # = status
    capsplus status           # 一页看全：keyd / 扩展 / 语音 / 截图 / 托盘 / 装机件，每条带下一步
    capsplus doctor [--fix]   # 体检；--fix 只修"能安全自动修"的那几条
    capsplus install          # 一次装全（幂等；支持 --dry-run / --no-keyd / --no-extension / --no-units）
    capsplus uninstall        # 卸用户侧；默认不动 /etc/keyd 与用户数据
    capsplus up|down|restart  # 用户侧栈的总开关（systemd --user 的 capsplus.target）
    capsplus render|apply     # keyd 配置（沿用原实现，见 capsplus/apply.py）
    capsplus ext sync|verify|bench|status
    capsplus voice|shot|qbar|settings|tray   # 转发到对应入口
    capsplus logs [--lines N] [--follow]
    capsplus test             # 跑仓内全量单测（三种口径见 README「开发」）
    capsplus version

三条设计口径（改之前先读）：
  ① **不 import gi**：status 必须能在没有 PyGObject 的机器上跑（CI 与 SSH 里就是这样）。
  ② 每条状态都要带"下一步动作"，不许只报红 —— 报红不给动作等于把问题丢回给用户。
  ③ 起停只认 systemd --user 的 target；单元没装时**退回直接起进程**并说清楚，绝不假装成功。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

from . import __version__
from .env import KEYD_BIN_NAMES, bindir, find_keyd_bin, keyd_bin_origin
from .extcheck import EXT_UUID, extension_status, qbar_observed

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = Path.home() / ".local" / "state" / "capsplus"
VOICE_SOCK = STATE_DIR / "voice.sock"
ASR_HEALTH = "http://127.0.0.1:18090/health"
# 扩展部署目录（与 extcheck 同一口径；这里单独算一份是为了报"仓内 vs 机器上"的漂移）
DEPLOY_DIR = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / EXT_UUID
EXT_SRC = REPO / "extensions" / EXT_UUID
EXT_FILES = ("extension.js", "metadata.json", "stylesheet.css")

USAGE = """\
用法: capsplus <子命令> [参数]

  status                    一页看全（默认；= capsplus）
  doctor [--fix]            体检；--fix 只修能安全自动修的装机件
  install [--dry-run] [--no-keyd] [--no-extension] [--no-units]
                            一次装全：bin 软链 / 桌面入口 / 扩展副本 / keyd 配置 / systemd 单元组
  uninstall [--dry-run] [--purge-extension] [--purge-keyd]
                            卸用户侧（默认不动 /etc/keyd 与用户数据）
  up | down | restart       用户侧栈的总开关（systemd --user 的 capsplus.target）
  render                    打印渲染结果（不落盘）
  apply [--dry-run]         写 /etc/keyd 并 reload（有备份、出错回滚）
  ext sync|verify|status|bench
                            扩展：同步副本 / 只校验 / 进状态页 / 隔离会话自检（不用重登）
  voice <toggle|start|stop|status|meter>
  shot  <--region|--capture|--annotate|--pin ...>
  qbar | settings | tray    打开对应组件
  logs [-n N] [-f]          用户单元日志（没有用户管理器时退回两个运行日志）
  test [gui]                仓内全量单测（gui = 在 Xvfb 里真跑界面用例）
  version | help
"""


# ---------------------------------------------------------------------------
# 小工具（都可注入，便于判据）
# ---------------------------------------------------------------------------
def _run(argv: list[str], timeout: float = 6.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}: {exc}")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _systemctl_user(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args], timeout=20)


def _has_user_manager() -> bool:
    proc = _systemctl_user("is-system-running")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _unit_installed(name: str) -> bool:
    return (Path.home() / ".config" / "systemd" / "user" / name).exists()


def unit_enabled(name: str) -> bool:
    proc = _systemctl_user("is-enabled", name)
    return proc.returncode == 0 and proc.stdout.strip() in ("enabled", "static", "generated")


def unit_active(name: str) -> bool:
    proc = _systemctl_user("is-active", name)
    return proc.returncode == 0 and proc.stdout.strip() == "active"


def voice_worker_alive() -> bool:
    """socket 在 = 有个 worker 守着它（worker 是 Qbar 式单实例，socket 随进程生灭）。"""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(str(VOICE_SOCK))
        return True
    except OSError:
        return False


def asr_healthy() -> tuple[bool | None, str]:
    """本机识别服务在不在。返回 (True/False/None, 一句话)。None = 没测（缺 curl）。"""
    if _which("curl") is None:
        return None, "没装 curl，跳过识别服务探测"
    proc = _run(["curl", "-s", "-m", "1.5", ASR_HEALTH], timeout=3)
    if proc.returncode != 0 or not proc.stdout.strip():
        return False, "127.0.0.1:18090 没应答（识别服务没起？`systemctl --user status voice-svc`）"
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return False, "识别服务回了非 JSON（版本不对？）"
    if data.get("ok") and data.get("model_loaded"):
        return True, f"就绪（已跑 {int(data.get('uptime_s', 0)) // 60} 分钟）"
    return False, f"活着但没就绪：{data}"


def _tray_pids() -> list[int]:
    """找出**真的在跑托盘**的进程号。

    为什么不用 `pgrep -f capsplus-tray`（2026-10-20 踩过，代价是一条命令把自己杀了）：
    `-f` 匹配的是整条命令行，于是任何"命令行里恰好提到这个字符串"的 shell / 编辑器 / 我这条
    `capsplus up` 的调用链都会被算成托盘；`pkill` 拿同一份名单去动手时就会**连调用者一起杀**。
    所以改成读 `/proc/<pid>/cmdline` 按**参数位**认：`python3 …/capsplus-tray` 或直接
    `…/capsplus-tray`。宁可漏（报"没在跑"）也不能误杀。
    """
    me = os.getpid()
    pids = []
    proc_root = Path("/proc")
    try:
        entries = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return pids
    for entry in entries:
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if pid == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv:
            continue
        # 直接执行本仓/软链的入口，或 `python3 <入口>`；只认最后一段文件名，避免把
        # "命令行里提到这个路径的 grep / vim / 我自己的调用链"当成托盘
        cand = [argv[0]] + (argv[1:2] if argv[0].endswith(("python3", "python")) else [])
        if any(os.path.basename(c) == "capsplus-tray" for c in cand):
            pids.append(pid)
    return pids


def tray_alive() -> bool:
    return bool(_tray_pids())


def extension_drift() -> tuple[bool, str]:
    """仓内源码 vs 机器上部署副本。True = 一致。

    这一条是**重登需求的唯一客观判据**：不一致 = 机器上跑的是旧代码，
    而 GNOME 不会就地重新 import（见 docs/KNOWN-LIMITS.md），只能重登。
    """
    if not DEPLOY_DIR.exists():
        return False, "机器上没有部署副本（跑 `capsplus install` 或 `capsplus ext sync`）"
    diff = []
    for name in EXT_FILES:
        src = EXT_SRC / name
        dst = DEPLOY_DIR / name
        if not src.exists():
            continue
        if not dst.exists():
            diff.append(f"{name} 缺")
            continue
        if src.read_bytes() != dst.read_bytes():
            diff.append(name)
    if diff:
        return False, "部署副本落后于仓内：" + "、".join(diff)
    return True, "与仓内一致"


def bin_links_ok() -> tuple[int, int]:
    """(已就位, 应有) —— bin 软链有多少条指向本仓。"""
    names = ("capsplus", "capsplus-qbar", "capsplus-settings", "capsplus-shot",
             "capsplus-tray", "capsplus-voice", "keyd-qbar-cmd")
    ok = 0
    for name in names:
        path = bindir() / name
        try:
            if path.is_symlink() and Path(os.readlink(path)) == REPO / "bin" / name:
                ok += 1
        except OSError:
            pass
    return ok, len(names)


# ---------------------------------------------------------------------------
# 状态：一个真相源
# ---------------------------------------------------------------------------
OK, WARN, BAD, INFO = "✓", "!", "✗", "·"

# 重登这件事在两组里都会出现（扩展本身 ERROR / 截图通道断了）—— 用**同一个字符串**，
# 好让 render_groups 的动作去重生效，也不会出现"有红项却没有已知动作"的假缺口。
NEED_RELOGIN = ("注销重登一次（GNOME 不会就地重新 import 扩展；这是平台硬边界）。"
                "重登之前先跑 `capsplus ext bench`：它绿了说明只差这一下，红了才是代码有问题")


def collect_status() -> list[dict]:
    """返回分组状态。每组：{title, rows: [(记号, 文本)], next: [动作]}。

    判据与 `render_status` 共用；GUI（设置窗的状态页）也吃这份结构化数据，
    免得"命令行看到的"和"窗口里看到的"变成两份真相。
    """
    groups: list[dict] = []

    # ── keyd（键位的根） ────────────────────────────────────────────────────
    from .apply import KEYD_CONF, config_summary, service_state

    rows = []
    nxt = []
    state = service_state()
    rows.append((OK if state == "active" else BAD, f"keyd 服务：{state}"))
    if state != "active":
        nxt.append(f"起 keyd：`sudo systemctl enable --now keyd`（本机命令名 {keyd_bin_origin()}）")
    if find_keyd_bin() is None:
        rows.append((BAD, "keyd 可执行文件：没找到（键位映射整块用不了）"))
        nxt.append("装 keyd：Debian/Ubuntu `sudo apt install %s`；其它发行版看 README「依赖」"
                   % KEYD_BIN_NAMES[0])
    counts, conf, same = config_summary()
    if not conf.exists():
        rows.append((BAD, f"配置文件：{conf} 不存在"))
        nxt.append("装配置：`capsplus apply`（会先备份、出错自动回滚）")
    else:
        rows.append((OK if same else WARN,
                     f"配置文件：{conf}（{'与渲染一致' if same else '与渲染不一致——手改过，或没 apply'}）"))
        if not same:
            nxt.append("重新渲染并应用：`capsplus apply`")
        rows.append((INFO, "段/条数：" + ("、".join(f"{s}={n}" for s, n in counts) or "（空）")))
    groups.append({"title": "键位（keyd）", "rows": rows, "next": nxt})

    # ── GNOME 扩展（截图与"原窗口开标签页"的通道） ─────────────────────────
    rows, nxt = [], []
    ext_state, hint = extension_status()
    drift_ok, drift_text = extension_drift()
    rows.append(((OK if ext_state == "ACTIVE" else BAD), f"扩展状态：{ext_state} —— {hint}"))
    rows.append((OK if drift_ok else WARN, f"部署副本：{drift_text}"))
    if ext_state == "ERROR":
        nxt.append(NEED_RELOGIN)
    elif ext_state in ("missing", "disabled"):
        nxt.append("装并启用扩展：`capsplus ext sync`")
    elif ext_state == "ACTIVE" and not drift_ok:
        nxt.append("部署副本落后：`capsplus ext sync` 之后仍需重登才生效")
    alive, obs = qbar_observed()
    if alive is not None:
        rows.append((OK if alive else WARN, f"Qbar 实测：{obs}"))
    groups.append({"title": "GNOME 扩展（抓帧 / 原窗口开标签页）", "rows": rows, "next": nxt,
                   "state": ext_state})

    # ── 语音（Caps+M） ─────────────────────────────────────────────────────
    rows, nxt = [], []
    rows.append((OK if voice_worker_alive() else INFO,
                 "worker：" + ("在跑" if voice_worker_alive() else "没起（按一次 Caps+M 会自动拉起）")))
    ok, text = asr_healthy()
    rows.append(({True: OK, False: BAD, None: INFO}[ok], f"识别服务：{text}"))
    if ok is False:
        nxt.append("起识别服务：`systemctl --user status voice-svc`（模型在本机，全程离线）")
    groups.append({"title": "语音输入（Caps+M）", "rows": rows, "next": nxt})

    # ── 截图（Caps+F1） ────────────────────────────────────────────────────
    rows, nxt = [], []
    ext_ok = ext_state == "ACTIVE"
    rows.append((OK if ext_ok else BAD,
                 "抓帧通道：" + ("通（扩展在 shell 里）" if ext_ok else "断（扩展没在跑）")))
    for name, why in (("wl-copy", "剪贴板上屏"), ("wl-paste", "剪贴板还原"), ("ydotool", "注入按键")):
        found = _which(name)
        rows.append((OK if found else WARN, f"{name}：{found or '没装（' + why + ' 用不了）'}"))
    if not ext_ok:
        # 与扩展那组用**同一个字符串**：render_groups 去重后只出现一次，
        # 但这一组也不算"报红不给动作"（groups_without_actions 的兜底不该为它响）。
        nxt.append(NEED_RELOGIN)
    groups.append({"title": "截图（Caps+F1）", "rows": rows, "next": nxt})

    # ── 用户侧栈（托盘 / 自启 / 装机件） ───────────────────────────────────
    rows, nxt = [], []
    if _unit_installed("capsplus.target"):
        rows.append((OK, "单元组：capsplus.target 已安装"
                          f"（{'enabled' if unit_enabled('capsplus.target') else '未 enable'}，"
                          f"{'active' if unit_active('capsplus.target') else '未启动'}）"))
        if not unit_enabled("capsplus.target"):
            nxt.append("开机自启：`capsplus install`（会把 target enable 上）")
    else:
        rows.append((WARN, "单元组：capsplus.target 没装（托盘大概走的是 autostart）"))
        nxt.append("收进统一管理：`capsplus install`")
    rows.append((OK if tray_alive() else WARN,
                 "托盘：" + ("在跑" if tray_alive() else "没在跑（`capsplus up` 或 `capsplus tray`）")))
    good, total = bin_links_ok()
    rows.append((OK if good == total else WARN,
                 f"命令入口：~/.local/bin 里 {good}/{total} 条指向本仓"))
    if good != total:
        nxt.append("补齐入口：`capsplus install`")
    rows.append((INFO, f"日志：{STATE_DIR}（qbar.log / voice.log）与 `capsplus logs`"))
    groups.append({"title": "用户侧栈", "rows": rows, "next": nxt})
    return groups


def groups_without_actions(groups: list[dict]) -> list[str]:
    """有 ✗ 项却一条"下一步"都没有的分组标题（纯函数：判据与兜底共用）。

    为什么要这条：报红不给动作 = 把问题丢回给用户，而本项目 2026-10-16 那次
    "扩展静默不加载"就是这么让人白折腾了一晚上。新加一节状态时忘了写动作，这条会当场说出来。
    """
    bad = []
    for group in groups:
        if any(mark == BAD for mark, _text in group.get("rows", [])) and not group.get("next"):
            bad.append(group.get("title", "?"))
    return bad


def render_groups(groups: list[dict], stream=None) -> list[dict]:
    """纯渲染：给定分组，画成人看的一页（不跑任何探测 —— 判据要能喂假数据进来）。"""
    stream = stream or sys.stdout

    def out(text: str = "") -> None:
        print(text, file=stream)

    out(f"capsplus {__version__} —— 一页看全（记号：✓ 正常 / ! 要注意 / ✗ 坏了 / · 参考）")
    actions: list[str] = []
    for group in groups:
        out()
        out(f"[{group['title']}]")
        for mark, text in group["rows"]:
            out(f"  {mark} {text}")
        for item in group.get("next", []):
            if item not in actions:
                actions.append(item)
    bare = groups_without_actions(groups)
    if bare:
        actions.append("这几组有红项但还没有已知动作：" + "、".join(bare) + " —— 用 `capsplus logs` 排障")
    out()
    if actions:
        out("下一步：")
        for i, item in enumerate(actions, 1):
            out(f"  {i}) {item}")
        out()
        out("一键做能安全自动做的那些：`capsplus doctor --fix`")
    else:
        out("下一步：没有待办。")
    out("排障入口：`capsplus logs`（用户单元 + 两个运行日志）、`capsplus test`（仓内全量单测）")
    return groups


def render_status(stream=None) -> list[dict]:
    """探测 + 渲染。返回结构化数据，GUI（设置窗的状态页）复用同一份。"""
    return render_groups(collect_status(), stream)

    def out(text: str = "") -> None:
        print(text, file=stream)

    out(f"capsplus {__version__} —— 一页看全（每行末尾的记号：✓ 正常 / ! 要注意 / ✗ 坏了 / · 参考）")
    actions: list[str] = []
    for group in groups:
        out()
        out(f"[{group['title']}]")
        for mark, text in group["rows"]:
            out(f"  {mark} {text}")
        for item in group["next"]:
            if item not in actions:
                actions.append(item)
    out()
    if actions:
        out("下一步：")
        for i, item in enumerate(actions, 1):
            out(f"  {i}) {item}")
        out()
        out("一键做能安全自动做的那些：`capsplus doctor --fix`")
    else:
        out("下一步：没有待办。")
    out(f"排障入口：`capsplus logs`（用户单元 + 两个运行日志）、`capsplus test`（仓内全量单测）")
    return groups


# ---------------------------------------------------------------------------
# doctor：只修"能安全自动修"的（不猜、不覆盖用户数据）
# ---------------------------------------------------------------------------
def doctor(fix: bool = False, stream=None) -> int:
    stream = stream or sys.stdout
    groups = render_status(stream)
    if not fix:
        return 0
    from .install import Reporter, install_bins, install_desktop, install_units, sync_extension

    report = Reporter(stream=stream)
    print("-" * 60, file=stream)
    print("doctor --fix：只动装机件（bin 软链 / 桌面入口 / 单元 / 扩展副本），不碰键位与用户数据。",
          file=stream)
    ext = next((g for g in groups if g["title"].startswith("GNOME 扩展")), {})
    if ext.get("rows", [(None, "")])[1][0] == WARN:
        sync_extension(report, sync=True)
    good, total = bin_links_ok()
    if good != total:
        install_bins(report)
    install_desktop(report)
    if not _unit_installed("capsplus.target"):
        install_units(report)
    print("-" * 60, file=stream)
    print("修完再看一眼：`capsplus status`", file=stream)
    return 0


# ---------------------------------------------------------------------------
# up / down / restart
# ---------------------------------------------------------------------------
def up(stream=None) -> int:
    stream = stream or sys.stdout
    if _unit_installed("capsplus.target") and _has_user_manager():
        # 先收掉"不是 systemd 起的"那个托盘（旧 autostart 路径遗留的）。
        # 为什么必须收：托盘靠 tray.lock 抢单实例，抢不到的**退 1**，而单元是 Restart=on-failure
        # ⇒ 不收掉就是每 5 秒重启一次的循环（2026-10-20 从 autostart 迁到单元组时踩到的形状）。
        orphans = _tray_pids()
        if orphans and not unit_active("capsplus-tray.service"):
            for pid in orphans:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            print("· 先停掉 autostart 留下的那个托盘实例（改由单元接管）", file=stream)
        proc = _systemctl_user("start", "capsplus.target")
        if proc.returncode == 0:
            print("✓ capsplus.target 已启动（托盘 + 扩展校验）", file=stream)
            return 0
        print(f"✗ 启动失败：{proc.stderr.strip()}", file=stream)
        return 1
    # 退回直接起进程：单元没装或用户管理器连不上时，别假装成功
    if tray_alive():
        print("· 托盘已经在跑（单元组没装，但进程活着）", file=stream)
        return 0
    tray = REPO / "bin" / "capsplus-tray"
    try:
        subprocess.Popen([str(tray)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        print(f"✗ 起不了托盘：{exc}", file=stream)
        return 1
    print("✓ 已直接拉起托盘（没有单元组，所以它不进 systemd 的管理）", file=stream)
    return 0


def down(stream=None) -> int:
    stream = stream or sys.stdout
    # 先数一遍再停：停完再数必然数不到，回一句"本来就没在跑"会让人以为根本没起来过
    pids = _tray_pids()
    if _unit_installed("capsplus.target") and _has_user_manager():
        _systemctl_user("stop", "capsplus.target")
    # 单元之外的托盘（autostart 时代遗留的、或用户自己手起的）也收掉 —— 否则 stop 之后
    # 图标还在，用户会以为"关不掉"
    for pid in _tray_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    print("✓ 托盘已停" if pids else "· 托盘本来就没在跑", file=stream)
    return 0


def restart(stream=None) -> int:
    down(stream)
    return up(stream)


# ---------------------------------------------------------------------------
# 转发类子命令
# ---------------------------------------------------------------------------
def launch(name: str, argv: list[str] | None = None, wait: bool = False) -> int:
    """跑仓里的另一个入口（bin/<name>）。GUI 类默认不 wait，免得把终端挂住。"""
    path = REPO / "bin" / name
    if not path.exists():
        print(f"✗ 仓里没有 bin/{name}（CAPSPLUS_REPO 指对了吗？）", file=sys.stderr)
        return 1
    try:
        if wait:
            return subprocess.call([str(path), *(argv or [])])
        subprocess.Popen([str(path), *(argv or [])], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return 0
    except OSError as exc:
        print(f"✗ 起不了 {name}：{exc}", file=sys.stderr)
        return 1


def extension_cmd(argv: list[str]) -> int:
    from .install import Reporter, sync_extension

    sub = argv[0] if argv else "status"
    if sub in ("sync", "verify"):
        report = Reporter()
        return 0 if sync_extension(report, sync=(sub == "sync")) else 1
    if sub == "status":
        render_status()
        return 0
    if sub == "bench":
        script = REPO / "tools" / "ext-bench.sh"
        if not script.exists():
            print("✗ 仓里没有 tools/ext-bench.sh（这是「不重登就能自检扩展」的那条路）", file=sys.stderr)
            return 1
        return subprocess.call(["sh", str(script), *argv[1:]])
    print("用法: capsplus ext [sync|verify|status|bench]", file=sys.stderr)
    return 2


def logs_cmd(argv: list[str]) -> int:
    lines = "60"
    follow = False
    i = 0
    while i < len(argv):
        if argv[i] in ("-n", "--lines") and i + 1 < len(argv):
            lines = argv[i + 1]
            i += 2
            continue
        if argv[i] in ("-f", "--follow"):
            follow = True
        i += 1
    if _has_user_manager():
        cmd = ["journalctl", "--user", "-u", "capsplus-tray.service",
               "-u", "capsplus-extension-sync.service", "--no-pager", "-n", lines]
        if follow:
            cmd.append("-f")
        return subprocess.call(cmd)
    # 没有用户管理器：直接给两个运行日志，别让人空手
    for name in ("voice.log", "qbar.log"):
        path = STATE_DIR / name
        if path.exists():
            print(f"--- {path}（最后 {lines} 行）---")
            print("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-int(lines):]))
    return 0


def test_cmd(argv: list[str]) -> int:
    env = dict(os.environ)
    if argv and argv[0] == "gui":
        return subprocess.call(
            ["xvfb-run", "-a", "--server-args=-screen 0 1280x800x24",
             "env", "-u", "WAYLAND_DISPLAY", "GDK_BACKEND=x11", "CAPSPLUS_GUI=1",
             sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=str(REPO), env=env)
    return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           cwd=str(REPO), env=env)


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    rest = argv[1:]

    if cmd in ("status", "st"):
        render_status()
        return 0
    if cmd in ("doctor", "check"):
        return doctor(fix=("--fix" in rest))
    if cmd in ("install", "setup"):
        from .install import install

        return install(dry_run="--dry-run" in rest,
                       keyd="--no-keyd" not in rest,
                       extension="--no-extension" not in rest,
                       units="--no-units" not in rest)
    if cmd in ("uninstall", "remove"):
        from .install import uninstall

        return uninstall(dry_run="--dry-run" in rest,
                         purge_extension="--purge-extension" in rest,
                         purge_keyd="--purge-keyd" in rest)
    if cmd == "up":
        return up()
    if cmd == "down":
        return down()
    if cmd == "restart":
        return restart()
    if cmd in ("render", "apply"):
        from .apply import main as apply_main

        return apply_main(argv)
    if cmd == "ext":
        return extension_cmd(rest)
    if cmd == "voice":
        return launch("capsplus-voice", rest, wait=True)
    if cmd == "shot":
        return launch("capsplus-shot", rest, wait=True)
    if cmd == "qbar":
        return launch("capsplus-qbar")
    if cmd == "settings":
        return launch("capsplus-settings")
    if cmd == "tray":
        return launch("capsplus-tray")
    if cmd == "logs":
        return logs_cmd(rest)
    if cmd == "test":
        return test_cmd(rest)
    if cmd in ("version", "--version", "-V"):
        print(f"capsplus {__version__}")
        return 0
    if cmd in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    print(f"未知子命令: {cmd}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
