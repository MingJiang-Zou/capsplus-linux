# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""语音输入的协议层：worker socket 路径、帧格式、超时、陈旧 socket 清理。

为什么单独一层（2026-10-17）：按键脚本（`bin/capsplus-voice`，dash）、服务端
（`capsplus/voice_worker.py`）、界面（`capsplus/voice_osd.py`）三方都要说同一套话，
而**这一层不许 import `gi`** —— CI 的 ubuntu-latest 没有 PyGObject，测试模块在收集阶段
就得能把它 import 进来（判据：`tests/test_no_gi_smoke.py`）。

线格式（与 Qbar 的"第二次启动只发一个字节"同源思路，但这里要回话，所以是**换行分隔 JSON**）：

    命令（客户端 → 服务端）：{"cmd":"start"} / stop / status / ping
    事件（服务端 → 客户端）：{"kind":"state",...} / {"kind":"text",...} / {"kind":"error",...}

四条硬规矩：
  1. 一帧一行，行尾 `\\n`；读侧遇到坏帧**归成 error 事件**，绝不抛异常把服务端带走
     （照 `capsplus/extcheck.py` 的口径：任何异常都归成状态）。
  2. 一切超时都写在这里（连接 0.6s / 命令 1.5s），别在各个调用方各写一个数。
  3. 连不上但 socket 文件还在 = **陈旧 socket**， unlink 掉再自己起（照 `qbar_app.py::try_toggle_existing`）。
  4. socket 权限 0600：Qbar 那个只收一个字节，这个要收**用户说的话**，不能给同机别人连。
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

PROTOCOL_VERSION = 1

# 命令与事件的**全集**：加一条就要改这里，别在调用方写裸字符串
COMMANDS = ("start", "stop", "status", "ping")
EVENT_KINDS = ("state", "text", "error")
STATES = ("idle", "recording", "decoding", "review")

CONNECT_TIMEOUT_S = 0.6       # 本机 socket 连接：同机 unix socket，连不上就是没进程
CMD_TIMEOUT_S = 1.5           # 发完命令等第一帧回执
EVENT_TIMEOUT_S = 3.0         # --watch 式连续读事件（人用，按键路径不走这条）
MAX_FRAME_BYTES = 1 << 20     # 1 MiB 封顶：防一条不闭合的帧把内存吃掉
LISTEN_BACKLOG = 4
SOCK_MODE = 0o600


