# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""守护式命令（`wl-copy`）不许用「等管道 EOF」判成败：真子进程具型 + 回读自证 + 隐私全链。

要还的债（2026-10-19）：明江真机 09:15 识别出 6 个字，焦点事实也拿到了，结果却是
`inject_done backend=clipboard ok=0 code=step_failed steps=0`，而 55 减 50 恰好等于
`STEP_TIMEOUT_S = 5.0` —— **上屏这个功能从落地起一次都没真贴上过字**。

根因（**源码实证**，不是猜的）：`wl-copy` 2.2.1 `src/wl-copy.c` 的 `did_set_selection_callback`：

    int devnull = open("/dev/null", O_RDWR);
    dup2(devnull, STDOUT_FILENO);      /* 只换了 stdout */
    dup2(devnull, STDIN_FILENO);
    close(devnull);
    pid_t pid = fork();                /* 子进程留守服务选区 */
    if (pid > 0) exit(0);              /* 父进程立刻退 0 */

注释里那句 "so the stdout file descriptor isn't kept alive" 说明它**只考虑了 stdout**：
**stderr 原样被子进程继承**。于是 `subprocess.run(capture_output=True)`（stdout 与 stderr 都接管道）
等不到 stderr 的 EOF ⇒ `TimeoutExpired` ⇒ 第一步"写剪贴板"被判失败 ⇒ Ctrl+V 从未执行。
会 fork 常驻的命令，永远不许用"等管道 EOF"的方式判成败。

本文件的判据分三组，每组都能红（反例具型在各自 docstring 里）：

  A. **真子进程具型（`TestTheRealWlCopyShape`）**：夹具按上面那段 C 源码的形状 fork
     （stdout 换 /dev/null、**stderr 留着**），先用老写法 `capture_output=True` 证明它会超时，
     再证明**只重定向 stdout 不够、唯一握着管道的是 stderr** —— 判据自己先咬自己一次。
  B. **执行器（`TestDaemonStepIsWaitedForByParentExitOnly` / `TestDaemonStepTempFileHygiene`）**：
     守护步只等**父进程退出**（毫秒级）、错误详情只读**字节数**、临时文件 0600 且**用完即毁**
     （成功 / 失败 / 超时三条路都不许留）；普通步（ydotool / wl-paste）**照旧收 stderr**
     —— 一刀切会再造一个"读不到错误"的瞎子。
  C. **自证与隐私（`TestWriteSelfProof`）**：rc=0 不算成功，`wl-paste` **逐字节回读**才算；
     回读不是我们的文本 ⇒ `owner_rejected` 且**粘贴那步不发**；剪贴板字节（含"留守进程"写进
     stderr 的那份）**一个字节都不许**出现在日志 / 事件帧 / 诊断行 / 摘要 / 状态目录里。

全套**不碰明江的屏幕、不碰他的剪贴板**：假 `wl-copy`/`wl-paste`/`ydotool` 三个可执行脚本
住在本次 `mktemp` 沙箱里，剪贴板状态就是沙箱里的一个 JSON 文件；用的却是**真子进程、真管道、
真 fork**（假 runner 在这条判定上什么都证明不了）。状态目录由模块级护栏钉进沙箱 ——
`run_step` 的守护步会往状态目录写临时文件，护栏忘了就是往明江的真目录里写（同一教训见
`tests/test_voice_clipboard.py` 的 `TestSandboxGuard`）。
"""

import atexit
import base64
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_inject as INJ                 # noqa: E402
from capsplus import voice_proto as P                    # noqa: E402
from capsplus import voice_worker as W                   # noqa: E402

OUR_TEXT = "九点在三号楼开会"
CARD = "6222021234567890123"          # 躺在剪贴板里的卡号：隐私判据的探针
FAKE_CODES = {"KEY_LEFTCTRL": 29, "KEY_V": 47}

# ── 沙箱：本模块任何用例都不许碰明江的真状态目录 / 真剪贴板 ──────────────────────
SANDBOX = Path(tempfile.gettempdir()) / f"capsplus-daemon-copy-{os.getpid()}"
FIXTURE = SANDBOX / "bin"
CLIP_DIR = SANDBOX / "clipboard"
STATE_DIR = SANDBOX / "state"
_ORIG_STATE_DIR = os.environ.get("CAPSPLUS_STATE_DIR")


def _cleanup_sandbox():
    shutil.rmtree(SANDBOX, ignore_errors=True)


atexit.register(_cleanup_sandbox)


def _sandbox(name: str) -> Path:
    path = SANDBOX / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def setUpModule():
    # 护栏必须是**默认值**：`run_step` 的守护步往状态目录写临时错误文件，
    # 忘了钉住就是往明江的 `~/.local/state/capsplus/` 里写（test_voice_clipboard 真栽过一次）。
    os.environ["CAPSPLUS_STATE_DIR"] = str(_sandbox("module"))


def tearDownModule():
    _cleanup_sandbox()
    if _ORIG_STATE_DIR is None:
        os.environ.pop("CAPSPLUS_STATE_DIR", None)
    else:
        os.environ["CAPSPLUS_STATE_DIR"] = _ORIG_STATE_DIR


# ── 假的三个可执行件：形状抄自真 wl-copy 的 C 源码（见模块 docstring）─────────────
_WL_COPY = '''#!/usr/bin/env python3
"""假的 wl-copy：形状 = 读干 stdin → 落盘 → stdout 换 /dev/null、stderr 留着 → fork → 父退 0。

