# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""上屏时的剪贴板备份 / 条件还原：四条判据，每条都带能红的反例具型。

要还的债（2026-10-17）：我对用户说过「约 800ms 后自动恢复原剪贴板」，而当时 `voice_inject.py`
里**没有任何 `wl-paste` 备份 / 回写**，文档只能按事实写成「会覆盖且不恢复」。这一轮把它做成事实。
行为顺序就是判据：

    snapshot → wl-copy <文本> → ydotool Ctrl+V → 等 CLIPBOARD_SETTLE_MS → **条件还原** → 释放快照

四条，各自都咬得住：

  1. **顺序**（`TestSequenceOrdering`）：快照在 `wl-copy` 之前、还原在 Ctrl+V **之后**、还原是最后一段。
     反例具型三种具型都红：把 snapshot 挪到 wl-copy 之后 / 删掉还原步 / 还原排在粘贴之前。
     另有行为级一条：假剪贴板记录的**实际 argv 顺序**逐字钉死 —— 代码里一挪动就红，不只检查器自娱。
  2. **条件还原**（`TestConditionalRestore`）：两个方向各一条 —— ①「还是我们写的那段」⇒ 还原确实发生；
     ②「已被别处改」⇒ **不还原**、摘要里写「剪贴板已被别处更新，未还原」、且一条命令都不许多跑。
  3. **空剪贴板**（`TestEmptyClipboard`）：还原动作是 `--clear`，**不是写一个空串**（stdin 必须是 None）；
     本机 `wl-copy --help` 探不到 `--clear` 时退化成「不还原 + 说明」，不许假设它有。
  4. **隐私**（`TestSnapshotNeverHitsDisk`）：照本仓「念一句银行卡号」那条判据的写法 —— 剪贴板里
     假装是卡号，跑完整条链后**状态目录里的每个文件、voice.log、每一帧事件、每一句摘要**都不许含它。

全套用可注入的假 runner（一台假 Wayland 剪贴板），headless 也跑：不起窗口、不打真键、
不碰真剪贴板；状态目录由 `env()` **默认**钉进本模块的沙箱（`SANDBOX`，模块结束随
`tearDownModule` 收掉），`TestSandboxGuard` 钉住这个默认值 —— 因为本轮真栽过一次：
某个类忘了指 `CAPSPLUS_STATE_DIR`，判据就往用户的 `~/.local/state/capsplus/voice.log` 里写了行。
"""

import atexit
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import voice_inject as INJ                 # noqa: E402
from capsplus import voice_proto as P                    # noqa: E402

FAKE_CODES = {"KEY_LEFTCTRL": 29, "KEY_V": 47}
CARD = "6222021234567890123"              # 假装躺在剪贴板里的卡号：隐私判据的探针
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
OUR_TEXT = "九点在三号楼开会"

# ── 沙箱护栏：本模块任何用例都不许碰用户的**真状态目录** ─────────────────────────
# 本轮真栽过一次：`TestRestoreRunsOnTheRealMainLoop` 忘了指 `CAPSPLUS_STATE_DIR`，于是
# `Server.run_clipboard_restore` 里的 `P.log_line` 往 `~/.local/state/capsplus/voice.log`
# 追加了 32 行（内容没漏，只有代码 `restored`，但那是他的文件）。
# 结论：**护栏必须是默认值，不能靠每个类自觉** —— `env()` 直接把状态目录钉进沙箱，
# 用 `env()` 的用例自动被保护，`TestSandboxGuard` 再钉死这件事。
SANDBOX = Path(tempfile.gettempdir()) / f"capsplus-clip-sandbox-{os.getpid()}"
SANDBOX.mkdir(parents=True, exist_ok=True)


def _cleanup_sandbox():
    shutil.rmtree(SANDBOX, ignore_errors=True)


# `tearDownModule` 只在"这个模块是被 loader 认领的那一份"时才被调到 —— 全量 discover 里同一份文件
# 会被以两个名字各 import 一次，于是有一份的沙箱永远留在那。atexit 才是与导入次数无关的收口。
atexit.register(_cleanup_sandbox)


def _sandbox_dir(name: str) -> Path:
    path = SANDBOX / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# 2026-10-19：`run_step` 的守护步在**状态目录**里开 0600 临时错误文件（读完即毁）。
# 本模块有一批判据在 `env()` 之外直接调 inject/perform_restore（还原链那几个），没有模块级
# 护栏就会把文件写进用户的真状态目录 —— 护栏必须是默认值，不能靠每个类自觉（TestSandboxGuard
# 的同一个教训）。`env()` 的 with-patch 仍可覆盖；两处指的都是本模块 SANDBOX 下的子目录。
_ORIG_STATE_DIR = os.environ.get("CAPSPLUS_STATE_DIR")


def setUpModule():
    os.environ["CAPSPLUS_STATE_DIR"] = str(_sandbox_dir("module"))


def tearDownModule():
    _cleanup_sandbox()
    if _ORIG_STATE_DIR is None:
        os.environ.pop("CAPSPLUS_STATE_DIR", None)
    else:
        os.environ["CAPSPLUS_STATE_DIR"] = _ORIG_STATE_DIR


def _proc(rc=0, out=b"", err=b""):
    return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """等后台线程把活干完。**只有测试侧在轮询**：被测代码里一处 sleep 都没有（AST 门管着）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


class FakeWaylandClipboard:
    """一台假剪贴板 + 一个假键注入器，同时是**判据的记录器**。

    只实现本模块真用到的四种问句：`--list-types`、`--no-newline --type M`、`wl-copy`
    （带 `--primary` / `--clear` / `--type` 的组合）、`ydotool`。
    假件不许「什么都答应」：类型不在清单里要像真 `wl-paste` 一样 rc≠0 —— 否则
    「已被别处改」和「读不到」两种情况分不开，判据 2 就会假绿。
    """

    def __init__(self, offerings=None, clear_supported=True):
        self.clip = dict(offerings or {})
        self.prim = {}
        self.clear_supported = clear_supported
        self.calls = []
        self.threads = []          # 每条命令跑在**哪个线程**（主线程 = 判据要红的形状）
        self.stamps = []           # 每条命令的时刻（验证"等了一段才还原"）

    # -- 测试侧的手：模拟「别的应用在这段等待里复制了东西」--------------------
    def put(self, mime: str, data: bytes, primary: bool = False) -> None:
        (self.prim if primary else self.clip)[mime] = data

    def types(self, primary: bool = False) -> tuple:
        return tuple((self.prim if primary else self.clip).keys())

    @property
    def joined(self):
        return [" ".join(a) for a, _ in self.calls]

    def payloads(self):
        return [inp for _argv, inp in self.calls]

    def where(self, needle: str):
        """最后一条含 `needle` 的命令：返回 (是否主线程, argv, 时刻)。"""
        for argv, inp in reversed(self.calls):
            if needle in " ".join(argv):
                i = len(self.calls) - 1 - self.calls[::-1].index((argv, inp))
                return self.threads[i], argv, self.stamps[i]
        raise AssertionError(f"没跑过含 {needle!r} 的命令：{self.joined}")

    def _store(self, primary: bool) -> dict:
        return self.prim if primary else self.clip

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw.get("input")))
        self.threads.append(threading.current_thread() is threading.main_thread())
        self.stamps.append(time.monotonic())
        tool = os.path.basename(argv[0])
        rest = argv[1:]
        primary = "--primary" in rest
        if tool == "wl-paste":
            store = self._store(primary)
            if "--list-types" in rest:
                body = b"\n".join(t.encode("utf-8") for t in store)
                return _proc(0, body + (b"\n" if store else b""))
            if "--type" in rest:
                mime = rest[rest.index("--type") + 1]
                if mime in store:
                    return _proc(0, store[mime])
                return _proc(1, err=b"selection does not offer that type")
            return _proc(1, err=b"no plain text target")
        if tool == "wl-copy":
            if "--clear" in rest:
                if not self.clear_supported:
                    return _proc(1, err=b"unrecognized option `--clear'")
                self._store(primary).clear()
                return _proc(0)
            data = kw.get("input") or b""
            if "--type" in rest:
                offers = {rest[rest.index("--type") + 1]: data}
            else:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    offers = {"application/octet-stream": data}
                else:
                    offers = {"text/plain;charset=utf-8": data, "text/plain": data}
            store = self._store(primary)
            store.clear()
            store.update(offers)
            return _proc(0)
        if tool == "ydotool":
            return _proc(0)                     # 假键注入：只记账，什么都不动
        return _proc(127, err=b"unexpected tool")


