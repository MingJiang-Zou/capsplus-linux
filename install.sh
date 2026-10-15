#!/usr/bin/env bash
# capsplus-linux 安装脚本：只装用户侧（bin 软链、autostart、应用菜单入口），并检查依赖。
# 刻意**不碰** /etc/keyd —— 那份配置由你手工跑 `capsplus apply` 并审 diff。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
AUTOSTART_DIR="${HOME}/.config/autostart"
APPS_DIR="${HOME}/.local/share/applications"
WITH_AUTOSTART=1

for arg in "$@"; do
  case "$arg" in
    --no-autostart) WITH_AUTOSTART=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
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
check_cmd python3
check_cmd fdfind
check_cmd keyd.rvaiya
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
for name in capsplus capsplus-tray capsplus-qbar capsplus-settings keyd-qbar-cmd; do
  ln -sfn "$REPO/bin/$name" "$BIN_DIR/$name"
  echo "  $BIN_DIR/$name -> $REPO/bin/$name"
done

echo "== 安装应用菜单入口 =="
# Qbar 启动器 + 托盘：都装成"应用菜单版"（能在 GNOME 应用网格 / Dock 里搜到、固定住）。
# 每项是「仓内模板文件:安装后的文件名:要绑的 bin 名」；Exec/Icon 按本机重写，
# 免得模板里写死 /home/zmj 的绝对路径，换个用户就跑不起来。
mkdir -p "$APPS_DIR"
# 只装 Qbar 一个入口：托盘本来就开机自启，应用菜单里再放一条是冗余
# （2026-10-15 明江反馈"应用里面有两个"）
for spec in "packaging/capsplus-qbar.desktop:capsplus-qbar.desktop:capsplus-qbar"; do
  src="$REPO/${spec%%:*}"
  rest="${spec#*:}"
  dst="$APPS_DIR/${rest%%:*}"
  bin="${rest#*:}"
  sed -e "s#^Exec=.*#Exec=$BIN_DIR/$bin#" \
      -e "s#^Icon=.*#Icon=$REPO/assets/capsplus-on.svg#" \
      "$src" > "$dst"
  echo "  $dst"
done
if command -v desktop-file-validate >/dev/null 2>&1; then
  if desktop-file-validate "$APPS_DIR/capsplus-qbar.desktop"; then
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
  sed -e "s#^Exec=.*#Exec=$BIN_DIR/capsplus-tray#" \
      -e "s#^Icon=.*#Icon=$REPO/assets/capsplus-on.svg#" \
      "$REPO/packaging/capsplus-tray.desktop" > "$AUTOSTART_DIR/capsplus-tray.desktop"
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
  4) 托盘常驻:        capsplus-tray   （或 systemctl --user enable --now capsplus-tray）
  5) Qbar:            capsplus-qbar   （keyd 侧用 bin/keyd-qbar-cmd 包装）
  6) 快速链接配置:    ${HOME}/.config/capsplus/qbar.toml
     （首次打开 Qbar 时自动生成；托盘右键「编辑快速链接」也能开）
  7) 设置窗口:        capsplus-settings
     三页：快速链接 / 通用 / 按键映射；改完按键要点「应用」才生效，出错自动回滚。
     入口只有三个：托盘「设置…」、Qbar 里输 -> settings、命令行 capsplus-settings。
     刻意没装 .desktop —— 应用菜单里条目越多越难找（上一轮刚删掉一条）。
EOF