`--clear` 走真件那条**提前 exit(0)（不 fork）**的路；`FAKE_NO_FORK=1` 等价 `-f/--foreground`。
"""
import base64, json, os, sys, time

D = os.environ["FAKE_CLIP_DIR"]
argv = sys.argv[1:]
with open(os.path.join(D, "calls.log"), "a") as fh:
    fh.write("wl-copy " + " ".join(argv) + "\\n")
store_path = os.path.join(D, "prim.json" if "--primary" in argv else "clip.json")


def save(offers):
    with open(store_path, "w") as fh:
        fh.write(json.dumps({"types": {m: base64.b64encode(d).decode() for m, d in offers.items()}}))


if os.environ.get("FAKE_PARENT_SLEEP"):
    time.sleep(float(os.environ["FAKE_PARENT_SLEEP"]))

if "--clear" in argv:
    save({})
    sys.exit(0)

data = sys.stdin.buffer.read()
if os.environ.get("FAKE_COPY_STORE", "1") != "0":          # =0 模拟"所有权根本没接走"
    if os.environ.get("FAKE_STORE_OTHER"):
        data = os.environ["FAKE_STORE_OTHER"].encode("utf-8")
    if "--type" in argv:
        offers = {argv[argv.index("--type") + 1]: data}
    else:
        try:
            data.decode("utf-8")
            offers = {"text/plain;charset=utf-8": data, "text/plain": data}
        except UnicodeDecodeError:
            offers = {"application/octet-stream": data}
    save(offers)

rc = int(os.environ.get("FAKE_COPY_RC", "0"))
probe = os.environ.get("FAKE_STDERR_PROBE")
if rc != 0 and probe:
    # 父进程自己失败时先写 stderr 再退：真件的错误话就是从父进程出来的。
    # 这里是**确定性的**：字节一定先落到 stderr（守护步那会儿正是临时错误文件），
    # 所以"谁把这份文件的内容转述出去"的扰动一定会被判据抓到，不靠调度运气。
    # 位置必须在 FAKE_NO_FORK 的提前退出**之前** —— 否则前台模式那条路悄没声地把探针吞了，
    # 隐私判据就会假绿（本轮真踩过一次）。
    os.write(2, probe.encode("utf-8"))
    with open(os.path.join(D, "daemon-wrote.log"), "a") as fh:
        fh.write(probe + "\\n")

if os.environ.get("FAKE_NO_FORK") == "1":
    sys.exit(rc)

devnull = os.open(os.devnull, os.O_RDWR)                   # ← 真 wl-copy 就干到这儿：
os.dup2(devnull, 1)                                        #   只换 stdout，stderr 留着
os.close(devnull)
pid = os.fork()
if pid == 0:                                               # 留守者：握着继承来的 stderr
    with open(os.path.join(D, "daemons.pids"), "a") as fh:
        fh.write(str(os.getpid()) + "\\n")
    if probe:
        os.write(2, probe.encode("utf-8"))                  # 真件出错时就是这么写的
        with open(os.path.join(D, "daemon-wrote.log"), "a") as fh:
            fh.write(probe + "\\n")                         # 判据用它证明"探针真的上了场"
    time.sleep(float(os.environ.get("FAKE_DAEMON_LIFETIME", "8")))
    os._exit(0)
os._exit(rc)
'''

_WL_PASTE = '''#!/usr/bin/env python3
"""假的 wl-paste：`--list-types` / `--type M` 两种问句；文本除 `-n` 外补一个换行（同真件）。"""
import base64, json, os, sys

D = os.environ["FAKE_CLIP_DIR"]
argv = sys.argv[1:]
path = os.path.join(D, "prim.json" if "--primary" in argv else "clip.json")
types = json.load(open(path))["types"] if os.path.exists(path) else {}
with open(os.path.join(D, "calls.log"), "a") as fh:
    fh.write("wl-paste " + " ".join(argv) + "\\n")

if "--list-types" in argv:
    sys.stdout.write("\\n".join(types) + ("\\n" if types else ""))
    sys.exit(0)
if "--type" in argv:
    mime = argv[argv.index("--type") + 1]
    if mime in types:
        data = base64.b64decode(types[mime])
        if "--no-newline" not in argv:
            data += b"\\n"
        sys.stdout.buffer.write(data)
        sys.exit(0)
    sys.exit(1)
sys.exit(1)
'''

_YDOTOOL = '''#!/usr/bin/env python3
"""假的 ydotool：只记账，并可按环境变量失败（失败时要带 stderr 文本）。"""
import os, sys

with open(os.path.join(os.environ["FAKE_CLIP_DIR"], "calls.log"), "a") as fh:
    fh.write("ydotool " + " ".join(sys.argv[1:]) + "\\n")
text = os.environ.get("FAKE_YDOTOOL_STDERR")
if text:
    sys.stderr.write(text)
sys.exit(int(os.environ.get("FAKE_YDOTOOL_RC", "0")))
'''


def _install_fixtures() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in (("wl-copy", _WL_COPY), ("wl-paste", _WL_PASTE), ("ydotool", _YDOTOOL)):
        path = FIXTURE / name
        path.write_text(body, encoding="utf-8")
        path.chmod(stat.S_IRWXU)


_install_fixtures()

WL_COPY = str(FIXTURE / "wl-copy")
WL_PASTE = str(FIXTURE / "wl-paste")
YDOTOOL = str(FIXTURE / "ydotool")


def probe(**over) -> INJ.Probe:
    """一份「探测全通过、备份能力也齐」的假结论；`over` 用来逐项打破它。"""
    base = dict(wl_copy=WL_COPY, wl_copy_primary=True, wl_paste=WL_PASTE, wl_copy_clear=True,
                ydotool=YDOTOOL, ydotool_subcommands=("key", "click", "type"),
                ydotool_daemon=True, syntax=("keycode", "click-bitmask"),
                keycodes=dict(FAKE_CODES), keycodes_source="/fake/input-event-codes.h",
                ran=False)
    base.update(over)
    return INJ.Probe(**base)


@contextlib.contextmanager
def fixture_env(**over):
    """干净环境：状态目录钉进沙箱、三个假件都指向夹具；`FAKE_*` 旋钮按需覆盖。"""
    base = {k: v for k, v in os.environ.items()
            if not k.startswith("FAKE_") and k != "CAPSPLUS_STATE_DIR"}
    base.update({"FAKE_CLIP_DIR": str(CLIP_DIR), "CAPSPLUS_STATE_DIR": str(STATE_DIR)})
    base.update(over)
    with mock.patch.dict(os.environ, base, clear=True):
        yield


def reset_clipboard(**offers) -> None:
    """把假剪贴板重置成 `offers`（mime → bytes）；顺手清掉调用记录与上一轮的 pid。"""
    for name in ("calls.log", "daemons.pids", "daemon-wrote.log", "clip.json", "prim.json"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(CLIP_DIR / name)
    if offers:
        with open(CLIP_DIR / "clip.json", "w") as fh:
            fh.write(json.dumps({"types": {m: base64.b64encode(d).decode()
                                           for m, d in offers.items()}}))


def calls() -> list:
    path = CLIP_DIR / "calls.log"
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def clipboard_bytes(mime: str):
    path = CLIP_DIR / "clip.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text())["types"].get(mime)
    return base64.b64decode(blob) if blob else None


def paste_happened() -> bool:
    return any(c.startswith("ydotool ") for c in calls())


def daemon_wrote() -> str:
    path = CLIP_DIR / "daemon-wrote.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """等后台线程 / 子进程的状态落地。**只有测试侧在轮询**（被测代码里一处 sleep 都没有）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


