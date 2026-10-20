#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
# raise-browser 扩展的**部署（写）**与**校验（只读）**。
#
# 三种模式（2026-10-20 定，别再混着用）：
#   --sync    （默认）把仓里的扩展复制进 ~/.local/share/gnome-shell/extensions，
#             并保证 uuid 在 enabled-extensions 里。**写入只允许发生在这一侧。**
#   --verify  只校验、**一个字节都不写**：比对仓内与部署副本的 md5，顺带看是否在 enabled-extensions 里。
#             登录时的 unit 走这一支（packaging/capsplus-extension-sync.service），
#             `capsplus ext verify` 也走它。
#   --quiet   成功不说话（给 systemd oneshot 用）。错误与警告照旧进 stderr。
#
# 为什么登录那一步从"写"改成"只校验"（2026-10-20 真事故）：
#   本 unit 是 After=graphical-session.target，正好在 gnome-shell 扫描扩展目录时执行 `cp`，
#   于是 shell 读到了**拷贝中的半截文件**：
#     gnome-shell: Extension raise-browser@capsplus.example:
#       TypeError: malformed UTF-8 character sequence at offset 8190
#   （8190 ≈ 8192-2 = 一个汉字被切在两半；两份文件本身都合法，坏的只是那一刻的时序）
#   ⇒ 扩展 import 失败 ⇒ 抓帧通道没有 ⇒ 按 Caps+F1 完全没反应、只能重登。
#   现在三头都堵上：sync 侧**先预检**（文件本身是坏的就不写）、再原子落盘（同目录临时名 + rename）
#   且"内容相同就不写"；登录侧只做 md5 比对 —— 稳态下登录根本不碰这些文件，连被读到半截的机会都没有。
#
# `--sync` 是**预检闸门**后的写：`sh tools/preflight-extension.sh` 红（退出 1）或环境不齐（退出 2）
# 时**一个字节都不写**（连目标目录都不建），退出码原样透传。预检查：合法 UTF-8（能报坏在第几字节）、
# metadata.json 合法且 uuid 对得上、`export default class` 形态、`node --check` 解析、
# 带 mock Shell 的 import 冒烟。反例具型见 docs/SHOT-TOOL.md 第三节。
#
# ⚠ 装/同步完仍需**注销重登一次**才生效：GNOME 只在启动时扫描用户扩展目录，
#   `ReloadExtension` 上游已废弃（实测返回 deprecated and does not work），
#   Wayland 也没有 X11 那个 Alt+F2 r。**但自检不用重登**：
#   `sh tools/ext-bench.sh` 起一个隔离的 headless shell 验一遍，绿了再去重登
#   —— 重登是最后一步，不是排查手段。
#
# 用法：
#   sh tools/install-extension.sh            # = --sync（先预检，红了不写）
#   sh tools/install-extension.sh --verify   # 只校验（登录 unit 用它）
#   sh tools/install-extension.sh --quiet    # 给 oneshot 用：成功不说话
#   CAPSPLUS_NOTIFY=0 sh tools/install-extension.sh   # 失败也不弹桌面通知（反例举证/CI 用）
set -eu

UUID="raise-browser@capsplus.example"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_DIR/extensions/$UUID"
DST_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
FILES="metadata.json extension.js stylesheet.css"

MODE=sync
QUIET=0
for _arg in "$@"; do
    case "$_arg" in
        --sync) MODE=sync ;;
        --verify) MODE=verify ;;
        --quiet) QUIET=1 ;;
        *)
            echo "✗ 不认识的参数：$_arg（可用：--sync / --verify / --quiet）" >&2
            exit 2
            ;;
    esac
done
say() { [ "$QUIET" = 1 ] || printf '%s\n' "$1"; }
warn() { printf '%s\n' "$1" >&2; }
md5_of() { md5sum < "$1" | cut -d' ' -f1; }

# 失败必须**出声**：登录 unit 与 keyd 这两条路上都没人看 stderr，而"扩展落后 / 没加载"的表现
# 恰恰就是"Caps+F1 没反应" —— 不说出来等于没发生（2026-10-20 明江为此白重登过）。
# GIO 优先：`gdbus` 是 GLib 自带的，不引新依赖；没有才退回 notify-send。
# 通知发不出去只是少一条提示，**不许**让它把脚本/unit 带崩，所以两条路都兜住、永远 return 0。
notify() {
    _title="$1"
    _body="$2"
    # `CAPSPLUS_NOTIFY=0` 关掉（与 capsplus/shot_frame.py 同一套开关）。为什么要有：反例举证时
    # 脚本会**故意**让预检红，那时不该把弹窗拍到明江屏幕上 —— 他不欠我们一次打扰。
    [ "${CAPSPLUS_NOTIFY:-1}" = 0 ] && return 0
    if command -v gdbus >/dev/null 2>&1; then
        if gdbus call --session --dest org.freedesktop.Notifications \
               --object-path /org/freedesktop/Notifications \
               --method org.freedesktop.Notifications.Notify \
               "capsplus" 0 "" "$_title" "$_body" "[]" "{}" 8000 >/dev/null 2>&1; then
            return 0
        fi
    fi
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -a capsplus "$_title" "$_body" >/dev/null 2>&1 || true
    fi
    return 0
}

