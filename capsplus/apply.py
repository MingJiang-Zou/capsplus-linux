# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""capsplus apply：渲染 -> 装到 /etc/keyd/capslock-plus.conf -> reload -> 查错 -> 必要时回滚。

流程（每一步都可单独调用，便于测试与手工救火）：
  1. 渲染到临时文件（先做归一化自检，macro 行带行尾注释在这里就会被抓出来）
  2. 备份现有 /etc/keyd/capslock-plus.conf 到 ~/.local/state/capsplus/keyd-conf-backup/<时间戳>
     （保留最近 10 份，超出的 mv 进焚化炉 /tmp/attic-<日期>/）
  3. sudo cp 新配置过去
  4. sudo keyd.rvaiya reload
  5. 抓 sudo journalctl -u keyd --since '10 seconds ago'，出现 "ERROR:" 行 ->
     cp 回滚上一版 + 再 reload + 以非 0 退出码返回

都用 `sudo -n`（非交互）：没配免密就立刻失败并给提示，绝不挂在密码提示上。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .render import render, normalized_bindings
from .mapping_overrides import OverrideError, load_and_merge, load_overrides

KEYD_CONF = Path("/etc/keyd/capslock-plus.conf")
KEYD_BIN = shutil.which("keyd.rvaiya") or "/usr/bin/keyd.rvaiya"
BACKUP_DIR = Path.home() / ".local/state/capsplus/keyd-conf-backup"
KEEP_BACKUPS = 10
ERROR_RE = re.compile(r"^.*ERROR:.*$", re.M)


class ApplyError(RuntimeError):
    """apply 失败（含已回滚）。message 里带可执行的下一步提示。"""


@dataclass
class ApplyResult:
    reloaded: bool = False
    log: str = ""
    errors: list[str] = field(default_factory=list)
    backup: Path | None = None
    rolled_back: bool = False

    @property
    def ok(self) -> bool:
        return self.reloaded and not self.errors and not self.rolled_back


def _run(cmd: list[str], need_root: bool = False, check: bool = True,
         input_text: str | None = None) -> subprocess.CompletedProcess:
    if need_root:
        cmd = ["sudo", "-n", *cmd]
    proc = subprocess.run(cmd, capture_output=True, text=True, input=input_text)
    if check and proc.returncode != 0:
        hint = ""
        if need_root and "password is required" in (proc.stderr or ""):
            hint = "\n提示: 需要免密 sudo（见 README「免密 sudo」一节），或手工执行该命令。"
        raise ApplyError(f"命令失败({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}{hint}")
    return proc


def attic_dir() -> Path:
    """焚化炉（按规矩删除 = mv 进 attic，不用 rm）。"""
    return Path(f"/tmp/attic-{datetime.now():%Y%m%d}")


def _snapshot() -> Path | None:
    """把当前配置存成一份带时间戳的副本，返回该副本路径（不改动配置本身）。

    故意不在最后调 _prune_backups()：裁剪必须在"这份副本已经不被需要"之后再做，
    否则 snapshot 一返回就可能把自己裁进焚化炉（上一版就没了）。
    """
    if not KEYD_CONF.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"capslock-plus.conf.{datetime.now():%Y%m%d-%H%M%S-%f}"
    shutil.copy2(KEYD_CONF, dest)
    return dest


def _prune_backups(keep: int = KEEP_BACKUPS) -> None:
    """裁掉最旧的备份（mv 进焚化炉，不用 rm）。

    ⚠ 调用时机有坑：apply 里必须在快照**之前**用 keep=KEEP_BACKUPS-1 裁一次。
    否则新快照会把本次要回滚的那一版挤出保留窗口，而 previous_backup() 取的是
    "最新一份"，结果就是"回滚到了刚刚的快照"= 回滚无效（2026-10-15 演练抓到的真 bug）。
    """
    copies = sorted(BACKUP_DIR.glob("capslock-plus.conf.*"), key=lambda p: p.name)
    stale = copies[:-keep] if keep > 0 else copies
    if not stale:
        return
    grave = attic_dir()
    grave.mkdir(parents=True, exist_ok=True)
    for path in stale:
        shutil.move(str(path), str(grave / path.name))