def daemon_pids() -> list:
    """本轮所有留守者的 pid（夹具每 fork 一个就追加一行）。"""
    path = CLIP_DIR / "daemons.pids"
    if not path.exists():
        return []
    return [int(ln) for ln in path.read_text().split() if ln.strip().isdigit()]


def kill_fixture_daemons() -> None:
    """收掉本轮 fork 出来的留守者。

    **只扫 `/proc` 里 argv 含本轮沙箱路径的进程**（`SANDBOX` 里带着本次测试进程自己的 pid），
    所以碰不到并发的另一次测试运行，更碰不到明江的任何进程。
    为什么不用 `daemons.pids` 收尸：父进程退得比子进程被调度还早，那行登记可能**来不及写**，
    按文件收尸就会漏（本轮实测漏了两个）。
    """
    me = str(SANDBOX).encode("utf-8")
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue                            # 进程刚没了 / 不是我们能读的
        if me in cmdline and b"/bin/wl-copy" in cmdline:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(entry), 9)


def fresh_state_dir() -> Path:
    """给单条用例一个**空**的状态目录（守护步的临时错误文件就落在这种目录里）。"""
    return _sandbox(f"state-{time.monotonic_ns()}")


def spy_runner(seen: dict):
    """包一层真 `subprocess.run`：只在守护步那一路记下 `sink` 是谁。

    `run_step` 给的是 `os.fdopen(fd, "wb")` 出来的文件对象 —— 它的 `.name` 是**文件描述符号**
    而不是路径，所以这里从 `/proc/self/fd/N` 反查真路径（fd 在步内一直开着，查得到）。
    """
    def run(argv, **kw):
        sink = kw.get("stdout")
        if sink is not None and isinstance(getattr(sink, "name", None), int):
            path = os.readlink(f"/proc/self/fd/{sink.name}")
            seen["path"] = path
            seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        return subprocess.run(argv, **kw)
    return run


