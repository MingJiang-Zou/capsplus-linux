# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""`capsplus install` / `capsplus uninstall`：把装机动作收成**一处**。

为什么要这个模块（2026-10-20）：本项目原先有五条互不相干的装机/启动路径 ——
  ① `install.sh` 装 bin 软链与桌面入口（不装扩展、不装 unit）
  ② `tools/install-extension.sh` 装 GNOME 扩展（要单独记得跑）
  ③ `capsplus apply` 写 /etc/keyd（要单独记得跑）
  ④ `packaging/capsplus-tray.service` 与 `capsplus-extension-sync.service` 靠人手 `sed` 渲染再 enable
  ⑤ 托盘又走 `~/.config/autostart`，与 ④ 是两条并行的自启路
结果就是用户的实际体验："这里开一个东西那边开一个东西，漏一步就半残，坏了也不在一个地方看见"。
本模块把它们合成一条幂等的 `install`：**跑两遍结果一样、失败不留半截、删东西一律进焚化炉**。

四条设计口径（改之前先读）：
  ① 只装**用户侧**（bin 软链、桌面入口、systemd --user 单元、用户扩展目录）；
     唯一会碰系统的是 /etc/keyd 那一份配置，而它走 `capsplus/apply.py` —— 有备份 + 校验 + 回滚。
  ② `--dry-run` 必须先看得到完整计划再落子；`--no-keyd` / `--no-extension` / `--no-units`
     让每一步都能单独关掉（半装状态也可用 `capsplus doctor` 查出来）。
  ③ 装的顺序有讲究：**先落文件、后动 systemd**（`daemon-reload` 之前写坏一个 unit 会让整个
     用户管理器报语法错）；扩展在同一秒不要被写（那是 2026-10-20 那次
     `malformed UTF-8 character sequence at offset 8190` 的根因，详见 tools/install-extension.sh）。
  ④ 退役的东西（旧的 qbar 桌面入口、旧的 autostart 托盘）一律 `mv` 进 `/tmp/attic-<日期>/`，
     不许 `rm` —— 仓里的规矩。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from .env import KEYD_BIN_NAMES, bindir

REPO = Path(__file__).resolve().parent.parent

# 装进 bin 目录的入口（名字与 bin/ 下文件一一对应；keyd-qbar-cmd 是给 keyd 的包装脚本）
ENTRY_POINTS = (
    "capsplus",
    "capsplus-qbar",
    "capsplus-settings",
    "capsplus-shot",
    "capsplus-tray",
    "capsplus-voice",
    "keyd-qbar-cmd",
)

# 渲染进 ~/.config/systemd/user/ 的单元（源在 packaging/，@BIN_DIR@ / @REPO_DIR@ 现渲染）
USER_UNITS = ("capsplus.target", "capsplus-tray.service", "capsplus-extension-sync.service")

# 桌面入口：**只留一条**。Qbar 由 keyd 的 Caps+Q 唤起、设置窗里也能开，
# 应用菜单里再放一条只会变成"应用里面有两个"（2026-10-15 用户实测反馈）。
DESKTOP_ENTRIES = (("packaging/capsplus-settings.desktop", "capsplus-settings.desktop"),)

# 旧路径 → 退役（装新的成功之后才动它们）
LEGACY = (
    "~/.local/share/applications/capsplus-qbar.desktop",
    "~/.config/autostart/capsplus-tray.desktop",
)

APPLICATIONS_DIR = Path.home() / ".local" / "share" / "applications"
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
AUTOSTART_TRAY = AUTOSTART_DIR / "capsplus-tray.desktop"


