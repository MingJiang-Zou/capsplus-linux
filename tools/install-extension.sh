#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
# 装 raise-browser 扩展：复制到用户扩展目录 + 加进 enabled-extensions。
#
# ⚠ 必须**重新登录一次**才会生效：GNOME Shell 只在启动时扫描用户扩展目录，
#   当场 `gnome-extensions enable` 会报「扩展不存在」（2026-10-16 在 GNOME 50.1 实测）。
#   Wayland 会话也不能像 X11 那样 Alt+F2 + r 热重启 shell ⇒ 只有注销重登 / 重启。
#
# 用法：sh tools/install-extension.sh
set -eu

# --quiet：只报错误与"下一步该干什么"，给 systemd oneshot 用
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = 1 ] || printf '%s\n' "$1"; }

UUID="raise-browser@capsplus.example"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)/extensions/$UUID"
DST_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"

if [ ! -f "$SRC_DIR/extension.js" ]; then
    echo "✗ 找不到 $SRC_DIR/extension.js（请在仓目录里跑本脚本）" >&2
    exit 1
fi

mkdir -p "$DST_DIR"

# ⚠ 必须**原子落盘**（先在同目录写临时名，再 rename）—— 2026-10-20 真事故：
#   本 unit 是 After=graphical-session.target，正好在 gnome-shell 扫描扩展目录时执行 `cp`，
#   于是 shell 读到了**拷贝中的半截文件**：
#     gnome-shell: Extension raise-browser@capsplus.example:
#       TypeError: malformed UTF-8 character sequence at offset 8190
#   （8190 ≈ 8192-2 = 一个汉字被切在两半；文件本身两份都合法，坏的只是那一刻的时序）
#   ⇒ 扩展 import 失败 ⇒ 抓帧通道没有 ⇒ 明江按 Caps+F1 完全没反应、只能重登。
#   同目录 rename 是原子的：shell 只会看到"旧完整文件"或"新完整文件"。
install_atomic() {
    _rel="$1"; _base="$(basename "$1")"
    # 内容一样就**一个字节都不写**：稳态下登录时根本不碰这些文件，连"被读到半截"的机会都没有
    if [ -f "$DST_DIR/$_base" ] && \
       [ "$(md5sum < "$SRC_DIR/$_rel" | cut -d' ' -f1)" = "$(md5sum < "$DST_DIR/$_base" | cut -d' ' -f1)" ]; then
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
    echo "⚠ 缺 $SRC_DIR/stylesheet.css：贴图会退回裸控件观感（按钮看不出能点）" >&2
fi
# 落盘后校对：尺寸 + md5 两边一致才认（半截文件从此当场现形，而不是等 shell 报 UTF-8 错）
for _f in metadata.json extension.js stylesheet.css; do
    [ -f "$SRC_DIR/$_f" ] || continue
    if [ "$(md5sum < "$SRC_DIR/$_f" | cut -d' ' -f1)" != "$(md5sum < "$DST_DIR/$_f" | cut -d' ' -f1)" ]; then
        echo "✗ $_f 落盘后与源不一致（原子拷贝失败？）" >&2
        exit 1
    fi
done
say "✓ 已复制到 $DST_DIR（含 stylesheet.css；原子落盘 + md5 校对）"

# 追加进已启用列表（保留用户原有的，不覆盖）
if command -v gsettings >/dev/null 2>&1; then
    CUR="$(gsettings get org.gnome.shell enabled-extensions)"
    case "$CUR" in
        *"$UUID"*) echo "• enabled-extensions 里已经有它，不动" ;;
        *)
            NEW="$(printf '%s' "$CUR" | sed "s#\]\$#, '$UUID']#")"
            gsettings set org.gnome.shell enabled-extensions "$NEW"
            say "✓ 已加进 enabled-extensions：$(gsettings get org.gnome.shell enabled-extensions)"
            ;;
    esac
else
    echo "⚠ 这台机没有 gsettings（非 GNOME？）—— 复制完可能还要手动在「扩展」应用里打开" >&2
fi

echo
say "下一步：**注销再登录**（或重启）。之后自检："
say "  gdbus call --session --dest io.github.capsplus.RaiseBrowser \\"
say "    --object-path /io/github/capsplus/RaiseBrowser \\"
say "    --method io.github.capsplus.RaiseBrowser.Probe"
say "  # 期望输出形如 (true, 'default=microsoft-edge.desktop windows=1 on-current=1 …')"
echo
say "要撤销：gsettings set org.gnome.shell enabled-extensions \"<去掉这个 uuid 的列表>\""
say "         再把目录移进回收处（别直接 rm -rf，便于反悔）："
say "           mkdir -p /tmp/attic-\$(date +%Y%m%d) && \\"
say "             mv ~/.local/share/gnome-shell/extensions/$UUID /tmp/attic-\$(date +%Y%m%d)/"