# ── A. 真子进程具型：老写法为什么必然超时 ───────────────────────────────────────
class TestTheRealWlCopyShape(unittest.TestCase):
    """判据自己先咬自己一次：夹具必须真的"父退了、留守者还握着管道"。"""

    def setUp(self):
        reset_clipboard()
        self.addCleanup(kill_fixture_daemons)

    def test_capture_output_on_a_forking_command_never_sees_eof(self):
        """老写法具型：`capture_output=True` + 超时 = 明江 09:15 那个 `steps=0`。

        这条**不是**在测被测代码 —— 它钉的是"这个夹具真的能重放事故"。否则后面那句
        "守护步现在毫秒级返回"就是空口无凭：夹具要是没 fork 出握着管道的留守者，
        所有判据都会假绿。
        """
        with fixture_env(FAKE_DAEMON_LIFETIME="3"):
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run([WL_COPY], input=OUR_TEXT.encode("utf-8"),
                               capture_output=True, timeout=1.0)
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.0, "压根没等到超时 ⇒ 这条具型是假的")
        self.assertTrue(wait_for(lambda: daemon_pids(), 2.0),
                        "夹具没 fork 出留守者 ⇒ 具型不成立")

    def test_it_is_stderr_that_holds_the_pipe_not_stdout(self):
        """三种管道组合各试一次：**唯一握着管道的是 stderr**（与 wl-copy 源码逐字对上）。

        ①两个都接管道 ⇒ 超时；②只留 stderr 接管道 ⇒ 还是超时；③把 stderr 换成 /dev/null
        ⇒ 立刻拿到 EOF。三个变量只改一件事：谁接管道。**只重定向 stdout 的"半修"**
        会死在这条上 —— 这正是本轮要防的那种修法。
        """
        for label, kw in (("① stdout+stderr 都接管道",
                           dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)),
                          ("② 只留 stderr 接管道",
                           dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))):
            with self.subTest(label):
                with fixture_env(FAKE_DAEMON_LIFETIME="2"):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        subprocess.run([WL_COPY], input=b"x", timeout=0.8, **kw)
        with fixture_env(FAKE_DAEMON_LIFETIME="2"):
            proc = subprocess.run([WL_COPY], input=b"x", stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=5.0)
        self.assertEqual(proc.returncode, 0,
                         "③ 把 stderr 换掉就该立刻 EOF ⇒ 否则说明握着管道的不是 stderr")


