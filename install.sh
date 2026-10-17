#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
# capsplus-linux 安装脚本：只装用户侧（bin 软链、autostart、应用菜单入口），并检查依赖。
# 刻意**不碰** /etc/keyd —— 那份配置由你手工跑 `capsplus apply` 并审 diff。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 与 capsplus/env.py:bindir() 同一口径：CAPSPLUS_BINDIR 指哪儿就装哪儿（渲染出的 keyd
# command(...) 路径也是它，两边必须一致，否则 keyd 找不到唤起 Qbar 的脚本）
BIN_DIR="${CAPSPLUS_BINDIR:-${HOME}/.local/bin}"
AUTOSTART_DIR="${HOME}/.config/autostart"
APPS_DIR="${HOME}/.local/share/applications"
WITH_AUTOSTART=1

for arg in "$@"; do
  case "$arg" in
    --no-autostart) WITH_AUTOSTART=0 ;;
    # 顶部第 2..5 行是 GPL 声明块，帮助只印真正的说明（6..7 行）
    -h|--help) sed -n '6,7p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

echo "== 依赖检查 =="
missing=0
check_cmd() { if command -v "$1" >/dev/null 2>&1; then echo "  ✓ $1 -> $(command -v "$1")"; else echo "  ✗ 缺少 $1"; missing=1; fi; }
# runuser 在 /usr/sbin，普通用户 PATH 里没有 —— 只有 root（keyd）调用时才用得到
check_runuser() {
  for c in "$(command -v runuser || true)" /usr/sbin/runuser /sbin/runuser; do
    if [ -n "$c" ] && [ -x "$c" ]; then echo "  ✓ runuser -> $c（仅 root/keyd 侧使用）"; return; fi
  done
  echo "  ✗ 缺少 runuser（keyd 唤起 Qbar 需要它）"; missing=1
}
# keyd 的可执行名各发行版不一样：Debian/Ubuntu 因与 onak 撞名装成 keyd.rvaiya，
# Arch / Fedora / openSUSE 就叫 keyd。两个都认；都没装只给命令，**不替你装**（它要动内核输入层）。
check_keyd() {
  for c in keyd.rvaiya keyd; do
    if command -v "$c" >/dev/null 2>&1; then echo "  ✓ keyd -> $(command -v "$c")"; return; fi
  done
  echo "  ✗ 没找到 keyd / keyd.rvaiya —— 键位映射这块整个用不了。各发行版装法（自己挑一条）："
  echo "      Debian/Ubuntu : sudo apt install keyd.rvaiya   （Debian 系；Ubuntu 也可用上游 .deb）"
  echo "      Arch          : sudo pacman -S keyd"
  echo "      Fedora        : sudo dnf install keyd"
  echo "      openSUSE      : sudo zypper install keyd"
  echo "      通用（上游源码）: https://github.com/rvaiya/keyd  README 的 Install 一节"
  missing=1
}
# fd 也是同名不同物：Debian/Ubuntu 叫 fdfind，别处多半叫 fd（Qbar 的文件搜索要用）
check_fd() {
  for c in fdfind fd; do
    if command -v "$c" >/dev/null 2>&1; then echo "  ✓ $c -> $(command -v "$c")（Qbar 文件搜索）"; return; fi
  done
  echo "  ✗ 没找到 fd/fdfind —— 只影响 Qbar 搜文件，其余功能不受影响"
  echo "      Debian/Ubuntu: sudo apt install fd-find    Arch: sudo pacman -S fd    Fedora: sudo dnf install fd"
  missing=1
}
# tomllib 是 3.11 才进标准库的，版本不够时快速链接配置根本读不了 —— 早点说清楚
check_python() {
  if ! command -v python3 >/dev/null 2>&1; then echo "  ✗ 缺少 python3"; missing=1; return; fi
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "  ✓ python3 -> $(command -v python3)（$(python3 -V 2>&1)）"
  else
    echo "  ✗ python3 版本太低：$(python3 -V 2>&1)。本项目用了标准库 tomllib，**需要 >= 3.11**"
    echo "      Ubuntu 24.04 及更早: sudo apt install python3.11（或 python3.12），再用它跑 install.sh"
    echo "      Fedora/Arch        : 系统 python3 已经够新，一般不用管"
    missing=1
  fi
}
check_python
check_fd
check_keyd
check_runuser
check_cmd desktop-file-validate

python3 - <<'PY' || missing=1
import sys
try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import Gtk, AyatanaAppIndicator3  # noqa
    print(f"  ✓ PyGObject GTK3 {Gtk.get_major_version()}.{Gtk.get_minor_version()} + AyatanaAppIndicator3")
except Exception as exc:  # noqa: BLE001
    print(f"  ✗ PyGObject/GTK3/AppIndicator 不可用: {exc}")
    print("    需要: python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1")
    sys.exit(1)