def probe(**over) -> INJ.Probe:
    """一份「探测全通过、备份能力也齐」的假结论；`over` 用来逐项打破它。"""
    base = dict(wl_copy="/usr/bin/wl-copy", wl_copy_primary=True,
                wl_paste="/usr/bin/wl-paste", wl_copy_clear=True,
                ydotool="/usr/bin/ydotool", ydotool_subcommands=("key", "click", "type"),
                ydotool_daemon=True, syntax=("keycode", "click-bitmask"),
                keycodes=dict(FAKE_CODES), keycodes_source="/fake/input-event-codes.h",
                ran=False)
    base.update(over)
    return INJ.Probe(**base)


@contextlib.contextmanager
def env(**over):
    """干净环境：还原开关默认开，并把 `CAPSPLUS_STATE_DIR` 钉进沙箱。

    状态目录在这里给默认值是**故意的**（见上面沙箱护栏那段）：判据跑的是真 `Server`，
    它会写 `voice.log` —— 不设默认就等于往用户的真目录里写。`over` 仍可覆盖（`TestSandboxGuard`
    就是查这个默认值咬不咬得住）。
    """
    base = {k: v for k, v in os.environ.items()
            if k not in (INJ.RESTORE_ENV, "CAPSPLUS_STATE_DIR")}
    base.update(over)
    base.setdefault("CAPSPLUS_STATE_DIR", str(_sandbox_dir("env")))
    with mock.patch.dict(os.environ, base, clear=True):
        yield


# ── 判据 1：顺序 ───────────────────────────────────────────────────────────────
def stage_order_violations(stages, expect_restore: bool = False) -> list[str]:
    """时序检查器（纯函数，返回空 = 合规）。规则五条，少一条就是摆设。"""
    known = [s for s in stages if s in INJ.KNOWN_STAGES]
    bad = [f"UNKNOWN-STAGE:{s}" for s in stages if s not in INJ.KNOWN_STAGES]
    if INJ.STAGE_SNAPSHOT in known and INJ.STAGE_WRITE in known:
        if known.index(INJ.STAGE_SNAPSHOT) > known.index(INJ.STAGE_WRITE):
            bad.append("SNAPSHOT-AFTER-WRITE：快照在读剪贴板之前没做，读回来的已经是我们的文本")
    if INJ.STAGE_RESTORE in known:
        at = known.index(INJ.STAGE_RESTORE)
        if INJ.STAGE_SNAPSHOT not in known:
            bad.append("RESTORE-WITHOUT-SNAPSHOT：没备份却要还原，还原的是空气")
        if INJ.STAGE_PASTE not in known[:at]:
            bad.append("RESTORE-BEFORE-PASTE：还没粘贴就回写，等于把刚递出去的文本抽走")
        if at != len(known) - 1:
            bad.append("RESTORE-NOT-LAST：还原之后还有动作，那些动作读到的已不是用户的原物")
    elif expect_restore:
        bad.append("RESTORE-MISSING：这一轮备份成功了，序列里却没有还原段")
    return bad