def state_dir() -> Path:
    """状态目录：`CAPSPLUS_STATE_DIR` 覆盖，否则 `~/.local/state/capsplus`。

    与 `qbar_app.STATE_DIR` / `tray_app.STATE_DIR` 同一个目录，但**家目录一律现算**，
    仓里不许出现任何具体用户的绝对路径（判据：`tests/test_portability.py`）。
    这个口子也是测试的注入点：单测把状态目录指到 tmp，绝不碰真用户目录。
    """
    override = os.environ.get("CAPSPLUS_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "capsplus"


def socket_path() -> Path:
    """worker 的 socket 路径（`CAPSPLUS_VOICE_SOCK` 可整条覆盖，测试用）。"""
    override = os.environ.get("CAPSPLUS_VOICE_SOCK")
    if override:
        return Path(override).expanduser()
    return state_dir() / "voice.sock"


def log_path() -> Path:
    return state_dir() / "voice.log"


# ── 帧的构造与解析 ───────────────────────────────────────────────────────────
def make_command(cmd: str, **fields) -> dict:
    """造一条命令。命令名不在白名单里 = 编程错误，当场抛（这种错不该上线上）。"""
    if cmd not in COMMANDS:
        raise ValueError(f"未知命令 {cmd!r}，只认 {' / '.join(COMMANDS)}")
    frame = {"cmd": cmd, "proto": PROTOCOL_VERSION}
    frame.update(fields)
    return frame


def make_event(kind: str, **fields) -> dict:
    """造一个事件帧。`kind` 不在白名单里同样当场抛。"""
    if kind not in EVENT_KINDS:
        raise ValueError(f"未知事件 {kind!r}，只认 {' / '.join(EVENT_KINDS)}")
    frame = {"kind": kind, "proto": PROTOCOL_VERSION}
    frame.update(fields)
    return frame


def state_event(state: str, **fields) -> dict:
    if state not in STATES:
        raise ValueError(f"未知状态 {state!r}，只认 {' / '.join(STATES)}")
    return make_event("state", state=state, **fields)


def error_event(code: str, message: str = "", hint: str = "") -> dict:
    return make_event("error", code=code, message=message, hint=hint)


def encode(frame: dict) -> bytes:
    """帧 → 一行字节。中文不转义（`ensure_ascii=False`），但绝不允许出现裸换行。"""
    # json.dumps 会把值里的换行转义成 \n 两个字面字符 ⇒ 产出一定是单行，行协议不会破。
    line = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    data = line.encode("utf-8") + b"\n"
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError(f"帧太大（{len(data)} > {MAX_FRAME_BYTES}），拒发")
    return data


def parse_frame(text: str) -> dict:
    """把一行文本解析成帧。**任何坏输入都归成 error 帧，绝不抛**。

    为什么要这么硬：服务端 accept 循环里抛异常，整个语音功能就没了，而用户只看到
    "按了键没反应" —— 和 `capsplus/extcheck.py` 的体检同理，状态必须能报告而不是崩。
    """
    line = (text or "").strip()
    if not line:
        return error_event("empty_frame", "空行")
    if len(line.encode("utf-8", "replace")) > MAX_FRAME_BYTES:
        return error_event("frame_too_large", f"超过 {MAX_FRAME_BYTES} 字节")
    try:
        obj = json.loads(line)
    except ValueError as exc:
        return error_event("bad_json", f"{type(exc).__name__}: {exc}")
    if not isinstance(obj, dict):
        return error_event("not_object", f"顶层是 {type(obj).__name__}")
    if "cmd" in obj:
        if obj["cmd"] not in COMMANDS:
            return error_event("bad_command", f"未知命令 {obj['cmd']!r}")
        return obj
    kind = obj.get("kind")
    if kind not in EVENT_KINDS:
        return error_event("bad_kind", f"既不是命令也不是已知事件（kind={kind!r}）")
    return obj


def read_frames(sock, limit: int = 8, timeout: float = EVENT_TIMEOUT_S) -> list[dict]:
    """从已连接的 socket 读若干帧（对端关闭或超时都正常收尾）。不抛异常。"""
    frames: list[dict] = []
    buf = b""
    sock.settimeout(timeout)
    try:
        while len(frames) < limit:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                frames.append(error_event("read_timeout", f"{timeout}s 内没等到帧"))
                break
            except OSError as exc:
                frames.append(error_event("read_failed", str(exc)))
                break
            if not chunk:
                break                       # 对端正常关闭
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                frames.append(parse_frame(line.decode("utf-8", "replace")))
                if len(frames) >= limit:
                    break
    finally:
        try:
            sock.settimeout(None)
        except OSError:
            pass
    return frames


# ── 连接侧 ───────────────────────────────────────────────────────────────────
def connect(path=None, timeout: float = CONNECT_TIMEOUT_S) -> socket.socket:
    """连 worker。连不上抛 OSError —— 调用方用 `ping()` / `request()`，那两个不抛。"""
    target = Path(path) if path else socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(target))
    except OSError:
        sock.close()
        raise
    return sock


def ping(path=None) -> tuple[bool, str]:
    """worker 活着吗。**不抛异常**，并且顺手清理陈旧 socket。

    返回 (alive, 一句话原因)。三种情况都要说清：
      True  "worker 在跑（pid=…）"
      False "没在跑"（socket 文件本来就不存在）
      False "陈旧 socket 已清理，将自起 worker"（文件在但连不上 = 上个进程没来得及收尾）
    """
    target = Path(path) if path else socket_path()
    if not target.exists():
        return False, "worker 没在跑（没有 socket 文件）"
    try:
        sock = connect(target)
    except OSError as exc:
        removed = remove_stale(target)
        why = f"{type(exc).__name__}: {exc}"
        if removed:
            return False, f"socket 文件在但连不上（{why}）→ 陈旧 socket 已清理"
        return False, f"socket 文件在但连不上（{why}）"
    try:
        with sock:
            sock.sendall(encode(make_command("ping")))
            frames = read_frames(sock, limit=1, timeout=CMD_TIMEOUT_S)
    except OSError as exc:
        return True, f"worker 在跑但发不出 ping（{type(exc).__name__}: {exc}）"
    for frame in frames:
        if frame.get("kind") == "state":
            return True, f"worker 在跑（state={frame.get('state', '?')}）"
        if frame.get("kind") == "error":
            return True, f"worker 在跑但答错了：{frame.get('code')} {frame.get('message', '')}"
    return True, "worker 在跑（连上了，但没等到应答帧）"