PY

# 设置窗口（capsplus-settings）只要能连上显示服务器就能起；
# 连不上不算装坏（SSH / headless 里跑安装是常事），所以这里只提示、不置 missing。
python3 - <<'PY' || true
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
ok, _ = Gtk.init_check()
print("  ✓ 显示服务器可用：capsplus-settings 能起窗" if ok
      else "  · 当前连不上显示服务器：capsplus-settings 装了也起不了窗（其余功能不受影响）")
PY

echo "== 安装 bin 软链 =="
mkdir -p "$BIN_DIR"
for name in capsplus capsplus-tray capsplus-qbar capsplus-settings keyd-qbar-cmd capsplus-voice; do
  ln -sfn "$REPO/bin/$name" "$BIN_DIR/$name"
  echo "  $BIN_DIR/$name -> $REPO/bin/$name"
done

echo "== 安装应用菜单入口 =="
# 模板里的 @BIN_DIR@ / @REPO_DIR@ 换成本机实际路径再装 —— 仓里不许留任何具体用户的绝对路径。
render_template() {    # $1=模板 $2=目标
  sed -e "s#@BIN_DIR@#$BIN_DIR#g" -e "s#@REPO_DIR@#$REPO#g" "$1" > "$2"
}
mkdir -p "$APPS_DIR"
# 装两条：Qbar 启动器 + 设置界面。
# 托盘**不**装（它开机自启，应用菜单里再放一条是冗余 —— 2026-10-15 用户实测反馈"应用里面有两个"）；
# 设置界面要装，因为它是真能点的窗口，而且托盘图标的宿主扩展（ubuntu-appindicators）
# 有可能处于 INACTIVE（本会话没加载），那时应用菜单是唯一不靠命令行的入口。
for pair in "packaging/capsplus-qbar.desktop:capsplus-qbar.desktop" \
            "packaging/capsplus-settings.desktop:capsplus-settings.desktop"; do
  src="$REPO/${pair%%:*}"
  dst="$APPS_DIR/${pair#*:}"
  render_template "$src" "$dst"
  echo "  $dst"
done
if command -v desktop-file-validate >/dev/null 2>&1; then
  if desktop-file-validate "$APPS_DIR/capsplus-qbar.desktop" "$APPS_DIR/capsplus-settings.desktop"; then
    echo "  ✓ desktop-file-validate 通过"
  else
    echo "  ✗ desktop-file-validate 报错（上面那些行）" >&2
    missing=1
  fi
else
  echo "  · 跳过 desktop-file-validate（本机没这个命令），用 gio info 自查"
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" && echo "  ✓ 已更新 desktop 数据库"
else
  echo "  · 跳过 update-desktop-database（本机没这个命令）"
fi

if [ "$WITH_AUTOSTART" = 1 ]; then
  echo "== 安装 autostart =="
  mkdir -p "$AUTOSTART_DIR"
  render_template "$REPO/packaging/capsplus-tray.desktop" "$AUTOSTART_DIR/capsplus-tray.desktop"
  echo "  $AUTOSTART_DIR/capsplus-tray.desktop"
  echo "  （不想开机自启就删掉它，或下次加 --no-autostart）"
else
  echo "== 跳过 autostart（--no-autostart）=="
  echo "  注意：应用菜单入口不受影响，Qbar 仍能在应用网格里搜到"
fi

echo "== 状态目录 =="
mkdir -p "${HOME}/.local/state/capsplus/keyd-conf-backup"
echo "  ${HOME}/.local/state/capsplus/"

if [ "$missing" != 0 ]; then
  echo; echo "有依赖缺失，功能会受限（见上面 ✗）。" >&2
fi

cat <<EOF

下一步（手工，脚本不代劳）：
  1) 看渲染结果:      capsplus render | less
  2) 应用（会备份+reload，出错自动回滚）:  capsplus apply
  3) 状态:            capsplus status
  4) 托盘常驻:        capsplus-tray   （autostart 已装；想用 systemd --user 就照
                        packaging/capsplus-tray.service 头部那三行命令自己渲染+启用）
  5) Qbar:            capsplus-qbar   （keyd 侧用 bin/keyd-qbar-cmd 包装）
  6) 快速链接配置:    ${HOME}/.config/capsplus/qbar.toml
     （首次打开 Qbar 时自动生成；托盘右键「编辑快速链接」也能开）
  7) 设置窗口:        capsplus-settings
     三页：快速链接 / 通用 / 按键映射；改完按键要点「应用」才生效，出错自动回滚。
     入口只有三个：托盘「设置…」、Qbar 里输 -> settings、命令行 capsplus-settings。
     刻意没装 .desktop —— 应用菜单里条目越多越难找（上一轮刚删掉一条）。
EOF