class TestSequenceOrdering(unittest.TestCase):
    def test_full_chain_stages_are_snapshot_write_paste_restore(self):
        clip = FakeWaylandClipboard({"text/html": "<b>用户的原文</b>".encode("utf-8")})
        with env():
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(), runner=clip)
        self.assertEqual(result.stages, [INJ.STAGE_SNAPSHOT, INJ.STAGE_WRITE, INJ.STAGE_PASTE])
        # 没还原时检查器必须红（这就是「删掉还原步」那个具型）
        self.assertTrue(any("RESTORE-MISSING" in v for v in
                            stage_order_violations(result.stages, expect_restore=True)))
        armed = result.stages + [INJ.STAGE_RESTORE]      # 还原由调用方在主循环到点后补上
        self.assertEqual(stage_order_violations(armed, expect_restore=True), [], armed)

    def test_runner_saw_the_snapshot_commands_before_the_copy(self):
        """行为级：假剪贴板记录的**实际命令顺序**逐字钉死 —— 代码里一挪动就红。

        2026-10-19 起，`wl-copy` 之后多一条**回读自证**（`wl-paste --no-newline --type …`，
        逐字节比对"写进去的就是剪贴板里的那份"）。它不是第二段，`stages` 里也不该多一段 ——
        它属于 write 那段的自证；命令序列这条判据照样把它逐字钉住。
        """
        clip = FakeWaylandClipboard({"text/html": "<b>原文</b>".encode("utf-8")})
        with env():
            INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(), runner=clip)
        self.assertEqual(clip.joined, [
            "/usr/bin/wl-paste --list-types",
            "/usr/bin/wl-paste --no-newline --type text/html",
            "/usr/bin/wl-copy",
            "/usr/bin/wl-paste --no-newline --type text/plain;charset=utf-8",   # 回读自证
            "/usr/bin/ydotool key 29:1 47:1 47:0 29:0",
        ], f"上屏的命令顺序变成这样了：{clip.joined}")

    def test_inject_does_not_run_the_restore_itself(self):
        """还原**不许**发生在 `inject` 里（那是刚粘完的当口，目标应用还没把数据取走）。"""
        clip = FakeWaylandClipboard({"text/html": b"x"})
        with env():
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(), runner=clip)
        self.assertIsNotNone(result.restore_job)
        # 快照读 2 条 + 写 1 条 + 回读自证 1 条 + Ctrl+V 1 条；多出来的那条就是"偷偷还原了"。
        self.assertEqual(len(clip.calls), 5, f"inject 里就还原了：{clip.joined}")
        self.assertEqual(clip.joined[-1], "/usr/bin/ydotool key 29:1 47:1 47:0 29:0",
                         f"最后一条命令不是粘贴 ⇒ 还原插在注入里了：{clip.joined}")
        self.assertNotIn("--clear", " ".join(clip.joined))

    # -- 反例具型 --------------------------------------------------------------
    def test_counterexample_snapshot_moved_after_copy_is_red(self):
        bad = stage_order_violations([INJ.STAGE_WRITE, INJ.STAGE_PASTE,
                                      INJ.STAGE_SNAPSHOT, INJ.STAGE_RESTORE])
        self.assertTrue(any("SNAPSHOT-AFTER-WRITE" in v for v in bad),
                        f"把快照挪到 wl-copy 之后居然没红：{bad}")

    def test_counterexample_restore_step_deleted_is_red(self):
        bad = stage_order_violations([INJ.STAGE_SNAPSHOT, INJ.STAGE_WRITE, INJ.STAGE_PASTE],
                                     expect_restore=True)
        self.assertTrue(any("RESTORE-MISSING" in v for v in bad),
                        f"把还原整段删掉居然没红：{bad}")

    def test_counterexample_restore_before_paste_is_red(self):
        bad = stage_order_violations([INJ.STAGE_SNAPSHOT, INJ.STAGE_RESTORE,
                                      INJ.STAGE_WRITE, INJ.STAGE_PASTE])
        self.assertTrue(any("RESTORE-BEFORE-PASTE" in v for v in bad),
                        f"还原排在粘贴之前居然没红：{bad}")

    def test_counterexample_restore_not_last_is_red(self):
        bad = stage_order_violations([INJ.STAGE_SNAPSHOT, INJ.STAGE_WRITE, INJ.STAGE_PASTE,
                                      INJ.STAGE_RESTORE, INJ.STAGE_PASTE])
        self.assertTrue(any("RESTORE-NOT-LAST" in v for v in bad), bad)

    def test_counterexample_restore_without_snapshot_is_red(self):
        bad = stage_order_violations([INJ.STAGE_WRITE, INJ.STAGE_PASTE, INJ.STAGE_RESTORE])
        self.assertTrue(any("RESTORE-WITHOUT-SNAPSHOT" in v for v in bad), bad)

    def test_counterexample_invented_stage_name_is_red(self):
        self.assertTrue(any("UNKNOWN-STAGE" in v for v in
                            stage_order_violations(["snapshot", "copy", "paste"])))

    def test_clean_sequence_is_accepted(self):
        self.assertEqual(stage_order_violations(
            [INJ.STAGE_SNAPSHOT, INJ.STAGE_WRITE, INJ.STAGE_PASTE, INJ.STAGE_RESTORE],
            expect_restore=True), [])

    def test_primary_backend_snapshots_the_primary_selection(self):
        clip = FakeWaylandClipboard()
        clip.put("text/html", b"<i>sel</i>", primary=True)
        with env():
            result = INJ.inject(OUR_TEXT, backend="primary", probe=probe(), runner=clip)
        self.assertIn("--primary", clip.joined[0].split(), clip.joined[0])
        self.assertTrue(result.restore_job.primary)
        self.assertEqual(clip.joined[2], "/usr/bin/wl-copy --primary")

    def test_disabled_switch_reads_nothing_from_the_clipboard(self):
        """开关关掉时**不做快照读**（少一次对用户数据的访问），也不留待还原作业。

        2026-10-19 起这条要说得更准：`inject` 里还有一次读 —— **回读自证**自己刚写进去的那段
        （判"写没写成"，与还原无关，开关管不了它）。所以判据钉的是**没有 `--list-types` 快照读**，
        而不是"一条 wl-paste 都不许有"；后者会把回读自证也一起判死。
        """
        clip = FakeWaylandClipboard({"text/html": b"x"})
        with env(**{INJ.RESTORE_ENV: "0"}):
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(), runner=clip)
        self.assertEqual(clip.joined,
                         ["/usr/bin/wl-copy",
                          "/usr/bin/wl-paste --no-newline --type text/plain;charset=utf-8",
                          "/usr/bin/ydotool key 29:1 47:1 47:0 29:0"])
        self.assertNotIn("--list-types", " ".join(clip.joined),
                         "开关关着还去列类型读快照 = 多一次对用户剪贴板数据的访问")
        self.assertIsNone(result.restore_job)
        self.assertIn("CAPSPLUS_VOICE_RESTORE_CLIPBOARD=0", result.clipboard_note)

    def test_switch_helper_only_zero_turns_it_off(self):
        with env():
            self.assertTrue(INJ.restore_from_env(), "默认必须是开")
        for value in ("0", " 0 "):
            with env(**{INJ.RESTORE_ENV: value}):
                self.assertFalse(INJ.restore_from_env(), value)
        for value in ("1", "on", "", "nope"):
            with env(**{INJ.RESTORE_ENV: value}):
                self.assertTrue(INJ.restore_from_env(), value)

    def test_degraded_write_never_arms_a_restore(self):
        """探测没过 ⇒ 只写剪贴板等用户自己粘贴：这时还原会把**他还没粘出去的东西**抽走。

        2026-10-19 起降级路径当场**回读自证**（它是「先占后贴」的前半段：我们必然没有键盘焦点，
        正是"所有权可能被拒"的最坏形状），所以命令里多一条回读 —— 那是自证，不是还原。
        """
        clip = FakeWaylandClipboard({"text/html": b"x"})
        with env():
            result = INJ.inject(OUR_TEXT, backend="clipboard",
                                probe=probe(ydotool_daemon=False), runner=clip)
        self.assertEqual(result.code, "needs_manual_paste")
        self.assertIsNone(result.restore_job)
        self.assertEqual(clip.joined,
                         ["/usr/bin/wl-copy",
                          "/usr/bin/wl-paste --no-newline --type text/plain;charset=utf-8"],
                         clip.joined)
        self.assertIn("不做还原", result.clipboard_note)

    def test_manual_backend_touches_the_clipboard_not_at_all(self):
        clip = FakeWaylandClipboard({"text/html": b"x"})
        with env():
            result = INJ.inject(OUR_TEXT, backend="manual", probe=probe(), runner=clip)
        self.assertEqual(clip.calls, [], "manual 后端读/写了剪贴板 = 动了它原有的语义")
        self.assertIn("一个字节都不动", result.clipboard_note)

    def test_settle_constant_is_named_and_positive(self):
        """等待时长必须是**具名常量**（改它只动一处），且不能是 0（粘贴是异步的）。"""
        self.assertGreater(INJ.CLIPBOARD_SETTLE_MS, 0)
        self.assertLess(INJ.CLIPBOARD_SETTLE_MS, 1000,
                        "等太久 ⇒ 用户自己复制了新东西 ⇒ 条件还原判否，白等不还原")