# ---------------------------------------------------------------------------
# 输出与工具
# ---------------------------------------------------------------------------
class Reporter:
    """装机的输出：每一步一行，`--dry-run` 时行首换成 `[计划]`，写完再给人看一遍总结。"""

    def __init__(self, dry_run: bool = False, stream=None):
        self.dry_run = dry_run
        self.stream = stream or sys.stdout
        self.actions: list[str] = []
        self.warnings: list[str] = []

    def step(self, text: str) -> None:
        self.actions.append(text)
        print(f"{'[计划] ' if self.dry_run else '  '}{text}", file=self.stream)

    def ok(self, text: str) -> None:
        self.actions.append(text)
        print(f"{'[计划] ' if self.dry_run else '  '}✓ {text}", file=self.stream)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        print(f"  ! {text}", file=self.stream)

    def head(self, text: str) -> None:
        print(f"\n== {text} ==", file=self.stream)


def attic_dir(today: str | None = None) -> Path:
    """焚化炉：小文件进 `/tmp/attic-<日期>/`（本机 /tmp 是 tmpfs，重启自清 = 天然终删）。"""
    return Path("/tmp") / f"attic-{today or date.today().strftime('%Y%m%d')}"


def retire(path: Path, grave: Path | None = None) -> Path | None:
    """把不再需要的东西移进焚化炉（**不是删除**），返回落点；不存在则返回 None。"""
    path = Path(path).expanduser()
    if not path.exists() and not path.is_symlink():
        return None
    grave = grave or attic_dir()
    grave.mkdir(parents=True, exist_ok=True)
    # 同名撞车时加序号，别覆盖上一次退役下来的东西（可能是唯一的那份）
    target = grave / path.name
    n = 1
    while target.exists() or target.is_symlink():
        target = grave / f"{path.name}.{n}"
        n += 1
    shutil.move(str(path), str(target))
    return target


