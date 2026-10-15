#!/usr/bin/env bash
# capsplus-linux 安装脚本：只装用户侧（bin 软链、autostart），并检查依赖。
# 刻意**不碰** /etc/keyd —— 那份配置由你手工跑 `capsplus apply` 并审 diff。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
AUTOSTART_DIR="${HOME}/.config/autostart"
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
check_cmd python3
check_cmd fdfind
check_cmd keyd.rvaiya
check_cmd runuser

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

echo "== 安装 bin 软链 =="
mkdir -p "$BIN_DIR"
for name in capsplus capsplus-tray capsplus-qbar keyd-qbar-cmd; do
  ln -sfn "$REPO/bin/$name" "$BIN_DIR/$name"
  echo "  $BIN_DIR/$name -> $REPO/bin/$name"
done

if [ "$WITH_AUTOSTART" = 1 ]; then
  echo "== 安装 autostart =="
  mkdir -p "$AUTOSTART_DIR"
  sed -e "s#^Exec=.*#Exec=$BIN_DIR/capsplus-tray#" \
      -e "s#^Icon=.*#Icon=$REPO/assets/capsplus-on.svg#" \
      "$REPO/packaging/capsplus-tray.desktop" > "$AUTOSTART_DIR/capsplus-tray.desktop"
  echo "  $AUTOSTART_DIR/capsplus-tray.desktop"
  echo "  （不想开机自启就删掉它，或下次加 --no-autostart）"
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
EOF