# ── 判据：挑类型的规则（纯函数）--------------------------------------------------
class TestChooseMime(unittest.TestCase):
    def test_priority_prefers_the_unrecoverable_kind(self):
        both = ["TARGETS", "text/plain;charset=utf-8", "text/html", "image/png"]
        self.assertEqual(INJ.choose_mime(both), "image/png",
                         "图片 / 文件列表掉了就真没了，优先级必须压过文本")
        self.assertEqual(INJ.choose_mime(["text/plain", "text/uri-list"]), "text/uri-list")
        self.assertEqual(INJ.choose_mime(["text/plain;charset=utf-8", "text/html"]), "text/html")

    def test_priority_table_is_the_documented_one(self):
        self.assertEqual(INJ.SNAPSHOT_MIME_PRIORITY,
                         ("image/png", "text/uri-list", "text/html",
                          "text/plain;charset=utf-8", "text/plain"))

    def test_charset_parameters_are_matched_by_base_type(self):
        self.assertEqual(INJ.choose_mime(["TEXT", "text/plain; charset=utf-8"]),
                         "text/plain; charset=utf-8")

    def test_unknown_types_only_are_refused(self):
        """只有读不了的类型 ⇒ None。不许「挑个差不多的」凑：还原回错的东西比不还原更糟。"""
        self.assertIsNone(INJ.choose_mime(["wayland/roles", "x-special/gnome-copied-files"]))

    def test_empty_list_returns_none(self):
        self.assertIsNone(INJ.choose_mime([]))
        self.assertIsNone(INJ.choose_mime(["", "  "]))


class TestSnapshotReading(unittest.TestCase):
    def test_bytes_land_in_memory_and_the_summary_only_names_type_and_size(self):
        clip = FakeWaylandClipboard({"image/png": PNG})
        snap = INJ.snapshot_clipboard(probe(), clip)
        self.assertTrue(snap.backed_up)
        self.assertEqual(snap.mime, "image/png")
        self.assertEqual(snap.data, PNG)
        self.assertEqual(snap.describe(), f"image/png {len(PNG)} 字节")

    def test_too_large_is_dropped_not_kept(self):
        clip = FakeWaylandClipboard({"image/png": b"x" * 5000})
        snap = INJ.snapshot_clipboard(probe(), clip, max_bytes=1024)
        self.assertFalse(snap.backed_up)
        self.assertIsNone(snap.data, "超上限还把字节留在现场 = 常驻 worker 囤大对象")
        self.assertIn("超过上限", snap.problem)

    def test_unreadable_type_names_the_type_and_offers_the_way_out(self):
        clip = FakeWaylandClipboard({"wayland/roles": b"whatever"})
        snap = INJ.snapshot_clipboard(probe(), clip)
        self.assertFalse(snap.backed_up)
        self.assertIn("wayland/roles", snap.types)
        with env():
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(), runner=clip)
        self.assertIsNone(result.restore_job)
        self.assertIn("剪贴板里有 wayland/roles 无法备份", result.clipboard_note)
        self.assertIn("本次会覆盖且不还原", result.clipboard_note)
        self.assertIn("CAPSPLUS_VOICE_BACKEND=manual", result.clipboard_note,
                      "备份不了的这种情况必须给一条用户能自己走开的路（manual 后端）")
        self.assertTrue(result.ok, "备份不了不等于上屏失败，不许混报")

    def test_missing_wl_paste_does_not_block_injection(self):
        """没 wl-paste 只该让「还原」做不成，不该把整条上屏判死（那会退回更糟的行为）。"""
        clip = FakeWaylandClipboard({"text/html": b"x"})
        with env():
            result = INJ.inject(OUR_TEXT, backend="clipboard",
                                probe=probe(wl_paste=None), runner=clip)
        self.assertTrue(result.ok, result.describe())
        self.assertIsNone(result.restore_job)
        self.assertIn("没探到 wl-paste", result.clipboard_note)
        self.assertEqual(clip.joined,
                         ["/usr/bin/wl-copy", "/usr/bin/ydotool key 29:1 47:1 47:0 29:0"])

    def test_read_back_failure_is_a_problem_not_an_exception(self):
        class Boom:
            def __init__(self):
                self.calls = []

            def __call__(self, argv, **kw):
                self.calls.append(list(argv))
                if "--type" in argv:
                    raise OSError("没权限")
                return _proc(0, b"image/png\n")

        runner = Boom()
        snap = INJ.snapshot_clipboard(probe(), runner)
        self.assertIn("读 image/png 没跑起来", snap.problem)
        self.assertEqual(snap.mime, "image/png", "还得说清是哪个类型读不回")