# ── B. 执行器：两类命令两种等法 ────────────────────────────────────────────────
class TestDaemonStepIsWaitedForByParentExitOnly(unittest.TestCase):
    def setUp(self):
        reset_clipboard()
        self.addCleanup(kill_fixture_daemons)

    def test_the_write_step_returns_while_the_forked_child_still_runs(self):
        """反例具型：老写法在这条上要等到 5 秒超时；守护步必须**只等父进程退出**（毫秒级）。"""
        step = INJ.Step([WL_COPY], stdin=OUR_TEXT.encode("utf-8"), label="写剪贴板",
                        stage=INJ.STAGE_WRITE, daemonizes=True)
        with fixture_env(FAKE_DAEMON_LIFETIME="3"):
            started = time.monotonic()
            outcome = INJ.run_step(step, subprocess.run, state_dir=fresh_state_dir())
            elapsed = time.monotonic() - started
            self.assertTrue(wait_for(lambda: daemon_pids(), 2.0),
                            "夹具没 fork 出留守者 ⇒ 这条判据没咬到东西")
            pid = daemon_pids()[-1]
            try:
                os.kill(pid, 0)                 # 留守者这时候**还活着**：我们没等它
            except ProcessLookupError:          # pragma: no cover - 只在夹具坏掉时发生
                self.fail("return 时留守者已经没了 ⇒ 执行器还在等它（或夹具寿命太短）")
        self.assertEqual(outcome.rc, 0)
        self.assertLess(outcome.ms, 1000, f"守护步等了 {outcome.ms}ms ⇒ 又在等管道 EOF 了")
        self.assertLess(elapsed, 2.0, f"墙上时间 {elapsed:.2f}s ⇒ 又被 {INJ.STEP_TIMEOUT_S}s 掐过")
        self.assertEqual(outcome.stderr_text, "",
                         "守护步不许带出文本：里面的字节可能是剪贴板内容的片段（隐私红线）")

    def test_normal_step_still_captures_its_stderr(self):
        """一条刀切的反例：普通步（ydotool）**必须**还收得到 stderr 文本。

        守护/普通两种等法里，普通步那半要是也被改成"只读字节数"，这条红 ——
        那就是再造一个"读不到错误"的瞎子。
        """
        step = INJ.Step([YDOTOOL, "key"], label="Ctrl+V", stage=INJ.STAGE_PASTE)
        with fixture_env(FAKE_YDOTOOL_RC="3", FAKE_YDOTOOL_STDERR="failed to connect socket"):
            outcome = INJ.run_step(step, subprocess.run, state_dir=fresh_state_dir())
        self.assertEqual(outcome.rc, 3)
        self.assertIn("failed to connect socket", outcome.stderr_text)
        self.assertEqual(outcome.err_len, len("failed to connect socket"))

    def test_ydotool_failure_still_shows_its_stderr_in_the_result(self):
        """行为级：粘贴那步失败时，那段错误文本要能走到 `InjectResult.message` 里。"""
        reset_clipboard(**{"text/html": b"x"})
        with fixture_env(FAKE_YDOTOOL_RC="3", FAKE_YDOTOOL_STDERR="failed to connect socket"):
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(),
                                runner=subprocess.run)
        self.assertEqual(result.code, "step_failed")
        self.assertIn("failed to connect socket", result.message)
        self.assertEqual(result.steps, 1)

    def test_a_failing_daemonizing_step_is_decided_without_waiting_on_the_pipe(self):
        """父进程退非 0 ⇒ 结论照样**立刻**有（不靠等管道），详情只有规模、没有文本。"""
        step = INJ.Step([WL_COPY], stdin=b"x", label="写剪贴板", stage=INJ.STAGE_WRITE,
                        daemonizes=True)
        with fixture_env(FAKE_COPY_RC="1", FAKE_STDERR_PROBE="什么事都没成"):
            started = time.monotonic()
            outcome = INJ.run_step(step, subprocess.run, state_dir=fresh_state_dir())
            elapsed = time.monotonic() - started
        self.assertEqual(outcome.rc, 1)
        self.assertLess(elapsed, 2.0, "rc≠0 也不许靠等管道才知道")
        self.assertGreaterEqual(outcome.err_len, len("什么事都没成".encode("utf-8")),
                                "错误输出确实进过那份文件 ⇒「只报规模」这句话才有对象")
        self.assertEqual(outcome.stderr_text, "", "守护步的 stderr_text 恒为空串（隐私红线）")


