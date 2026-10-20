#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
# raise-browser 扩展的**无重登自检**：起一个隔离的 headless gnome-shell，看它有没有真的把扩展
# 加载起来、抓帧/贴图通道有没有挂上。
#
# 为什么要有它（2026-10-20）：GNOME 只在启动时扫描扩展目录（`ReloadExtension` 上游已废弃，
# 实测返回 deprecated and does not work；Wayland 也没有 Alt+F2 r）⇒ 改一行 extension.js 就要
# 注销重登。更坏的是**扩展坏了不报错**：现象只是 Caps+F1 没反应，于是"重登"从最后一步变成了
# 排查手段。明江原话：「老是要重登 感觉也不好吧」。现在：改完先跑这个（1~3 秒，不碰他正在用的
# 会话），绿了再去重登 —— 重登只用于把新代码送进他自己的 shell。
#
# 输出约定（别改，脚本和人都靠它）：干净的一行结论打 **stdout**
#   成功 → `EXT_OK（<秒>s）：…`，退出码 0
#   失败 → 可操作原因（含日志路径）打 stdout，退出码 1
#   环境不齐（没有 gnome-shell / dbus-run-session / python3）→ 退出码 2
# 那些噪音（headless shell 顺手激活的 nautilus / tracker / portal 在临时总线上吵的东西）
# 一律进 `$LOG.noise`，不糊满调用者的终端。
#
# 三条硬边界（都踩过，别自己发明）：
#   ① 判活只认日志，不认 gdbus：本机 gjs 的 register_object 是原始 C 绑定，扩展导出不了对象，
#      隔离会话里 `gdbus … --method Probe` 报 UnknownMethod「对象在路径处不存在」（2026-10-20 实测）。
#   ② 只 kill 自己起的那个 pid（`$!`）。**绝不许** `pkill -f gnome-shell` —— 那会杀明江的会话。
#   ③ 判词不在这里写第二遍：解析走 `python3 -m capsplus.extcheck --bench-log`（那边能单测：
#      把日志喂给 bench_verdict 就行，不用真起 shell）。
#
# 依赖：gnome-shell（本机图形栈）+ dbus-run-session + python3。CI 上没有图形栈，所以 CI 只做
# `sh -n` 与判词单测，不真跑（真跑了也只会得到"没起来"的红）。
#
# 用法：
#   sh tools/ext-bench.sh                  # 自检**部署副本**（shell 真正会加载的那一份）
#   sh tools/ext-bench.sh --timeout 40     # 慢机器上放宽等待（默认 25 秒）
#   sh tools/ext-bench.sh --keep-log       # 成功也保留日志（失败时永远保留）
#   sh tools/ext-bench.sh --log /tmp/x.log # 自己指定日志路径（给反例举证用）
#   sh tools/ext-bench.sh --quiet          # 成功时不打提示（只认退出码，给脚本/CI 用）
set -eu

READY_MARK='[raise-browser] ready'
TIMEOUT=25
KEEP_LOG=0
QUIET=0
LOG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --timeout)
            [ $# -ge 2 ] || { echo "✗ --timeout 要跟秒数" >&2; exit 2; }
            TIMEOUT="$2"
            shift 2
            ;;
        --timeout=*) TIMEOUT="${1#--timeout=}"; shift ;;
        --keep-log) KEEP_LOG=1; shift ;;
        --quiet) QUIET=1; shift ;;
        --log)
            [ $# -ge 2 ] || { echo "✗ --log 要跟路径" >&2; exit 2; }
            LOG="$2"
            shift 2
            ;;
        --log=*) LOG="${1#--log=}"; shift ;;
        -h|--help)
            sed -n 's/^# \{0,1\}//p' "$0" | sed -n '/^用法：/,/^ *sh tools\/ext-bench.sh --quiet/p'
            exit 0
            ;;
        *) echo "✗ 不认识的参数：$1（--timeout N / --keep-log / --log FILE / --quiet）" >&2; exit 2 ;;
    esac
done
case "$TIMEOUT" in
    ''|*[!0-9]*) echo "✗ --timeout 要正整数秒，实际：$TIMEOUT" >&2; exit 2 ;;