# ── 判据 2：条件还原的两个方向 ──────────────────────────────────────────────────
class TestConditionalRestore(unittest.TestCase):
    def _job(self, clip, text=OUR_TEXT, probe_over=None):
        with env():
            result = INJ.inject(text, backend="clipboard",
                                probe=probe(**(probe_over or {})), runner=clip)
        self.assertIsNotNone(result.restore_job, "这条判据要求还原作业存在")
        return result.restore_job

    def test_direction_one_still_ours_restores_the_snapshot(self):
        clip = FakeWaylandClipboard({"text/html": "<b>用户的原文</b>".encode("utf-8")})
        job = self._job(clip)
        res = INJ.perform_restore(job, clip)
        self.assertTrue(res.did, res.describe())
        self.assertEqual(res.code, "restored")
        self.assertEqual(clip.types(), ("text/html",), f"还原后剪贴板成了 {clip.types()}")
        self.assertEqual(clip.clip["text/html"], "<b>用户的原文</b>".encode("utf-8"))

    def test_direction_two_changed_by_someone_else_must_not_restore(self):
        """用户在这段等待里自己复制了东西 ⇒ **不还原**：把人家刚拷的冲掉比不还原更糟。"""
        clip = FakeWaylandClipboard({"text/html": "<b>原文</b>".encode("utf-8")})
        job = self._job(clip)
        clip.put("text/plain;charset=utf-8", "他刚复制的密码".encode("utf-8"))
        clip.put("text/plain", "他刚复制的密码".encode("utf-8"))
        before = len(clip.calls)
        res = INJ.perform_restore(job, clip)
        self.assertFalse(res.did)
        self.assertEqual(res.code, "not_ours")
        self.assertIn("剪贴板已被别处更新，未还原", res.message)
        self.assertEqual(len(clip.calls), before + 1,
                         f"判否之后只许停手（只多一次现值检查）：{clip.joined[before:]}")
        self.assertEqual(clip.types(), ("text/plain;charset=utf-8", "text/plain"),
                         "用户刚复制的东西被我们盖掉了")

    def test_image_in_place_of_our_text_counts_as_changed(self):
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        job = self._job(clip)
        clip.clip.clear()
        clip.put("image/png", PNG)
        res = INJ.perform_restore(job, clip)
        self.assertEqual((res.did, res.code), (False, "not_ours"), res.describe())

    def test_unreadable_current_value_is_conservative_too(self):
        """现值根本读不回来（runner 抛）⇒ 当「改过了」处理：宁可少还原一次，不可错盖一次。"""
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        job = self._job(clip)

        def raising(argv, **kw):
            if os.path.basename(argv[0]) == "wl-paste":
                raise OSError("连不上 wayland")
            return clip(argv, **kw)

        seen = []

        def spy(argv, **kw):
            seen.append(list(argv))
            return raising(argv, **kw)

        res = INJ.perform_restore(job, spy)
        self.assertEqual((res.did, res.code), (False, "check_failed"), res.describe())
        self.assertNotIn("wl-copy", " ".join(" ".join(a) for a in seen),
                         "读不回现值还继续发还原命令 = 没停手")

    def test_exact_bytes_matter_a_trailing_space_is_someone_else(self):
        """比对是**逐字节**的：差一个换行/空格都算别人改过（宁可不做）。"""
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        job = self._job(clip)
        clip.clip.clear()
        clip.put("text/plain;charset=utf-8", (OUR_TEXT + " ").encode("utf-8"))
        clip.put("text/plain", (OUR_TEXT + " ").encode("utf-8"))
        self.assertEqual(INJ.perform_restore(job, clip).code, "not_ours")

    def test_second_utterance_within_the_window_restores_the_oldest_only(self):
        """连续两句：认作「我们的」的是**两段**文本，还原的是**最早**那份快照。"""
        clip = FakeWaylandClipboard({"text/html": "<b>原物</b>".encode("utf-8")})
        with env():
            first = INJ.inject("第一句", backend="clipboard", probe=probe(), runner=clip)
            second = INJ.inject("第二句", backend="clipboard", probe=probe(), runner=clip)
        merged = first.restore_job
        merged.ours.extend(second.restore_job.ours)
        self.assertEqual(len(merged.ours), 2)
        self.assertEqual(merged.snapshot.mime, "text/html", "合并时快照必须还是最早那份")
        res = INJ.perform_restore(merged, clip)
        self.assertTrue(res.did, res.describe())
        self.assertEqual(clip.clip.get("text/html"), "<b>原物</b>".encode("utf-8"))

    def test_verify_mismatch_is_not_reported_as_success(self):
        """还原命令发了但回读不一致（真机上多半是 worker 没键盘焦点）⇒ **不许报已还原**。"""
        clip = FakeWaylandClipboard({"text/html": "原物".encode("utf-8")})
        job = self._job(clip)

        def lying(argv, **kw):
            if os.path.basename(argv[0]) == "wl-copy":
                return _proc(0)                       # 声称成功，实际什么都不改
            return clip(argv, **kw)

        res = INJ.perform_restore(job, lying)
        self.assertFalse(res.did)
        self.assertEqual(res.code, "verify_mismatch", res.describe())
        self.assertIn("回读不一致", res.message)

    def test_verify_skipped_for_oversized_snapshots(self):
        clip = FakeWaylandClipboard({"image/png": b"x" * 64})
        job = self._job(clip)
        with mock.patch.object(INJ, "VERIFY_MAX_BYTES", 8):
            res = INJ.perform_restore(job, clip)
        self.assertTrue(res.did)
        self.assertEqual(res.code, "restored_unverified")
        self.assertIn("未回读核对", res.message)

    def test_restore_of_text_omits_type_so_every_target_comes_back(self):
        """还原纯文本**不加 --type**：加了只剩一种目标，挑 TEXT/UTF8_STRING 的应用会粘不到。"""
        clip = FakeWaylandClipboard({"text/plain;charset=utf-8": "原文".encode("utf-8")})
        job = self._job(clip)
        step = INJ.restore_steps(job)[0]
        self.assertEqual(step.argv, ["/usr/bin/wl-copy"])
        self.assertEqual(step.stdin, "原文".encode("utf-8"))

    def test_restore_of_binary_must_carry_the_type(self):
        """反过来：图片不加 --type 会被 wl-copy 猜成 octet-stream，还原回来的就不是那张图。"""
        clip = FakeWaylandClipboard({"image/png": PNG})
        job = self._job(clip)
        step = INJ.restore_steps(job)[0]
        self.assertEqual(step.argv, ["/usr/bin/wl-copy", "--type", "image/png"])
        self.assertEqual(step.stdin, PNG)
        self.assertEqual(INJ.perform_restore(job, clip).code, "restored")
        self.assertEqual(clip.types(), ("image/png",))

    def test_restore_command_failure_is_structured(self):
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        job = self._job(clip)

        def refusing(argv, **kw):
            if os.path.basename(argv[0]) == "wl-copy":
                return _proc(2, err="wl-copy: 这段 stderr 里可能带内容片段，不许进摘要".encode("utf-8"))
            return clip(argv, **kw)

        res = INJ.perform_restore(job, refusing)
        self.assertEqual((res.did, res.code), (False, "step_failed"), res.describe())
        self.assertNotIn("stderr", res.message)
        self.assertNotIn("内容片段", res.message)

    def test_no_backup_never_pretends_it_can_restore(self):
        """备份本身没成的作业（判据 5）走到这里也只能报「不还原」。"""
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        job = self._job(clip)
        job.snapshot = INJ.Snapshot(types=("wayland/roles",),
                                    problem="类型不在可备份清单里")
        before = len(clip.calls)
        res = INJ.perform_restore(job, clip)
        self.assertEqual((res.did, res.code), (False, "no_backup"), res.describe())
        self.assertIn("未还原", res.message)
        self.assertIn("类型不在可备份清单里", res.message, "得说清是哪种备份没成")
        self.assertEqual(len(clip.calls), before, "备份没成还发命令 = 拿用户的剪贴板试手")