class TestDaemonStepTempFileHygiene(unittest.TestCase):
    """临时错误文件：状态目录里、0600、读完即毁 —— 成功 / 失败 / 超时三条路都不许留。"""

    def setUp(self):
        reset_clipboard()
        self.addCleanup(kill_fixture_daemons)

    def test_the_error_file_is_0600_inside_the_state_dir_and_unlinked_afterwards(self):
        seen: dict = {}
        step = INJ.Step([WL_COPY], stdin=b"x", label="写剪贴板", stage=INJ.STAGE_WRITE,
                        daemonizes=True)
        state = fresh_state_dir()
        with fixture_env():
            outcome = INJ.run_step(step, spy_runner(seen), state_dir=state)
        self.assertEqual(outcome.rc, 0)
        self.assertTrue(seen.get("path"), "守护步没走文件重定向那条路 ⇒ 判据没咬到东西")
        self.assertEqual(seen["mode"], 0o600, f"临时错误文件的权限不对：{seen}")
        self.assertEqual(Path(seen["path"]).parent, Path(state),
                         f"临时文件跑到状态目录外面去了：{seen['path']}")
        self.assertFalse(Path(seen["path"]).exists(), "读完没瞬毁 ⇒ 剪贴板片段留在盘上")
        self.assertEqual(list(Path(state).iterdir()), [], "状态目录里还有没被收干净的东西")

    def test_the_error_file_is_unlinked_when_the_step_fails(self):
        step = INJ.Step([WL_COPY], stdin=b"x", label="写剪贴板", stage=INJ.STAGE_WRITE,
                        daemonizes=True)
        state = fresh_state_dir()
        with fixture_env(FAKE_COPY_RC="2"):
            outcome = INJ.run_step(step, subprocess.run, state_dir=state)
        self.assertEqual(outcome.rc, 2)
        self.assertEqual(list(Path(state).iterdir()), [], "失败路径上留下了临时错误文件")

    def test_the_error_file_is_unlinked_when_the_step_times_out(self):
        """父进程自己挂住了（不是"守数据"那种正常形状）⇒ 超时照抛，文件照样瞬毁。"""
        step = INJ.Step([WL_COPY], stdin=b"x", label="写剪贴板", stage=INJ.STAGE_WRITE,
                        daemonizes=True)
        state = fresh_state_dir()
        with fixture_env(FAKE_PARENT_SLEEP="5", FAKE_NO_FORK="1"):
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                INJ.run_step(step, subprocess.run, state_dir=state, timeout=0.3)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "timeout 参数没生效")
        self.assertEqual(list(Path(state).iterdir()), [], "超时路径上留下了临时错误文件")