def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """先写同目录临时名再 rename —— 半截文件在这套装机件里已经害过一次（见模块 docstring ③）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def render_template(text: str, bin_dir: Path | None = None, repo: Path | None = None) -> str:
    """把模板里的 `@BIN_DIR@` / `@REPO_DIR@` 换成这台机器的真实路径。

    仓里**不许**出现任何具体用户的绝对路径（`tests/test_portability.py` 钉着这条），
    所以模板与运行路径唯一的接缝就是这两个记号。
    """
    b = str(bin_dir or bindir())
    r = str(repo or REPO)
    return text.replace("@BIN_DIR@", b).replace("@REPO_DIR@", r)


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}: {exc}")


def systemctl_user(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args])


# ---------------------------------------------------------------------------
# 依赖体检（与 install.sh 同口径；缺了只警告，不拦着装 —— SSH / 半残机器上也能装）
# ---------------------------------------------------------------------------
def check_dependencies(which=shutil.which) -> list[tuple[bool, str]]:
    """返回 [(是否齐全, 一句话)]。`which` 可注入，便于判据不依赖本机装了什么。"""
    out: list[tuple[bool, str]] = []
    for name, why in (
        ("python3", "整个工具链的解释器"),
        ("desktop-file-validate", "校验桌面入口（缺了只少一道自检）"),
        ("systemctl", "用户侧单元组（托盘 + 扩展校验）"),
    ):
        found = which(name)
        out.append((bool(found), f"{name} -> {found or '没找到（' + why + '）'}"))
    for name in KEYD_BIN_NAMES:
        found = which(name)
        if found:
            out.append((True, f"keyd -> {found}"))
            break
    else:
        out.append((False, "没找到 keyd 可执行文件（%s）—— 键位映射整块用不了"
                    % " / ".join(KEYD_BIN_NAMES)))
    for name in ("fdfind", "fd"):
        found = which(name)
        if found:
            out.append((True, f"fd -> {found}（Qbar 文件搜索）"))
            break
    else:
        out.append((False, "没找到 fd/fdfind —— 只影响 Qbar 搜文件"))
    return out


def gtk_available() -> tuple[bool, str]:
    """GTK3 + AppIndicator 在不在（不在只影响界面，不影响键位）。"""
    code = (
        "import gi\n"
        "gi.require_version('Gtk', '3.0')\n"
        "gi.require_version('AyatanaAppIndicator3', '0.1')\n"
        "from gi.repository import Gtk, AyatanaAppIndicator3\n"
        "print('GTK', Gtk.get_major_version())\n"
    )
    proc = _run([sys.executable, "-c", code])
    if proc.returncode == 0:
        return True, f"PyGObject {proc.stdout.strip()} + AppIndicator"
    return False, "PyGObject / GTK3 / AppIndicator 不可用（装 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1）"


# ---------------------------------------------------------------------------
# 每一步（都能单独调用：doctor 复用它，判据也复用它）
# ---------------------------------------------------------------------------
def install_bins(report: Reporter, bin_dir: Path | None = None) -> list[str]:
    bin_dir = Path(bin_dir or bindir())
    made = []
    report.head("安装 bin 软链")
    for name in ENTRY_POINTS:
        src = REPO / "bin" / name
        dst = bin_dir / name
        if not src.exists():
            report.warn(f"仓里缺少 bin/{name}，跳过")
            continue
        if report.dry_run:
            report.step(f"{dst} -> {src}")
            made.append(str(dst))
            continue
        bin_dir.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() and Path(os.readlink(dst)) == src:
            report.ok(f"{dst}（已指向本仓，未改动）")
        else:
            if dst.exists() or dst.is_symlink():
                retire(dst)
            dst.symlink_to(src)
            report.ok(f"{dst} -> {src}")
        made.append(str(dst))
    return made


def install_desktop(report: Reporter) -> list[str]:
    report.head("安装应用菜单入口（只留一条）")
    written = []
    for rel, name in DESKTOP_ENTRIES:
        src = REPO / rel
        text = render_template(src.read_text(encoding="utf-8"))
        dst = APPLICATIONS_DIR / name
        if report.dry_run:
            report.step(f"{dst}")
            continue
        written.append(str(dst))
        if dst.exists() and dst.read_text(encoding="utf-8") == text:
            report.ok(f"{dst}（内容没变，未改动）")
        else:
            write_atomic(dst, text)
            report.ok(f"{dst}")
    if report.dry_run:
        return written
    if shutil.which("desktop-file-validate"):
        proc = _run(["desktop-file-validate"] + written)
        if proc.returncode == 0:
            report.ok("desktop-file-validate 通过")
        else:
            report.warn("desktop-file-validate 报错：\n    " + proc.stderr.strip())
    if shutil.which("update-desktop-database"):
        _run(["update-desktop-database", str(APPLICATIONS_DIR)])
    return written


def sync_extension(report: Reporter, sync: bool = True) -> bool:
    """装扩展（原子落盘）。`sync=False` 只校验不写 —— 登录时的 unit 走这一支。"""
    script = REPO / "tools" / "install-extension.sh"
    if not script.exists():
        report.warn("仓里缺少 tools/install-extension.sh，跳过扩展")
        return False
    report.head("同步 GNOME 扩展" + ("（只校验，不写）" if not sync else ""))
    argv = ["sh", str(script), "--verify" if not sync else "--sync"]
    if report.dry_run:
        report.step(" ".join(argv))
        return True
    proc = _run(argv, cwd=REPO)
    text = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        # 脚本成功时最后几行是"下一步怎么自检"的话术（可能是一条 mv 命令），**不能**拿它当结论贴，
        # 那会读成"✓ mv …"这种莫名其妙的成功行（2026-10-20 实测踩到）。
        # 结论行只认以 ✓ 开头的那句；脚本没打 ✓ 时我们自己给一句。
        marks = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("✓")]
        for line in marks:
            print(f"      {line}", file=report.stream)
        report.ok(marks[-1].lstrip("✓ ").strip() if marks
                  else ("扩展已就位（--verify 通过）" if not sync else "扩展已同步"))
        return True
    report.warn("扩展同步失败：\n    " + "\n    ".join(text.splitlines()[-6:]))
    return False


def apply_keyd(report: Reporter) -> bool:
    """写 /etc/keyd 并 reload（有备份、会校验、出错自动回滚 —— 都在 capsplus/apply.py 里）。"""
    from .apply import ApplyError, apply

    report.head("应用 keyd 键位配置")
    if report.dry_run:
        report.step("渲染 + 归一化自检（不写 /etc，不 reload）")
        try:
            apply(dry_run=True)
            report.ok("渲染与自检通过")
            return True
        except Exception as exc:  # noqa: BLE001 —— 自检失败要把原话给用户
            report.warn(f"自检失败：{exc}")
            return False
    try:
        res = apply()
    except ApplyError as exc:
        report.warn(f"apply 失败（已尝试回滚）：{exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        report.warn(f"apply 意外失败：{type(exc).__name__}: {exc}")
        return False
    report.ok(f"已应用到 /etc/keyd 并 reload（备份：{res.backup or '无（原本没有配置）'}）")
    return True


def install_units(report: Reporter) -> bool:
    """渲染并启用用户侧单元组。**先落文件、后 daemon-reload**（写坏一个 unit 会让整组报语法错）。"""
    report.head("安装 systemd 用户单元（一个 target 收全部）")
    if not shutil.which("systemctl"):
        report.warn("没有 systemctl，跳过单元安装（`capsplus up` 会退回直接起进程）")
        return False
    rendered: list[Path] = []
    for name in USER_UNITS:
        src = REPO / "packaging" / name
        if not src.exists():
            report.warn(f"仓里缺少 packaging/{name}，跳过")
            continue
        dst = UNIT_DIR / name
        text = render_template(src.read_text(encoding="utf-8"))
        if report.dry_run:
            report.step(f"{dst}")
            continue
        if dst.exists() and dst.read_text(encoding="utf-8") == text:
            report.ok(f"{dst}（内容没变）")
        else:
            write_atomic(dst, text)
            report.ok(f"{dst}")
        rendered.append(dst)
    if report.dry_run:
        return True
    if not rendered:
        return False
    proc = systemctl_user("daemon-reload")
    if proc.returncode != 0:
        report.warn(f"daemon-reload 失败：{proc.stderr.strip()}")
        return False
    # 让 target 成为唯一的自启开关：先把单独 enable 过的 sync 摘掉，再 enable target
    systemctl_user("disable", "capsplus-extension-sync.service")
    proc = systemctl_user("enable", "capsplus.target")
    if proc.returncode != 0:
        report.warn(f"enable capsplus.target 失败：{proc.stderr.strip()}")
        return False
    report.ok("capsplus.target 已 enable（托盘 + 扩展校验都由它带起来）")
    return True


def retire_legacy(report: Reporter) -> list[str]:
    """退役旧入口。只在**新的一批装成功之后**调用，避免装失败还把能用的删掉。"""
    retired = []
    for spec in LEGACY:
        path = Path(spec).expanduser()
        if report.dry_run:
            if path.exists():
                report.step(f"退役 {path} -> {attic_dir()}/")
            continue
        where = retire(path)
        if where:
            report.ok(f"退役 {path} -> {where}")
            retired.append(str(where))
    return retired


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def install(dry_run: bool = False, keyd: bool = True, extension: bool = True,
            units: bool = True, reporter: Reporter | None = None) -> int:
    """一次装全。返回 0 = 全部成功；1 = 有步骤失败（其余步骤仍会跑完，别留半截）。"""
    report = reporter or Reporter(dry_run=dry_run)
    report.head("依赖检查")
    failed = []
    for ok, line in check_dependencies():
        print(f"  {'✓' if ok else '✗'} {line}")
        if not ok:
            failed.append(line)
    ok, line = gtk_available()
    print(f"  {'✓' if ok else '✗'} {line}")

    good = True
    install_bins(report)
    install_desktop(report)
    if extension:
        good = sync_extension(report, sync=True) and good
    else:
        report.head("跳过扩展（--no-extension）")
    if keyd:
        good = apply_keyd(report) and good
    else:
        report.head("跳过 keyd（--no-keyd）—— 键位不会生效，记得另外跑 capsplus apply")
    if units:
        good = install_units(report) and good
    else:
        report.head("跳过 systemd 单元（--no-units）")

    if good:
        retire_legacy(report)

    print("\n== 收尾 ==")
    if report.dry_run:
        print("  这是 --dry-run：一个字节都没改。去掉它才会真的落盘。")
        return 0
    if failed:
        print(f"  ! 有 {len(failed)} 项依赖缺失，功能会受限（见上面 ✗）")
    # 最后一步永远是把"这台机器现在什么状态"打给用户 —— 装完还得让人知道下一步
    from .cli import render_status

    print()
    render_status()
    return 0 if good else 1


def uninstall(dry_run: bool = False, purge_extension: bool = False,
              purge_keyd: bool = False, reporter: Reporter | None = None) -> int:
    """卸掉用户侧的一切。默认**不碰** /etc/keyd 与你自己的配置（那是数据，不是装机件）。"""
    report = reporter or Reporter(dry_run=dry_run)
    report.head("停止并禁用用户侧单元组")
    if not report.dry_run and shutil.which("systemctl"):
        systemctl_user("disable", "--now", "capsplus.target")
        systemctl_user("stop", "capsplus-tray.service")
        systemctl_user("disable", "capsplus-extension-sync.service")
    report.step("capsplus.target / capsplus-tray.service 已停止")

    report.head("退役装机件")
    for name in ENTRY_POINTS:
        path = bindir() / name
        if report.dry_run:
            if path.exists() or path.is_symlink():
                report.step(f"退役 {path}")
            continue
        where = retire(path)
        if where:
            report.ok(f"{path} -> {where}")
    for name in USER_UNITS:
        path = UNIT_DIR / name
        if report.dry_run:
            if path.exists():
                report.step(f"退役 {path}")
            continue
        where = retire(path)
        if where:
            report.ok(f"{path} -> {where}")
    for _rel, name in DESKTOP_ENTRIES:
        path = APPLICATIONS_DIR / name
        if report.dry_run:
            if path.exists():
                report.step(f"退役 {path}")
            continue
        where = retire(path)
        if where:
            report.ok(f"{path} -> {where}")
    if not report.dry_run and shutil.which("systemctl"):
        systemctl_user("daemon-reload")
        if shutil.which("update-desktop-database"):
            _run(["update-desktop-database", str(APPLICATIONS_DIR)])

    if purge_extension:
        from .extcheck import EXT_DIR

        path = Path(EXT_DIR).expanduser()
        if report.dry_run:
            report.step(f"退役扩展目录 {path}")
        else:
            where = retire(path)
            if where:
                report.ok(f"扩展目录 {path} -> {where}")

    if purge_keyd:
        # 配置本身是可再生的（render 出来的），但删之前仍进焚化炉：这一份可能带着手工改动
        from .apply import KEYD_CONF

        if report.dry_run:
            report.step(f"退役 {KEYD_CONF}")
        else:
            where = retire(KEYD_CONF)
            if where:
                report.ok(f"keyd 配置 {KEYD_CONF} -> {where}")
                if any(shutil.which(n) for n in KEYD_BIN_NAMES):
                    from .env import resolve_keyd_bin

                    _run(["sudo", "-n", resolve_keyd_bin(), "reload"])

    print("\n== 说明 ==")
    print("  · ~/.config/capsplus/（快速链接、键位覆盖层）与 ~/.local/state/capsplus/（历史、日志）"
          "\n    是你的数据，**没有**动。要清自己删。")
    print("  · /etc/keyd/capslock-plus.conf 默认保留（--purge-keyd 才会退役它）。")
    if report.warnings:
        print("\n== 警告 ==")
        for line in report.warnings:
            print(f"  ! {line}")
    return 0