def previous_backup() -> Path | None:
    """备份目录里最新的一份（保留窗口内的历史版本），给人工救火用。"""
    copies = sorted(BACKUP_DIR.glob("capslock-plus.conf.*"), key=lambda p: p.name)
    return copies[-1] if copies else None


def keyd_log(since: str = "10 seconds ago") -> str:
    proc = subprocess.run(
        ["sudo", "-n", "journalctl", "-u", "keyd", "--no-pager", "--since", since],
        capture_output=True, text=True,
    )
    # journalctl 在"没有匹配记录"时返回 1，不算错误
    if proc.returncode not in (0, 1):
        raise ApplyError(f"journalctl 读取失败: {proc.stderr.strip()}")
    return proc.stdout


def parse_errors(log_text: str) -> list[str]:
    return [m.group(0).strip() for m in ERROR_RE.finditer(log_text or "")]


def reload_keyd() -> None:
    _run([KEYD_BIN, "reload"], need_root=True, check=False)
    # keyd reload 走的是 daemon 的 socket，退出码不完全可靠；真正判据是随后的 journal。


def install_conf(text: str) -> None:
    """把渲染结果装到 /etc/keyd/capslock-plus.conf（先写临时文件再 sudo cp）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        # 临时文件要给 root 可读（cp 是 sudo 跑的）
        os.chmod(tmp, 0o644)
        _run(["cp", str(tmp), str(KEYD_CONF)], need_root=True)
    finally:
        tmp.unlink(missing_ok=True)


def render_with_overrides():
    """渲染"仓库默认 + 用户覆盖层"的最终配置。

    覆盖层文件语法坏了 / 有非法条目 -> 抛 OverrideError（宁可拒绝，也不写出半份配置）。
    覆盖层里没有条目时，结果与 `render()` **逐字节相同**（单测锁住）。
    """
    data, overrides = load_and_merge()
    if overrides.error:
        raise OverrideError(f"{overrides.path} 读不了: {overrides.error}")
    if overrides.errors:
        raise OverrideError("用户覆盖层有问题（改好后重试，或删掉该文件）：\n  " +
                            "\n  ".join(overrides.errors))
    return render(data)


def apply(dry_run: bool = False, since: str = "10 seconds ago",
          text: str | None = None) -> ApplyResult:
    """主入口。dry_run=True 只渲染并自检，不碰系统；text 用于测试注入别的配置文本。"""
    text = render_with_overrides() if text is None else text
    normalized_bindings(text)  # macro 行带行尾注释 / 非法绑定行在这里就炸

    if dry_run:
        return ApplyResult(reloaded=False, log="dry-run: 已渲染并通过归一化自检，未改动系统")

    result = ApplyResult()
    # 顺序要害：先留上一版、再装新的、最后才裁剪。
    # ① 快照必须在 install 之前 —— 否则备份里存的是新装的（坏）配置，回滚 = 恢复坏配置；
    # ② 是否"本来就有配置"必须在 install 之前判断（install 之后文件必然存在）；
    # ③ 裁剪必须在快照之后 —— 否则刚做的快照可能被自己裁进焚化炉，回滚就没料可用。
    existed = KEYD_CONF.exists()
    # 快照 = "这一次替换掉的那一版" = 出错时要恢复的目标。
    # 它必须同时进保留窗口（人工救火可用），所以先腾位再快照。
    _prune_backups(KEEP_BACKUPS - 1)
    result.backup = _snapshot() if existed else None
    install_conf(text)
    reload_keyd()
    log = keyd_log(since)
    result.log = log
    result.errors = parse_errors(log)

    if result.errors:
        result.rolled_back = _rollback(result.backup)
        raise ApplyError(
            "keyd 报错:" +
            (f"\n  已回滚到上一版备份 {result.backup}" if result.rolled_back else
             f"\n  没有可回滚的备份（{KEYD_CONF} 原本不存在），请手工处理") +
            "\n  错误行:\n  " + "\n  ".join(result.errors)
        )
    result.reloaded = True
    return result


def _rollback(pre_install: Path | None) -> bool:
    """把出错前的配置恢复回去。返回 False 表示"原本就没有配置"。

    要害：恢复目标是 apply 开头做的那份快照（= 被本次替换掉的那一版），
    **不能**用 previous_backup() —— 那份快照自己就是目录里最新的一份，等于原地打转
    （2026-10-15 演练抓到的真 bug：日志说"已回滚"，文件却还是坏的）。
    """
    if pre_install is None:
        # 本来没有配置：把这次装进去的收进焚化炉，让 keyd 回到"没有这份配置"的状态
        grave = attic_dir()
        grave.mkdir(parents=True, exist_ok=True)
        if KEYD_CONF.exists():
            _run(["mv", str(KEYD_CONF), str(grave / "capslock-plus.conf.bad")], need_root=True)
        reload_keyd()
        return False
    _run(["cp", str(pre_install), str(KEYD_CONF)], need_root=True)
    reload_keyd()
    return True


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def service_state() -> str:
    proc = subprocess.run(["systemctl", "is-active", "keyd"], capture_output=True, text=True)
    return proc.stdout.strip() or proc.stderr.strip() or "unknown"


def config_summary(conf: Path = KEYD_CONF):
    """当前生效配置的摘要：(段名, 绑定条数) 列表 + 来源。"""
    if not conf.exists():
        return [], conf, False
    current = normalized_bindings(conf.read_text(encoding="utf-8"))
    rendered = normalized_bindings(render_with_overrides())
    counts: dict[str, int] = {}
    for section, _key, _action in current:
        counts[section] = counts.get(section, 0) + 1
    return sorted(counts.items()), conf, current == rendered


def last_log_line() -> str:
    proc = subprocess.run(
        ["sudo", "-n", "journalctl", "-u", "keyd", "--no-pager", "-n", "1"],
        capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):
        return f"(读取日志失败: {proc.stderr.strip()})"
    return proc.stdout.strip() or "(无日志)"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "render":
            # 渲染的是"默认 + 用户覆盖层"的最终结果（apply 装到 /etc/keyd 的就是它）
            sys.stdout.write(render_with_overrides())
            return 0
        if cmd == "apply":
            res = apply(dry_run="--dry-run" in argv)
            if res.reloaded:
                print(f"OK 已应用并 reload；备份: {res.backup}")
                print(res.log.strip().splitlines()[-1] if res.log.strip() else "")
            else:
                print(res.log)
            return 0
        if cmd == "status":
            counts, conf, same = config_summary()
            print(f"keyd 服务: {service_state()}")
            print(f"配置文件 : {conf} ({'存在' if conf.exists() else '不存在'})")
            print(f"与渲染一致: {'是' if same else '否（有人手改过或没 apply）'}")
            print("段/条数  : " + ", ".join(f"{s}={n}" for s, n in counts))
            ov = load_overrides()
            if ov.error:
                print(f"用户覆盖 : 读不了 —— {ov.error}")
            elif ov.count:
                print(f"用户覆盖 : {ov.path}（{ov.count} 条）")
            else:
                print(f"用户覆盖 : 无（全部用仓库默认）")
            for line in ov.errors:
                print(f"           ⚠ {line}")
            print(f"最近日志 : {last_log_line()}")
            return 0
        print(f"用法: capsplus [render|apply [--dry-run]|status]", file=sys.stderr)
        return 2
    except ApplyError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