# ── 判据 3：原本剪贴板是空的 ────────────────────────────────────────────────────
class TestEmptyClipboard(unittest.TestCase):
    def _result(self, clip, clear_supported=True):
        with env():
            return INJ.inject(OUR_TEXT, backend="clipboard",
                              probe=probe(wl_copy_clear=clear_supported), runner=clip)

    def test_empty_original_restores_by_clearing_not_by_writing_an_empty_string(self):
        clip = FakeWaylandClipboard({})
        result = self._result(clip, clear_supported=True)
        self.assertIsNotNone(result.restore_job,
                             "本机 wl-copy 认 --clear ⇒ 空剪贴板也必须能还原")
        step = INJ.restore_steps(result.restore_job)[0]
        self.assertEqual(step.argv, ["/usr/bin/wl-copy", "--clear"])
        self.assertIsNone(step.stdin, "写一个空串不算清空：别的应用会读到一条空文本")
        res = INJ.perform_restore(result.restore_job, clip)
        self.assertTrue(res.did, res.describe())
        self.assertEqual(res.code, "cleared")
        self.assertEqual(clip.types(), (), f"清空没生效：{clip.types()}")

    def test_empty_original_is_a_successful_backup_not_a_failure(self):
        clip = FakeWaylandClipboard({})
        snap = INJ.snapshot_clipboard(probe(), clip)
        self.assertTrue(snap.empty)
        self.assertTrue(snap.backed_up, "「原本没东西」是一种成功的备份结果")
        self.assertEqual(snap.describe(), "原本为空")

    def test_no_clear_support_degrades_to_no_restore_with_an_explanation(self):
        """探不到 --clear 就**不许假设它有**：退化成「不还原 + 把原因说清楚」。"""
        clip = FakeWaylandClipboard({})
        result = self._result(clip, clear_supported=False)
        self.assertIsNone(result.restore_job)
        self.assertIn("--clear", result.clipboard_note)
        self.assertIn("本次会覆盖且不还原", result.clipboard_note)
        self.assertIn("CAPSPLUS_VOICE_BACKEND=manual", result.clipboard_note)
        self.assertEqual([c for c in clip.joined if "wl-copy" in c], ["/usr/bin/wl-copy"],
                         "退化分支除了写上屏文本那一次，一条多余的写都不许发（更不许写空串）")
        self.assertEqual(clip.types(), ("text/plain;charset=utf-8", "text/plain"),
                         "剪贴板里此刻还是我们要贴的那段，没被空串或别的东西盖掉")

    def test_probe_reads_clear_support_from_the_help_text_only(self):
        """`--clear` 的能力只能来自 `wl-copy --help`，不许写死版本号。"""
        with_clear = INJ.run_probe(which=lambda n: "/usr/bin/" + n,
                                   help_runner=lambda a: (0, "-c, --clear  clear the clipboard"),
                                   binary_reader=lambda p: INJ.read_usage_markers("/x", reader=lambda q: b""),
                                   daemon_probe=lambda: (True, "可达"), header="/no/such")
        self.assertTrue(with_clear.wl_copy_clear)
        without = INJ.run_probe(which=lambda n: "/usr/bin/" + n,
                                help_runner=lambda a: (0, "--primary only"),
                                binary_reader=lambda p: b"",
                                daemon_probe=lambda: (True, "可达"), header="/no/such")
        self.assertFalse(without.wl_copy_clear)
        self.assertFalse(without.ran, "注入了假件还跑了真命令 = 在动用户的机器")

    def test_this_machine_really_has_clear(self):
        """探测结论必须等于本机真相：读**真装的** wl-copy 的 --help（只读，不注入）。"""
        found = shutil.which("wl-copy")
        if not found:
            self.skipTest("这台机没装 wl-copy（CI 上正常）")
        proc = subprocess.run([found, "--help"], capture_output=True, text=True, timeout=5)
        blob = (proc.stdout or "") + (proc.stderr or "")
        p = INJ.run_probe(which=lambda n: found if n == "wl-copy" else None,
                          help_runner=lambda a: (0, blob),
                          binary_reader=lambda q: b"",
                          daemon_probe=lambda: (False, "本条不看 daemon"), header="/no/such")
        self.assertEqual(p.wl_copy_clear, "--clear" in blob,
                         "探测口径必须是「--help 里有没有 --clear」")


# ── 判据 4：隐私 —— 快照一个字节都不许落盘 / 进日志 / 进事件帧 ──────────────────
class TestSnapshotNeverHitsDisk(unittest.TestCase):
    def _server(self, clip, backend="clipboard"):
        from capsplus import voice_worker as W

        srv = W.Server(run_command=clip, review_ui=False)
        srv.backend = backend
        srv.probe = lambda: probe()
        srv._events = []
        srv.broadcast = srv._events.append
        srv.after_ms = lambda ms, fn: None          # 测试手动放行，不用真定时器
        return srv

    def test_text_snapshot_is_absent_from_disk_log_frames_and_summaries(self):
        tmp = str(_sandbox_dir("privacy"))
        try:
            with env(CAPSPLUS_STATE_DIR=tmp):
                secret = CARD.encode("utf-8")
                clip = FakeWaylandClipboard({"text/plain;charset=utf-8": secret,
                                             "text/plain": secret})
                srv = self._server(clip)
                with env():
                    result = INJ.inject(OUR_TEXT, backend="clipboard",
                                        probe=probe(), runner=clip)
                srv.arm_clipboard_restore(result.restore_job, result.clipboard_note)
                srv.run_clipboard_restore()

                self.assertTrue(srv.clipboard_summary.startswith("已还原"),
                                srv.clipboard_summary)
                self.assertEqual(clip.clip["text/plain;charset=utf-8"], secret,
                                 "快照根本没经过这条链的话，下面的断言都是空的")

                hits = [str(p) for p in sorted(Path(tmp).rglob("*"))
                        if p.is_file() and secret in p.read_bytes()]
                self.assertEqual(hits, [], f"剪贴板内容落到这些文件里了：{hits}")
                if P.log_path().exists():
                    self.assertNotIn(CARD, P.log_path().read_text(encoding="utf-8",
                                                                  errors="replace"))
                self.assertNotIn(CARD, json.dumps(srv._events, ensure_ascii=False),
                                 "事件帧里不许带剪贴板内容")
                for text in (srv.clipboard_summary, result.clipboard_note,
                             " ".join(clip.joined)):
                    self.assertNotIn(CARD, text, f"摘要或命令行里漏了内容：{text!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_binary_snapshot_only_travels_through_stdin(self):
        clip = FakeWaylandClipboard({"image/png": PNG})
        with env():
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(), runner=clip)
        INJ.perform_restore(result.restore_job, clip)
        for argv, _inp in clip.calls:
            for one in argv:
                self.assertNotIn("\x89PNG", one, f"快照漏进 argv：{one!r}")
        payloads = [inp for inp in clip.payloads() if inp]
        self.assertEqual(payloads, [OUR_TEXT.encode("utf-8"), PNG],
                         "只有两处走 stdin：写上屏文本、还原快照字节")
        self.assertNotIn(PNG.decode("latin-1"), result.clipboard_note)

    def test_step_describe_never_contains_the_payload(self):
        job = INJ.RestoreJob(snapshot=INJ.Snapshot(mime="text/plain;charset=utf-8",
                                                   data=CARD.encode("utf-8")),
                             ours=[b"x"], wl_copy="/usr/bin/wl-copy",
                             wl_paste="/usr/bin/wl-paste")
        for step in INJ.restore_steps(job):
            self.assertNotIn(CARD, step.describe())
            self.assertNotIn(CARD, " ".join(step.argv))


