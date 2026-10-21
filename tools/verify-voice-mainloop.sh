#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
#
# 进程级验收判据（2026-10-17，真机事故）：用户按 Caps+M 后 GNOME 弹「voice_worker.py 无响应」——
# 取证发现**主线程停在 hrtimer_nanosleep**（time.sleep 轮询），GLib 循环跑在别的线程上，
# mutter 对客户端 ping 应答不稳。这条脚本不测功能，只测一件事：
#
#     起一个 headless 的 `voice_worker.py serve`，等 3 秒，看它**主线程**（lwp == pid）睡在哪。
#     红线：wchan == hrtimer_nanosleep 或 /proc/<pid>/syscall 首位 == 230（clock_nanosleep）⇒ 红。
#     绿线：应当停在 poll/epoll 一类**事件等待**上（poll_schedule_timeout / 7 / 232 都算对）。
#
# 用法：sh tools/verify-voice-mainloop.sh    退出码：0=绿 / 1=红（抓到病）/ 2=worker 没起来
# 修前必须能跑出红（这条判据证伪过一次才算数）；反例具型 = 修复前的 capsplus/voice_worker.py。
#
# 安全边界（三条，都写死在下面的 env 里）：
#   ① `CAPSPLUS_STATE_DIR` 指到 mktemp 的临时目录 —— **绝不碰真状态目录** ~/.local/state/capsplus；
#   ② 摘掉 DISPLAY / WAYLAND_DISPLAY / XDG_RUNTIME_DIR —— 本机 XDG_RUNTIME_DIR 里有 wayland-0，
#      `ui_available()` 会认它；全摘了才保证这个 worker 一辈子连不上真显示服务（它也不该连）；
#   ③ `CAPSPLUS_VOICE_UI=0` 双保险：就算上面漏了什么，review 也只会走"直接上屏"降级，不弹窗。
set -eu

SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"
REPO_DIR="$(dirname "$(dirname "$SELF")")"
PYTHON="${CAPSPLUS_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    PYTHON="$(command -v python3 2>/dev/null || true)"
    [ -n "$PYTHON" ] || PYTHON=/usr/bin/python3
fi

STATE="$(mktemp -d "${TMPDIR:-/tmp}/capsplus-mainloop-XXXXXX")"
LOG="$STATE/verify.log"
PID=""

cleanup() {
    # 只杀**自己记下的那个 pid**。绝不用 `pkill -f voice_worker`：模式串就在本脚本自己的
    # 命令行里，会把自己杀掉（本仓 2026-10-16 栽过第三次，见 echo-dispatch-coder 硬规矩）。
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        kill -TERM "$PID" 2>/dev/null || true
        i=0
        while kill -0 "$PID" 2>/dev/null && [ "$i" -lt 50 ]; do
            sleep 0.1
            i=$((i + 1))
        done
        kill -KILL "$PID" 2>/dev/null || true
    fi
    # 红线：删除一律进焚化炉不许 rm。这里 mv 进当日的 /tmp 焚化目录
    # （tmpfs、开机即清 + 10 天龄双兜底；内容只是 socket 和几 KB 日志，都是可弃物）。
    ATTIC="/tmp/attic-$(date +%Y%m%d)"
    mkdir -p "$ATTIC" 2>/dev/null || true
    mv "$STATE" "$ATTIC/" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

env -u DISPLAY -u WAYLAND_DISPLAY -u XDG_RUNTIME_DIR -u GDK_BACKEND \
    CAPSPLUS_STATE_DIR="$STATE" CAPSPLUS_VOICE_UI=0 \
    "$PYTHON" "$REPO_DIR/capsplus/voice_worker.py" serve >"$LOG" 2>&1 &
PID=$!

# 等"监听"那行出现（最多 5s）；worker 提前死了就直接红给看日志
i=0
while [ "$i" -lt 50 ]; do
    if grep -q "\[voice-worker\]" "$LOG" 2>/dev/null; then break; fi
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "== 红（worker 没起来）=="; cat "$LOG"; exit 2
    fi
    sleep 0.1
    i=$((i + 1))
done
if ! grep -q "\[voice-worker\]" "$LOG" 2>/dev/null; then
    echo "== 红（5s 内没开始监听）=="; cat "$LOG"; exit 2
fi

# 3 个采样点、每秒一次：主线程的 wchan 与 /proc/<pid>/syscall 首位。
# （这里 sleep 的是**本脚本**，不是被测进程 —— 判据要的就是被测方主线程不睡。）
sleep 3
RED=0
i=1
while [ "$i" -le 3 ]; do
    WCHAN="$(ps -L -o lwp=,wchan= -p "$PID" | awk -v p="$PID" '$1==p {print $2}' || true)"
    SYSC="$(cut -d' ' -f1 "/proc/$PID/syscall" 2>/dev/null || true)"
    echo "采样 $i：主线程 lwp=$PID wchan=[${WCHAN:-（在跑/取不到）}] syscall#=[${SYSC:-?}]"
    case "${WCHAN:-}" in
        *hrtimer_nanosleep*) RED=1 ;;
    esac
    if [ "${SYSC:-}" = "230" ]; then RED=1; fi
    i=$((i + 1))
    [ "$i" -le 3 ] && sleep 1
done

echo "== ps -L 全线程现场 =="
ps -L -o lwp,stat,wchan -p "$PID" 2>/dev/null || true

if [ "$RED" -eq 1 ]; then
    echo "== 红：主线程停在定时器睡眠（hrtimer_nanosleep / clock_nanosleep=230）=="
    echo "   ⇒ GLib 事件循环不在进程主线程上 —— mutter 的 ping 没人及时应答，"
    echo "     真桌面上就是那个「voice_worker.py 无响应」弹窗的病根。"
    exit 1
fi
echo "== 绿：主线程停在事件等待上（不是 hrtimer_nanosleep）=="
exit 0