esac
say() { [ "$QUIET" = 1 ] || printf '%s\n' "$1" >&3; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 外层：把自己塞进一个**独立会话总线**再跑一遍（内层）。这样 headless shell 顺手激活的那些
# 服务（nautilus / tracker / portal / evolution …）全挂在临时总线上，总线一关就一起收尸，
# **不会**注册到明江真实的会话总线上去。
# 参数改走环境变量传（POSIX sh 没有数组，别为了"好写"退回 bash）。
# 会话噪音统一塞进 `$LOG.noise`：dbus-daemon 与它激活的那些服务继承的正是这一路 stderr，
# 不重定向的话调用者终端会被几十行 nautilus/portal 警告糊满（2026-10-20 第一次跑就是这样）。
if [ "${CAPSPLUS_BENCH_INNER:-0}" != "1" ]; then
    if ! command -v dbus-run-session >/dev/null 2>&1; then
        echo "✗ 缺 dbus-run-session（包 dbus-daemon？）：没有它就隔离不了，不敢在真会话里起 shell" >&2
        exit 2
    fi
    if [ -z "$LOG" ]; then
        LOG="/tmp/capsplus-ext-bench.$$.log"
    fi
    NOISE="$LOG.noise"
    : > "$NOISE"
    # 重定向顺序有讲究：`3>&1` 必须排在 `>"$NOISE"` **前面**，否则 fd 3 会指到噪音文件上去。
    # fd 3 就是"调用者的 stdout"，内层用它说话；fd 1/2 全部进噪音文件。
    exec dbus-run-session -- env \
        CAPSPLUS_BENCH_INNER=1 \
        CAPSPLUS_BENCH_TIMEOUT="$TIMEOUT" \
        CAPSPLUS_BENCH_KEEP="$KEEP_LOG" \
        CAPSPLUS_BENCH_QUIET="$QUIET" \
        CAPSPLUS_BENCH_LOG="$LOG" \
        sh "$0" 3>&1 >"$NOISE" 2>>"$NOISE"
fi

# 内层：参数从环境里取（外层已经把参数解析完了）
TIMEOUT="${CAPSPLUS_BENCH_TIMEOUT:-$TIMEOUT}"
KEEP_LOG="${CAPSPLUS_BENCH_KEEP:-$KEEP_LOG}"
QUIET="${CAPSPLUS_BENCH_QUIET:-$QUIET}"
LOG="${CAPSPLUS_BENCH_LOG:-$LOG}"
[ -n "$LOG" ] || { echo "✗ 内层没拿到日志路径" >&2; exit 2; }
NOISE="$LOG.noise"

# 说话走 fd 3（外层接的是调用者 stdout）。手上没有 fd 3 时（有人手工 CAPSPLUS_BENCH_INNER=1 跑）
# 退回 stdout —— printf >&3 打到关闭的 fd 会 "Bad file descriptor"，那反而把自检自己搞挂。
if [ ! -e /proc/self/fd/3 ]; then
    exec 3>&1
fi

if ! command -v gnome-shell >/dev/null 2>&1; then
    echo "✗ 没有 gnome-shell：这台机跑不了自检（CI / 纯 SSH 上就跳过这一步）" >&3
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ 没有 python3：判词入口是 python3 -m capsplus.extcheck" >&3
    exit 2
fi

# XDG_RUNTIME_DIR 是 mutter 起 Wayland 后端的硬前置。它抢不到 wayland-0 的 lock 时会自己退到
# wayland-1，**不会**动到明江正在用的那个 compositor（2026-10-20 实测：日志里就是
# "unable to lock lockfile …/wayland-0.lock … Using Wayland display name 'wayland-1'"）。
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR

started=$(date +%s)
: > "$LOG"                       # 截断而不删：日志路径是调用方给的，别替人删文件
gnome-shell --headless >"$LOG" 2>&1 &
shell_pid=$!

# 等 ready 行（250ms 一步）；shell 自己先退了也别空等到超时
limit=$(( TIMEOUT * 4 ))
i=0
while [ "$i" -lt "$limit" ]; do
    grep -qF "$READY_MARK" "$LOG" 2>/dev/null && break
    kill -0 "$shell_pid" 2>/dev/null || break
    i=$(( i + 1 ))
    sleep 0.25
done

# 收尸：先 TERM 再 KILL，只认自己那个 pid。headless shell 收到 TERM 会走正常退出（日志里留
# "Shutting down GNOME Shell"），一般很快；等不到才升级到 KILL。
kill -TERM "$shell_pid" 2>/dev/null || true
j=0
while kill -0 "$shell_pid" 2>/dev/null && [ "$j" -lt 50 ]; do
    sleep 0.1
    j=$(( j + 1 ))
done
kill -KILL "$shell_pid" 2>/dev/null || true
wait "$shell_pid" 2>/dev/null || true

elapsed=$(( $(date +%s) - started ))

# 判词只在 Python 那一处（sh 里再写一遍 grep 就是第二份真相）。
# `2>&1`：python 把原因打 stderr（那边是惯例），这里要送到 stdout 那一路上给调用者看。
rc=0
PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m capsplus.extcheck --bench-log "$LOG" 2>&3 || rc=$?

if [ "$rc" != 0 ]; then
    # 失败：日志一定留着，并且把路径说出来（否则"红在哪"又得靠人猜）
    printf '日志：%s\n' "$LOG" >&3
    printf '会话噪音：%s\n' "$NOISE" >&3
    exit "$rc"
fi

if [ "$KEEP_LOG" = 1 ]; then
    printf '日志：%s\n' "$LOG" >&3
else
    # 成功就把噪音清掉（/tmp 是 tmpfs，重启即清；这里只是别让它堆着）
    : > "$LOG"
    : > "$NOISE"
fi
say "EXT_OK（${elapsed}s）：部署副本在 headless shell 里加载成功，抓帧/贴图通道都挂上了"
say "  ⇒ 文件层面没问题；要在明江自己的会话里生效，最后一步**仍是注销重登**"