# ── worker 侧：等待由主循环驱动、状态行、单槽合并 ───────────────────────────────
class TestWorkerRestorePlumbing(unittest.TestCase):
    def setUp(self):
        self.tmp = str(_sandbox_dir("worker"))
        self._patch = mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": self.tmp})
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _server(self, clip, backend="clipboard"):
        from capsplus import voice_worker as W

        srv = W.Server(run_command=clip, review_ui=False)
        srv.backend = backend
        srv.probe = lambda: probe()
        srv._events = []
        srv.broadcast = srv._events.append
        srv._deferred = []                              # 代替真定时器：只记账，由测试手动放行
        srv.after_ms = lambda ms, fn: srv._deferred.append((ms, fn))
        return srv

    def _inject(self, srv, text=OUR_TEXT, **clip_env):
        """走真入口 `Session._inject_directly`（后台线程那段）；`clip_env` 用来模拟开关被关掉。"""
        session = srv._ensure_session()
        with env(**clip_env):
            session._inject_directly(text, "")
        return session

    def test_injection_arms_one_mainloop_timer_with_the_named_delay(self):
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        srv = self._server(clip)
        self._inject(srv)
        self.assertEqual([ms for ms, _fn in srv._deferred], [INJ.CLIPBOARD_SETTLE_MS],
                         "还原的等待必须走 after_ms（主循环计时），时长是那个具名常量")
        self.assertEqual(clip.joined.count("/usr/bin/wl-copy"), 1,
                         "到点之前不许有任何还原命令")
        self.assertIn("已备份", srv.clipboard_summary)

    def test_firing_the_timer_is_what_makes_the_restore_happen(self):
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        srv = self._server(clip)
        self._inject(srv)
        self.assertNotIn("已还原", srv.clipboard_summary)
        _ms, fn = srv._deferred[0]
        fn()                                            # 模拟主循环到点
        self.assertTrue(wait_for(lambda: "已还原" in srv.clipboard_summary),
                        srv.clipboard_summary)
        self.assertEqual(clip.types(), ("text/html",))

    def test_restore_commands_do_not_run_on_the_main_thread(self):
        """到点回调只做一件事：把活交给后台线程。子进程读写回到主线程 = 又一个「无响应」的形状。

        注意只看**定时器放行之后**的那些命令：注入阶段的快照读是测试自己在主线程里调的，
        把它算进来这条判据就永远红（假绿的反面：假红）。
        """
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        srv = self._server(clip)
        self._inject(srv)
        _ms, fn = srv._deferred[0]
        split = len(clip.calls)
        fn()
        self.assertTrue(wait_for(lambda: "已还原" in srv.clipboard_summary),
                        srv.clipboard_summary)
        after = [(argv, clip.threads[i]) for i, (argv, _inp) in enumerate(clip.calls) if i >= split]
        self.assertTrue(after, "到点之后一条命令都没跑 = 还原根本没发生")
        checks = [(argv, on_main) for argv, on_main in after
                  if "wl-paste" in argv[0] and "--type" in argv and "text/html" in argv]
        self.assertTrue(checks, f"没看到还原前的现值检查：{[a for a, _ in after]}")
        self.assertTrue(all(not on_main for _argv, on_main in checks),
                        "还原的读写跑在主线程 = 主循环被子进程堵住")

    def test_nothing_is_armed_when_the_switch_is_off(self):
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        srv = self._server(clip)
        self._inject(srv, **{INJ.RESTORE_ENV: "0"})
        self.assertEqual(srv._deferred, [], "开关关着还挂定时器 = 白等一场")
        # 只判"没有 `--list-types` 快照读"：回读自证（判自己刚写的那段）与还原开关无关，
        # 把它一起判死等于逼代码放弃自证（2026-10-19）。
        self.assertEqual([c for c in clip.joined if "--list-types" in c], [],
                         "开关关着还去列类型读用户的剪贴板 = 多一次不必要的数据访问")
        self.assertIn("开关关着", srv.clipboard_summary)

    def test_slot_holds_the_oldest_snapshot_across_two_utterances(self):
        clip = FakeWaylandClipboard({"text/html": "<b>原物</b>".encode("utf-8")})
        srv = self._server(clip)
        self._inject(srv, "第一句")
        _ms, first_fn = srv._deferred[0]
        self._inject(srv, "第二句")                     # 第一次还没到点
        self.assertEqual(srv._clip_job.snapshot.mime, "text/html")
        self.assertEqual(len(srv._clip_job.ours), 2, "两次写的文本都要认作「我们的」")
        first_fn()                                      # 到点：只还原一次，还原成最早那份
        # 到点回调只把活交给**后台线程**（形状判据见下一条），所以这里必须等它干完：
        # 立刻断言等于赌线程调度（真栽过：同一条判据在改动前后各偏向一边）。
        self.assertTrue(wait_for(lambda: clip.clip.get("text/html") is not None),
                        f"到点后没还原成最初那份：{clip.joined}")
        self.assertEqual(clip.clip.get("text/html"), "<b>原物</b>".encode("utf-8"))
        self.assertIsNone(srv._clip_job)

    def test_merged_snapshot_bytes_are_released(self):
        """合并时新快照是中间态，必须**当场丢掉字节**（不留在常驻 worker 内存里）。"""
        clip = FakeWaylandClipboard({"image/png": PNG})
        srv = self._server(clip)
        self._inject(srv, "第一句")
        kept = srv._clip_job                        # 第一次那份才是用户的原物
        with env():
            second = INJ.inject("第二句", backend="clipboard", probe=probe(), runner=clip)
        self.assertIsNotNone(second.restore_job)
        srv.arm_clipboard_restore(second.restore_job, second.clipboard_note)
        self.assertIs(srv._clip_job, kept, "槽位里必须还是最早那份，不能被中间态换掉")
        self.assertIsNone(second.restore_job.snapshot.data,
                          "被丢弃的那份快照还留着字节 = 用户的原物在内存里多存了一份")
        self.assertEqual(srv._clip_job.snapshot.data, PNG)
        self.assertEqual(len(srv._clip_job.ours), 2)

    def test_later_firing_is_idempotent_not_a_second_overwrite(self):
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        srv = self._server(clip)
        self._inject(srv)
        _ms, fn = srv._deferred[0]
        fn()
        before = len(clip.calls)
        srv.run_clipboard_restore()                     # 迟到的一次：槽位已空
        self.assertEqual(len(clip.calls), before, "重复还原 = 又往用户剪贴板上盖了一次")

    def test_status_frame_carries_the_clipboard_line(self):
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        srv = self._server(clip)
        session = self._inject(srv)
        frame = srv._status_frame()
        self.assertEqual(frame["kind"], "state")
        self.assertIn("clipboard", frame, "status 里看不出上次是否还原 = 这条要求没做")
        self.assertIn("已备份", frame["clipboard"])
        _ms, fn = srv._deferred[0]
        fn()
        # 同上：到点的还原在后台线程里跑，"到点"不等于"已经干完"，等它落地再断言。
        self.assertTrue(wait_for(lambda: "已还原" in srv._status_frame()["clipboard"]),
                        srv._status_frame()["clipboard"])
        self.assertIn("已还原", srv._status_frame()["clipboard"])
        self.assertNotIn(OUR_TEXT, json.dumps(srv._status_frame(), ensure_ascii=False),
                         "状态帧里不许带上屏的文本（既有隐私语义不许回退）")
        self.assertEqual(session.state, "idle")

    def test_failed_injection_keeps_the_text_for_manual_paste(self):
        """粘贴那步失败 ⇒ 剪贴板里那段文本是用户唯一还能救回来的东西，**不许还原**。"""
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})

        def failing(argv, **kw):
            if os.path.basename(argv[0]) == "ydotool":
                return _proc(3, err=b"failed to connect socket")
            return clip(argv, **kw)

        srv = self._server(clip)
        srv.run_command = failing
        session = srv._ensure_session()
        with env():
            session._inject_directly(OUR_TEXT, "")
        self.assertIn("不还原", srv.clipboard_summary)
        self.assertIn("还留在剪贴板里", srv.clipboard_summary)
        self.assertEqual(clip.types(), ("text/plain;charset=utf-8", "text/plain"))
        self.assertIsNone(srv._clip_job)
        self.assertEqual(srv._deferred, [],
                         "注入失败还挂还原定时器 = 把用户能自己粘出去的东西盖掉")

    def test_probe_report_names_the_backup_layer(self):
        """status 的探测段要能看出备份能力（缺 wl-paste / 不认 --clear 都得看得见）。"""
        lines = " ".join(probe().report_lines())
        self.assertIn("wl-paste", lines)
        self.assertIn("--clear 有", lines)
        missing = " ".join(probe(wl_paste=None).report_lines())
        self.assertIn("备份不了", missing)