# UUID 在不在 enabled-extensions 里：yes / no / unknown。
# `unknown` 是**故意**的第三态：读不到（非 GNOME、没 gsettings、没 schema）不等于没启用，
# 拿它判红会在 CI 上假红（那台机连 /etc 都不碰）。2026-10-20 实测 gsettings get 在无 schema 时退出非 0。
enabled_state() {
    command -v gsettings >/dev/null 2>&1 || { echo unknown; return 0; }
    _cur="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null)" || { echo unknown; return 0; }
    case "$_cur" in
        *"$UUID"*) echo yes ;;
        *) echo no ;;
    esac
}

if [ ! -f "$SRC_DIR/extension.js" ]; then
    warn "✗ 找不到 $SRC_DIR/extension.js（请在仓目录里跑本脚本；CAPSPLUS_REPO 指对了吗）"
    exit 1
fi

# --------------------------------------------------------------- --verify：只读，一个字节都不写
verify_only() {
    _bad=0
    if [ ! -d "$DST_DIR" ]; then
        warn "✗ 还没部署：$DST_DIR 不存在（扩展不加载 ⇒ Caps+F1 静默失效）"
        _bad=1
    else
        for _f in $FILES; do
            [ -f "$SRC_DIR/$_f" ] || continue
            if [ ! -f "$DST_DIR/$_f" ]; then
                warn "✗ 部署副本缺 $_f（$DST_DIR）⇒ 扩展会缺东西加载"
                _bad=1
            elif [ "$(md5_of "$SRC_DIR/$_f")" != "$(md5_of "$DST_DIR/$_f")" ]; then
                warn "✗ $_f 部署副本落后于仓：shell 里跑的还是旧代码"
                _bad=1
            fi
        done
    fi
    _state="$(enabled_state)"
    case "$_state" in
        no)
            warn "✗ 装是装了，但不在 enabled-extensions 里（shell 根本不会加载它）"
            _bad=1
            ;;
        unknown)
            warn "• 读不到 enabled-extensions（没有 gsettings / 没有 org.gnome.shell schema？）—— 这一项跳过"
            ;;
    esac
    if [ "$_bad" = 0 ]; then
        # 顺序有讲究：`capsplus ext verify` 只把**最后一行**当结论行打出来（capsplus/install.py），
        # 所以"结论"必须压在最末。
        say "  shell 里是否**真的加载**、抓帧/贴图通道是否挂上：sh tools/ext-bench.sh（不用重登）"
        say "✓ 扩展已就位：部署副本与仓内一致，且 uuid 在 enabled-extensions 里"
        return 0
    fi
    warn "  这一侧**什么都不写**（登录时写文件正是 8190 半截文件那个竞态的根源）。要修："
    warn "    sh tools/install-extension.sh --sync"
    warn "  然后注销重登一次 —— 改过 extension.js 后 disable/enable 不会重新 import（2026-10-19 实测）"
    notify "capsplus 扩展没就位" "部署副本与仓内不一致或没启用 ⇒ Caps 热键会静默失效。跑 sh tools/install-extension.sh --sync，再注销重登一次"
    return 1
}

if [ "$MODE" = verify ]; then
    # 显式判一次返回码，而不是靠 set -e 顺带退出：unit 那边看的正是这个退出码（非 0 = 红）
    if verify_only; then
        exit 0
    fi
    exit 1
fi

# ---------------------------------------------------------------------- --sync：写入（唯一允许写的地方）

# ------------------------------------------------- 部署前预检（强制闸门，在**任何写入之前**）
# 为什么是闸门而不是提示（2026-10-20）：坏文件进了扩展目录 ⇒ shell import 失败 ⇒ 进 ERROR 态
# ⇒ Wayland 下**只能注销重登**才能恢复。原子落盘只堵住"被读到写一半"，堵不住"写进去的本身是坏的"。
# 闸门放在 `mkdir -p "$DST_DIR"` **之前**：连目录都不建，"一个字节都不写"才是可核对的事实。
# 退出码原样透传（0 绿 / 1 红 / 2 环境不齐）—— 调用方（keyd、人手、CI）靠它判。
# ⚠ **只管 --sync 这一侧**：登录 unit 走的是 `--verify`，那条路必须又轻又稳 ——
#   把 node 依赖压到登录路径上，会让"这台机没装 node"变成一个假的登录红。
_pre_rc=0
sh "$REPO_DIR/tools/preflight-extension.sh" --ext-dir "$SRC_DIR" || _pre_rc=$?
if [ "$_pre_rc" != 0 ]; then
    warn ""
    warn "✗ 预检没过（退出码 $_pre_rc）⇒ **不部署**：部署目录一个字节都没写。"
    warn "  修好上面标 ✗ 的那几行再跑一次；只想看结论、不写文件：sh tools/preflight-extension.sh"
    notify "capsplus 扩展预检没过" "坏文件没进扩展目录（部署副本保持原样）⇒ Caps 热键不会因此坏掉。修好仓内 extension.js 再跑 sh tools/install-extension.sh --sync"
    exit "$_pre_rc"
