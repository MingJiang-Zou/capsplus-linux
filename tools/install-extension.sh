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

UUID="raise-browser@capsplus.example"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)/extensions/$UUID"
DST_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"

if [ ! -f "$SRC_DIR/extension.js" ]; then
    echo "✗ 找不到 $SRC_DIR/extension.js（请在仓目录里跑本脚本）" >&2
    exit 1
fi

mkdir -p "$DST_DIR"
cp "$SRC_DIR/metadata.json" "$SRC_DIR/extension.js" "$DST_DIR/"
echo "✓ 已复制到 $DST_DIR"

# 追加进已启用列表（保留用户原有的，不覆盖）
if command -v gsettings >/dev/null 2>&1; then
    CUR="$(gsettings get org.gnome.shell enabled-extensions)"
    case "$CUR" in
        *"$UUID"*) echo "• enabled-extensions 里已经有它，不动" ;;
        *)
            NEW="$(printf '%s' "$CUR" | sed "s#\]\$#, '$UUID']#")"
            gsettings set org.gnome.shell enabled-extensions "$NEW"
            echo "✓ 已加进 enabled-extensions：$(gsettings get org.gnome.shell enabled-extensions)"
            ;;
    esac
else
    echo "⚠ 这台机没有 gsettings（非 GNOME？）—— 复制完可能还要手动在「扩展」应用里打开" >&2
fi

echo
echo "下一步：**注销再登录**（或重启）。之后自检："
echo "  gdbus call --session --dest io.github.capsplus.RaiseBrowser \\"
echo "    --object-path /io/github/capsplus/RaiseBrowser \\"
echo "    --method io.github.capsplus.RaiseBrowser.Probe"
echo "  # 期望输出形如 (true, 'default=microsoft-edge.desktop windows=1 on-current=1 …')"
echo
echo "要撤销：gsettings set org.gnome.shell enabled-extensions \"<去掉这个 uuid 的列表>\""
echo "         再把目录移进回收处（别直接 rm -rf，便于反悔）："
echo "           mkdir -p /tmp/attic-\$(date +%Y%m%d) && \\"
echo "             mv ~/.local/share/gnome-shell/extensions/$UUID /tmp/attic-\$(date +%Y%m%d)/"