def remove_stale(path=None) -> bool:
    """删掉没人应答的 socket 文件（`missing_ok`，不碰真在用的那个）。"""
    target = Path(path) if path else socket_path()
    try:
        if target.exists() and not _listener_alive(target):
            target.unlink()
            return True
    except OSError:
        return False
    return False


def _listener_alive(path: Path) -> bool:
    try:
        connect(path, timeout=0.3).close()
        return True
    except OSError:
        return False


def bind_server(path=None, backlog: int = LISTEN_BACKLOG) -> socket.socket:
    """起服务端 socket：先把同名陈旧文件 unlink 再 bind（照 Qbar `serve_socket` 的做法）。

    调用方负责在进程退出时 `unlink`；信号处理器也要装（`voice_worker` 里做了），
    否则留下一个连不上的 socket 文件，下次启动就得多绕一圈。
    """
    target = Path(path) if path else socket_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _listener_alive(target):
            raise OSError(f"已有 worker 在监听 {target}（单实例）")
        target.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(target))
    try:
        os.chmod(str(target), SOCK_MODE)
    except OSError:
        pass
    server.listen(backlog)
    server.settimeout(0.5)
    return server


def release_server(server: socket.socket, path=None) -> None:
    """关监听 + 删文件（Ctrl+C / SIGTERM 也要走这条，别留陈旧 socket）。"""
    target = Path(path) if path else socket_path()
    try:
        server.close()
    except OSError:
        pass
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def request(cmd: str, path=None, extra: dict | None = None,
            timeout: float = CMD_TIMEOUT_S) -> list[dict]:
    """发一条命令，拿回帧列表。**任何失败都归成 error 帧**，调用方只看帧不看异常。"""
    target = Path(path) if path else socket_path()
    try:
        frame = make_command(cmd, **(extra or {}))
    except ValueError as exc:
        return [error_event("bad_command", str(exc))]
    try:
        sock = connect(target)
    except OSError as exc:
        return [error_event("unreachable",
                            f"{type(exc).__name__}: {exc}",
                            hint="worker 没在跑：`capsplus-voice start` 会自己把它带起来")]
    with sock:
        try:
            sock.sendall(encode(frame))
        except OSError as exc:
            return [error_event("send_failed", str(exc))]
        return read_frames(sock, limit=1, timeout=timeout)


def toggle_plan(state: str | None) -> str:
    """第二次按 Caps+M 该发哪条命令（"切换"的语义在这里定死，别散到 sh 和 worker 两头）。

    idle / 读不到状态 / 未知状态 → `start`（起一次录音）；
    recording / decoding / review → `stop`（录音中就收尾送去识别，识别中就取消，
    弹窗开着就丢弃关闭）。照 `qbar/logic.py::toggle_action` 的写法：纯函数、单测可锁。
    """
    return "stop" if state in ("recording", "decoding", "review") else "start"


def current_state(path=None) -> str | None:
    """问一次当前状态；worker 不在或答不上来都返回 None（调用方按"没在跑"处理）。"""
    for frame in request("status", path=path):
        if frame.get("kind") == "state":
            return frame.get("state")
    return None


def log_line(message: str, keep: int = 200) -> None:
    """诊断日志：只留最近 `keep` 行，**绝不写识别出来的文本**（那是用户说的话）。

    与 `qbar_app.log()` 同一套写法（写失败就静默，日志不能把功能带崩）。
    """
    import datetime

    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        path.write_text("\n".join((old + [f"{stamp} {message}"])[-keep:]) + "\n",
                        encoding="utf-8")
    except OSError:
        pass