fi

mkdir -p "$DST_DIR"

# ⚠ 必须**原子落盘**（先在同目录写临时名，再 rename）：同目录 rename 是原子的，
#   shell 只会看到"旧完整文件"或"新完整文件"，永远不会看到拷贝中的半截。
install_atomic() {
    _rel="$1"
    _base="$(basename "$1")"
    # 内容一样就**一个字节都不写**：稳态下连"被读到半截"的机会都没有
    if [ -f "$DST_DIR/$_base" ] && \
       [ "$(md5_of "$SRC_DIR/$_rel")" = "$(md5_of "$DST_DIR/$_base")" ]; then
        return 0
    fi
    cp "$SRC_DIR/$_rel" "$DST_DIR/.$_base.tmp.$$"
    mv -f "$DST_DIR/.$_base.tmp.$$" "$DST_DIR/$_base"
}
install_atomic metadata.json
install_atomic extension.js
# stylesheet.css 也要复制：GNOME 会自动加载**扩展目录下**的同名样式表，
# 少了它 extension.js 里那些 style_class 一个都不生效 —— 2026-10-19 明江那句
# "贴出来的图 删都删不掉"就是这条（`×` 变成裸按钮：没尺寸、没背景、没 hover）。
if [ -f "$SRC_DIR/stylesheet.css" ]; then
    install_atomic stylesheet.css
else
    warn "⚠ 缺 $SRC_DIR/stylesheet.css：贴图会退回裸控件观感（按钮看不出能点）"
fi
# 落盘后校对：md5 两边一致才认（半截文件从此当场现形，而不是等 shell 报 UTF-8 错）
for _f in $FILES; do
    [ -f "$SRC_DIR/$_f" ] || continue
    if [ "$(md5_of "$SRC_DIR/$_f")" != "$(md5_of "$DST_DIR/$_f")" ]; then
        warn "✗ $_f 落盘后与源不一致（原子拷贝失败？）"
        notify "capsplus 扩展部署失败" "$_f 落盘后与仓内不一致 —— Caps 热键会静默失效。看终端输出/日志后重跑 sh tools/install-extension.sh --sync"
        exit 1
    fi
done
say "✓ 已复制到 $DST_DIR（含 stylesheet.css；原子落盘 + md5 校对）"

# 加进已启用列表（保留用户原有的，不覆盖）
_state="$(enabled_state)"
case "$_state" in
    yes)
        say "• enabled-extensions 里已经有它，不动"
        ;;
    no)
        CUR="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null)" || CUR="[]"
        NEW="$(printf '%s' "$CUR" | sed "s#\]\$#, '$UUID']#")"
        if gsettings set org.gnome.shell enabled-extensions "$NEW" 2>/dev/null; then
            say "✓ 已加进 enabled-extensions：$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '?')"
        else
            warn "⚠ 启用列表没写成功（需要会话总线？）—— 手动跑一次："
            warn "    gsettings set org.gnome.shell enabled-extensions \"$NEW\""
        fi
        ;;
    *)
        warn "⚠ 这台机读不到 gsettings（非 GNOME？）—— 复制完了可能还要在「扩展」应用里手动打开"
        ;;
esac

echo
say "下一步：**注销再登录**（或重启）才生效 —— GNOME 只在启动时扫描扩展目录。"
say "  自检不用重登：sh tools/ext-bench.sh   # 绿了再去重登，重登是最后一步"
say "  看状态：capsplus ext status（或 gnome-extensions info $UUID）"
say "  注意：别再拿 \`gdbus … Probe\` 判活 —— 本机 gjs 导出不了 D-Bus 对象，那条命令只会报"
say "        UnknownMethod（2026-10-20 实测）；判活只认 journal 里的 \`[raise-browser]\` 行。"
echo
say "要撤销：gsettings set org.gnome.shell enabled-extensions \"<去掉这个 uuid 的列表>\""
say "         再把目录移进回收处（别直接 rm -rf，便于反悔）："
say "           mkdir -p /tmp/attic-\$(date +%Y%m%d) && \\"
say "             mv ~/.local/share/gnome-shell/extensions/$UUID /tmp/attic-\$(date +%Y%m%d)/"