# ── C. 自证 / 隐私全链 ────────────────────────────────────────────────────────
class TestWriteSelfProof(unittest.TestCase):
    """`wl-copy` 父进程 rc=0 只代表"数据交给了它自己的留守者"，**不代表剪贴板里是我们的字**。"""

    def setUp(self):
        reset_clipboard()
        self.addCleanup(kill_fixture_daemons)

    def test_the_chain_now_succeeds_where_the_incident_timed_out(self):
        """全链行为级：真子进程 + 真 fork，写入毫秒级退回、粘贴那步真的发了。

        改动前这条是**红**的（第一步等管道 EOF ⇒ `step_failed` / `steps=0` / 5 秒）。
        """
        reset_clipboard(**{"text/html": "<b>原物</b>".encode("utf-8")})
        with fixture_env():
            started = time.monotonic()
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(),
                                runner=subprocess.run)
            elapsed = time.monotonic() - started
        self.assertEqual(result.code, "", result.message)
        self.assertTrue(result.ok, result.message)
        self.assertIsNotNone(result.copy_ms)
        self.assertLess(result.copy_ms, 1000, f"写入用了 {result.copy_ms}ms ⇒ 又在等管道 EOF")
        self.assertLess(elapsed, 2.0, f"整条链 {elapsed:.2f}s ⇒ 还踩在 {INJ.STEP_TIMEOUT_S}s 上")
        self.assertEqual(result.stages,
                         [INJ.STAGE_SNAPSHOT, INJ.STAGE_WRITE, INJ.STAGE_PASTE])
        self.assertTrue(paste_happened(), f"Ctrl+V 那步没发：{calls()}")
        self.assertEqual(clipboard_bytes("text/plain;charset=utf-8"),
                         OUR_TEXT.encode("utf-8"), "写进去的不是我们要的那段")
        self.assertEqual(list(STATE_DIR.iterdir()), [])

    def test_owner_rejected_when_the_selection_never_becomes_ours(self):
        """回读不是我们的文本 ⇒ **`owner_rejected`**（不是笼统 `step_failed`）、**粘贴那步不发**。

        真机若命中这条，说明 Wayland 侧没把选区所有权接走（"当时没有键盘焦点的客户端"
        最可疑，本仓尚未实测）。无论真凶是谁，这一刻**文本不在剪贴板里**：
        既不许说"已写进剪贴板"，也不许按下 Ctrl+V。
        """
        reset_clipboard(**{"text/html": "<b>原物</b>".encode("utf-8")})
        with fixture_env(FAKE_STORE_OTHER="别人的东西"):
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(),
                                runner=subprocess.run)
        self.assertEqual(result.code, "owner_rejected", result.message)
        self.assertFalse(result.ok)
        self.assertFalse(paste_happened(), f"所有权都没拿到还按了 Ctrl+V：{calls()}")
        self.assertIn("没上屏", result.describe())
        self.assertNotIn("已上屏", result.describe())
        self.assertIn("所有权", result.clipboard_note)
        self.assertIsNone(result.restore_job, "这时还原更糟：我们连自己写没写成都不知道")
        note = W.idle_note({"code": "owner_rejected", "ok": False})
        self.assertIn("失败", note)
        self.assertNotIn("结束", note, "失败不许说成「结束」")

    def test_a_failing_daemon_step_reports_a_size_not_the_content(self):
        """守护步失败时只许报"错误输出 N 字节"：那文件里的字节可能是剪贴板内容的片段。"""
        reset_clipboard(**{"text/html": b"x"})
        with fixture_env(FAKE_COPY_RC="1", FAKE_STDERR_PROBE=CARD):
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(),
                                runner=subprocess.run)
        self.assertEqual(result.code, "step_failed")
        self.assertIn("错误输出", result.message)
        self.assertIn("不转述", result.message)
        self.assertNotIn(CARD, result.message)
        self.assertNotIn(CARD, result.hint)
        self.assertIn(CARD, daemon_wrote(),
                      "探针没上场 ⇒ 这条判据什么都没拦（先修夹具，别修断言）")

    def test_only_a_byte_exact_readback_counts(self):
        """三态：逐字节相等 ⇒ True；别人的文本 ⇒ False；这台机读不了 ⇒ None（不把上屏判死）。

        `payload + b"\\n"` 那条是**真 wl-paste 的行为**（除 `-n` 外它会给文本补一个换行）：
        回读核对必须逐字节，多一个换行也不算自证成立。
        """
        payload = OUR_TEXT.encode("utf-8")

        def runner(stdout=b"", rc=0):
            def run(argv, **kw):
                return type("R", (), {"returncode": rc, "stdout": stdout, "stderr": b""})()
            return run

        self.assertIs(INJ.verify_clipboard_write(payload, False, WL_PASTE, runner(payload)), True)
        self.assertIs(INJ.verify_clipboard_write(payload, False, WL_PASTE,
                                                 runner("别人的".encode("utf-8"))), False)
        self.assertIs(INJ.verify_clipboard_write(payload, False, WL_PASTE, runner(payload + b"\n")),
                      False, "多一个字节也算不上自证")
        self.assertIsNone(INJ.verify_clipboard_write(payload, False, None, runner(payload)),
                          "没装 wl-paste 时不许把上屏判死（三态里的 None）")
        self.assertIsNone(INJ.verify_clipboard_write(None, False, WL_PASTE, runner(payload)))

    def test_the_done_line_carries_the_write_milliseconds(self):
        """`inject_done … copy_ms=`：修好之后"写入到底几毫秒"必须**看得见**（打点是自证的一半）。

        没跑到写入步（manual / 前置失败）记 `-1`，不许拿一个假的 0 充数 —— 0 会被读成
        "父进程瞬间退了"，那是另一种撒谎。
        """
        line = W.inject_done_line({"backend": "clipboard", "ok": True, "code": "", "steps": 2,
                                   "gate": "floor", "copy_ms": 7})
        self.assertIn("copy_ms=7", line)
        self.assertNotIn("copy_ms=-1", line)
        self.assertIn("copy_ms=-1",
                      W.inject_done_line({"backend": "manual", "ok": True, "code": "manual"}))
        self.assertIn(f"copy_ms={INJ.STEP_TIMEOUT_S * 1000:.0f}",
                      W.inject_done_line({"copy_ms": int(INJ.STEP_TIMEOUT_S * 1000)}),
                      "事故那个数（5000）也得能从这一行里读出来，否则事后没人分得清")

    def test_missing_wl_paste_does_not_kill_the_injection_but_says_it_was_not_checked(self):
        """没装 `wl-paste` ⇒ 不把上屏判死（既有口径），但摘要必须写"未回读核对"。"""
        reset_clipboard(**{"text/html": b"x"})
        with fixture_env():
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(wl_paste=None),
                                runner=subprocess.run)
        self.assertTrue(result.ok, result.message)
        self.assertIn("未回读核对", result.clipboard_note)
        self.assertIsNotNone(result.copy_ms, "写入毫秒数照样要打点")

    def test_clipboard_bytes_never_reach_any_surface(self):
        """**全链隐私条**：剪贴板里的字节（含守护步临时文件里躺着的那份）一个都不许外泄。

        跑的是真 `Server` + 真 `Session._inject_directly`（含还原定时器），两轮：
        ① **成功那轮**：`CARD` 上屏前就躺在剪贴板里（它会走完快照 → 还原整条链）；
        ② **失败那轮**：写入那步 rc≠0，父进程退之前往 stderr 里写的正是 `CARD`
           —— 这一轮的 `message` 会进错误帧，是最容易把内容带出去的口子。
        写进哪一个面都算红：`voice.log`、事件帧、诊断行、状态帧、摘要、命令记录、状态目录。
        """
        reset_clipboard(**{"text/plain;charset=utf-8": CARD.encode("utf-8")})
        state = _sandbox("privacy-chain")
        blobs = {}

        def server():
            srv = W.Server(run_command=subprocess.run, review_ui=False)
            srv.backend = "clipboard"
            srv.probe = lambda: probe()
            srv._events = []
            srv.broadcast = srv._events.append
            srv._deferred = []
            srv.after_ms = lambda ms, fn: srv._deferred.append((ms, fn))
            return srv

        with fixture_env(FAKE_STDERR_PROBE=CARD + "\n", CAPSPLUS_STATE_DIR=str(state)):
            srv = server()
            session = srv._ensure_session()
            session._inject_directly(OUR_TEXT, "")
            for _ms, fn in srv._deferred:                # 到点 → 后台线程做条件还原
                fn()
            self.assertTrue(wait_for(lambda: "已还原" in srv.clipboard_summary
                                     or "未还原" in srv.clipboard_summary),
                            f"还原没落地：{srv.clipboard_summary}")
            blobs.update({
                "成功轮·voice.log": P.log_path().read_text(encoding="utf-8", errors="replace"),
                "成功轮·事件帧": json.dumps(srv._events, ensure_ascii=False, default=str),
                "成功轮·状态帧": json.dumps(srv._status_frame(), ensure_ascii=False, default=str),
                "成功轮·剪贴板摘要": srv.clipboard_summary,
                "成功轮·inject_diag 行": W.inject_diag_line(session.inject_diag or {}),
                "成功轮·inject_done 行": W.inject_done_line(session.inject_diag or {}),
                "成功轮·verdict 行": W.verdict_line(session.inject_diag or {},
                                                    srv.clipboard_diag),
                "成功轮·上屏摘要": session.inject_summary,
                "成功轮·命令记录": " ".join(calls()),
                "成功轮·状态目录": " ".join(p.name for p in Path(state).iterdir()),
            })
            log_before = P.log_path().read_text(encoding="utf-8", errors="replace")

        with fixture_env(FAKE_COPY_RC="1", FAKE_STDERR_PROBE=CARD + "\n", FAKE_NO_FORK="1",
                         CAPSPLUS_STATE_DIR=str(state)):
            srv2 = server()
            session2 = srv2._ensure_session()
            session2._inject_directly(OUR_TEXT, "")
            blobs.update({
                "失败轮·事件帧": json.dumps(srv2._events, ensure_ascii=False, default=str),
                "失败轮·状态帧": json.dumps(srv2._status_frame(), ensure_ascii=False, default=str),
                "失败轮·剪贴板摘要": srv2.clipboard_summary,
                "失败轮·上屏摘要": session2.inject_summary,
                "失败轮·新日志": P.log_path().read_text(encoding="utf-8",
                                                        errors="replace")[len(log_before):],
                "失败轮·状态目录": " ".join(p.name for p in Path(state).iterdir()),
            })
            self.assertIn("step_failed", session2.inject_summary, session2.inject_summary)

        for what, blob in blobs.items():
            self.assertNotIn(CARD, blob, f"剪贴板内容漏进 {what}：{blob}")
        self.assertIn("inject_done", blobs["成功轮·inject_done 行"],
                      "这条链没真跑到收尾，判据是空的")
        self.assertEqual(blobs["成功轮·状态目录"], "voice.log",
                         "状态目录里除了日志还有别的东西 ⇒ 临时错误文件没瞬毁")
        self.assertIn(CARD, daemon_wrote(), "探针没上场 ⇒ 这条判据什么都没拦")


if __name__ == "__main__":
    unittest.main(verbosity=2)