class TestRestoreRunsOnTheRealMainLoop(unittest.TestCase):
    """真 GLib 主循环版：等待由 `timeout_add` 计时，**主循环在等的这段时间里一直在转**。

    这条是「剪贴板还原把主线程睡过去」那个事故的预防针 —— 上一轮语音链就是因为主线程
    `time.sleep` 等注入，mutter 的 ping 没人应答，用户的桌面弹了「无响应」。
    这里不建任何窗口（GLib 主循环本身不需要显示服务），所以有 gi 的机器都能跑；
    没装 PyGObject 的机器（CI 主 job）显式 skip，不算绿。
    """

    def setUp(self):
        from capsplus import voice_worker as W
        self.W = W
        self.glib = W._glib()
        # 这一类的 arm/run **不在 `env()` 里**（循环跑到哪算哪），所以状态目录必须在 setUp 就钉住：
        # 忘了就会往用户的 `~/.local/state/capsplus/voice.log` 里追加行（本轮真犯过）。
        self._patch = mock.patch.dict(os.environ,
                                      {"CAPSPLUS_STATE_DIR": str(_sandbox_dir("mainloop"))})
        self._patch.start()
        self.addCleanup(self._patch.stop)
        if self.glib is None:
            self.skipTest("这台机没有 PyGObject：`after_ms` 退化成 threading.Timer，"
                          "主循环这条判据在这台机上无从验起（CI 主 job 正常）")

    def test_the_wait_is_a_timer_not_a_sleep_and_the_loop_keeps_turning(self):
        clip = FakeWaylandClipboard({"text/html": "用户的原文".encode("utf-8")})
        srv = self.W.Server(run_command=clip, review_ui=False)
        srv.backend = "clipboard"
        srv.probe = lambda: probe()
        srv._events = []
        srv.broadcast = srv._events.append
        with env():
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(), runner=clip)
        self.assertIsNotNone(result.restore_job)

        loop = self.glib.MainLoop()
        srv._loop = loop
        ticks = []
        armed = []
        split = []
        deadline = time.monotonic() + 8.0        # 硬兜底：判据**不许**把整套拖死（本轮真挂过一次）

        def arm():                                # 在循环**里**登记 ⇒ after_ms 走真 timeout_add
            armed.append(time.monotonic())
            split.append(len(clip.calls))         # 之前的命令是注入阶段的，不算还原
            srv.arm_clipboard_restore(result.restore_job, result.clipboard_note)
            return False

        def poll() -> bool:
            ticks.append(time.monotonic())
            if "已还原" in srv.clipboard_summary or time.monotonic() > deadline:
                loop.quit()
                return False
            return True

        # ⚠ 这里用 `timeout_add(1)` 而不是 `idle_add`：本轮实测 **idle 源会被更高优先级的挂起事件
        # 饿死** —— 同一进程里只要还加载了 `tests/test_voice_mainloop.py`（它 import gi → Gtk），
        # `arm` 一次都没被派发，`poll` 里的 `armed[0]` 抛 IndexError 被 PyGI 吃掉、循环永不退出，
        # 整套测试卡死 45 秒以上。生产代码里 `Server.post` 用的正是 idle_add —— 这条差异已写进交付回报。
        self.glib.timeout_add(1, arm)
        self.glib.timeout_add(20, poll)           # 20ms 一次的心跳：循环没转就数不出 tick
        loop.run()
        srv._loop = None

        self.assertTrue(armed, "arm 没进主循环 ⇒ 计时这条验不了（原因见上面那条饿死注释）")
        self.assertIn("已还原", srv.clipboard_summary, srv.clipboard_summary)
        self.assertGreaterEqual(len(ticks), 3,
                                f"等待期间主循环只转了 {len(ticks)} 次 ⇒ 主线程被堵住了")
        tail = clip.calls[split[0]:]
        stamps = clip.stamps[split[0]:]
        threads = clip.threads[split[0]:]
        checks = [(argv, stamp, on_main)
                  for (argv, _inp), stamp, on_main in zip(tail, stamps, threads)
                  if "wl-paste" in argv[0] and "text/html" in argv]
        self.assertTrue(checks, f"到点之后没做现值检查：{clip.joined[split[0]:]}")
        self.assertGreaterEqual(checks[-1][1] - armed[0],
                                INJ.CLIPBOARD_SETTLE_MS / 1000.0 * 0.6,
                                "没等够 CLIPBOARD_SETTLE_MS 就动手还原 = 粘贴可能被抽走")
        self.assertTrue(all(not on_main for _a, _s, on_main in checks),
                        "还原的读写回到了主线程")


class TestSandboxGuard(unittest.TestCase):
    """护栏判据：本模块**默认**就不可能写到用户的真状态目录 —— 少一处都算没做到。

    为什么专门钉一条：本轮真的犯过一次（某个类忘了指 `CAPSPLUS_STATE_DIR`，`P.log_line`
    就往 `~/.local/state/capsplus/voice.log` 追加了 32 行）。把护栏做成 `env()` 的默认值之后，
    这条判据保证那个默认值不会被后来的人顺手删掉。
    """

    @staticmethod
    def _server(clip):
        from capsplus import voice_worker as W
        srv = W.Server(run_command=clip, review_ui=False)
        srv.backend = "clipboard"
        srv.probe = lambda: probe()
        srv.broadcast = lambda frame: None
        srv.after_ms = lambda ms, fn: None
        return srv

    def test_env_helper_pins_the_state_dir_into_the_sandbox(self):
        with env():
            self.assertTrue(str(P.state_dir()).startswith(str(SANDBOX)), P.state_dir())
            self.assertTrue(str(P.log_path()).startswith(str(SANDBOX)), P.log_path())

    def test_the_real_home_state_dir_is_never_the_target(self):
        real = Path.home() / ".local" / "state" / "capsplus"
        with env():
            self.assertNotEqual(P.log_path(), real / "voice.log",
                                "`env()` 的默认值被改掉了 ⇒ 用例又会往用户的真日志里写")

    def test_a_full_chain_writes_its_log_inside_the_sandbox_only(self):
        clip = FakeWaylandClipboard({"text/html": "原文".encode("utf-8")})
        with env():
            srv = self._server(clip)
            result = INJ.inject(OUR_TEXT, backend="clipboard", probe=probe(), runner=clip)
            srv.arm_clipboard_restore(result.restore_job, result.clipboard_note)
            srv.run_clipboard_restore()
            log = P.log_path()
            self.assertTrue(str(log).startswith(str(SANDBOX)), log)
            self.assertTrue(log.exists(), "沙箱里没有日志 = 这条链没真跑过，上面两条都是空的")
            # 2026-10-17 起这行是**可解析**的 `restore_diag branch=… still_ours=…`（散文行
            # 「剪贴板还原 …」已经废掉）：断言它落在 `restored` 分支，比原来只认"有这么一行"更硬。
            self.assertIn("restore_diag branch=restored",
                          log.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
