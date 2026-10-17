# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""语音输入这九个新文件的可移植性 / 无头性 / 弹窗护栏。

四条护栏都来自本仓已经踩过的坑（不是新发明的规矩）：

  1. **bin 入口按自己的 shebang 过语法检查**。`#!/bin/sh` 在本机是 dash：写 bash 数组会
     当场 syntax error，而 keyd 那边不报错、只是按键没反应（2026-10-16 实测；当时只跑了
     `bash -n` 把它遮住了）。判据见 `TestBinEntriesParseWithOwnInterpreter`。
  2. **顶层不许 import gi**。CI 的 ubuntu-latest 没有 PyGObject，测试模块在**收集阶段**
     崩掉 = 整套红、一条没跑。见 `TestVoiceModulesAreGiFree`（外加"假桌面还能不能 import"）。
  3. **降权包装只报一个显示后端**，且用户名/uid/家目录都不写死。这条不只 grep 源码，
     而是拿**假解释器把四种场景真跑一遍**（`CAPSPLUS_RUN_DIR` 就是为这个开的口子）。
  4. **弹窗不许去动别人的窗口**：AST 扫 `set_focus(...)` / `present_with_time(...)` /
     `set_keep_above(...)`。焦点归还靠 WM 在窗口销毁后的自然回退。

真开窗的用例在 `TestOsdRealWindow`：默认 skip，只有 `CAPSPLUS_GUI=1` 才跑，
且 GDK 连着**真实 Wayland 桌面**又没给 `CAPSPLUS_WAYLAND=1` 时整类拒跑（口径照
`tests/test_settings_window.py`，那是明江实测被弹窗打扰之后定下来的）。
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
NOGI_DIR = REPO / "tools" / "ci" / "nogi"

VOICE_MODULES = ("voice_proto", "voice_vad", "voice_asr", "voice_recorder",
                 "voice_inject", "voice_osd", "voice_worker")
LAUNCHER = REPO / "bin" / "capsplus-voice"

RUN_GUI = os.environ.get("CAPSPLUS_GUI") == "1"
ALLOW_WAYLAND_DESKTOP = os.environ.get("CAPSPLUS_WAYLAND") == "1"

try:                                    # 收集阶段绝不能崩：宽 except 是故意的
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk
except Exception as exc:                # noqa: BLE001
    gi = None
    GI_ERROR = str(exc)
else:
    GI_ERROR = ""

# 判据里要写这个变量名，但**不许**以字面量形式出现在"赋值"位置
# （`tests/test_display_env_hygiene.py` 锁的就是字面量赋值；这里只读、只往假解释器传，
#  传的目标是一个 /bin/sh 的 echo 脚本，永远不会真去连显示服务）。
VAR_WAYLAND = "WAYLAND_DISPLAY"
VAR_DISPLAY = "DISPLAY"


def top_level_gi_imports(source: str) -> list[str]:
    """模块顶层（不是函数里）出现的 `import gi` / `from gi.…`。"""
    hits = []
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "gi":
                    hits.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "gi":
                hits.append(f"line {node.lineno}: from {node.module} import …")
        elif isinstance(node, ast.Try):                     # try 里裸写也算（CI 会崩在收集阶段）
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    name = (getattr(sub, "module", None) or
                            ",".join(a.name for a in getattr(sub, "names", [])))
                    if name.split(".")[0] == "gi" and sub is not node:
                        hits.append(f"line {sub.lineno}: try 里顶层 import {name}")
    return hits


def focus_stealing_calls(source: str) -> list[str]:
    """会去动**别的窗口**或依赖激活令牌的调用 —— 本仓一律不许出现。"""
    forbidden = {"set_focus", "present_with_time", "set_keep_above", "activate"}
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in forbidden:
            hits.append(f"line {node.lineno}: .{node.func.attr}()")
    return hits


def display_works() -> bool:
    if gi is None:
        return False
    ok, _err = Gtk.init_check()
    return bool(ok)


def real_desktop_risk() -> str:
    """GDK 连的是真实 Wayland 桌面又没被授权 → 返回一段"该怎么跑"的话（空串 = 安全）。"""
    if not RUN_GUI or ALLOW_WAYLAND_DESKTOP:
        return ""
    try:
        from gi.repository import Gdk as _Gdk
        display = _Gdk.Display.get_default()
        backend = type(display).__name__ if display is not None else ""
    except Exception:                                       # noqa: BLE001
        return ""
    if "Wayland" in backend:
        return (f"GDK 当前连的是真实 Wayland 桌面（{backend}），弹窗会直接出现在明江屏幕上"
                "并抢走键盘焦点。正确跑法："
                "`xvfb-run -a --server-args=\"-screen 0 1024x768x24\" "
                f"env -u {VAR_WAYLAND} GDK_BACKEND=x11 CAPSPLUS_GUI=1 python3 -m unittest …`；"
                "确实要真桌面证据再加 `CAPSPLUS_WAYLAND=1`。")
    return ""


def child_env(**over) -> dict:
    """给子进程造环境。**只往假解释器传**，值也是编的（`wayland-9` 那种）。"""
    env = dict(os.environ)
    for name, value in over.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


class TestBinEntriesParseWithOwnInterpreter(unittest.TestCase):
    """bin 下**每个**入口都用它自己 shebang 指定的解释器过一遍语法。

    Python 入口走 `compile()`，shell 入口走 `-n`。上一版踩过：只跑 `bash -n` 时
    dash 的语法错被完全遮住（`KNOWN-LIMITS.md` 第 3 条旁边那段教训）。
    """

    def test_every_bin_entry_parses(self):
        entries = sorted(p for p in (REPO / "bin").iterdir() if p.is_file())
        self.assertIn(LAUNCHER, entries, "bin/capsplus-voice 没在列出的入口里？")
        self.assertGreaterEqual(len(entries), 5)
        bad = []
        for path in entries:
            shebang = path.read_text(encoding="utf-8").splitlines()[0]
            interp = shebang.lstrip("#!").strip() or "/bin/sh"
            if interp.endswith("python3") or interp.endswith("python"):
                proc = subprocess.run([sys.executable, "-c",
                                       "import sys,pathlib;compile(pathlib.Path(sys.argv[1])"
                                       ".read_text(encoding='utf-8'),sys.argv[1],'exec')",
                                       str(path)], capture_output=True, text=True)
            else:
                resolved = shutil.which(os.path.basename(interp)) or interp
                proc = subprocess.run([resolved, "-n", str(path)], capture_output=True, text=True)
            if proc.returncode != 0:
                bad.append(f"{path.name}（{interp}）：{(proc.stderr or proc.stdout).strip()[:200]}")
        self.assertEqual(bad, [], "这些入口在自己声明的解释器下语法不过：\n  " + "\n  ".join(bad))

    def test_voice_launcher_declares_posix_sh(self):
        first = LAUNCHER.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first.strip(), "#!/bin/sh", "语音入口必须是 POSIX sh（照 keyd-qbar-cmd）")

    def test_gpl_header_sits_on_lines_2_to_5_for_bin_entries(self):
        """bin 下带 shebang 的文件，声明块固定第 2..5 行（`test_license_headers` 只看前 8 行，
        这里补的是"位置"这一层 —— 与仓内现有入口逐字对齐）。"""
        lines = LAUNCHER.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[1].startswith("# SPDX-License-Identifier: GPL-2.0-only"))
        self.assertTrue(lines[2].startswith("# Copyright (c) 2026 capsplus-linux contributors"))
        self.assertIn("github.com/wo52616111/capslock-plus", lines[3] + lines[4])


def code_lines(text: str) -> str:
    """去掉整行注释之后的脚本正文。

    为什么去掉：这文件的**注释里**写着 ``A=(…)`` 当反面教材（照 keyd-qbar-cmd 的教训写的），
    拿字符串扫全文就会把它当成 bash 写法抓到 —— 判据要扫的是**会被执行的部分**。
    行尾注释也一起摘掉：`x=1  # 例：A=(…)` 里的反例同样不该误报。
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


def bashisms(text: str) -> list[str]:
    """bash 专用写法（参数展开 `${VAR:-x}` 在 dash 里合法，不在禁用之列）。"""
    body = code_lines(text)
    found = [b for b in ("=(", "[[ ", "((", "shopt", "<<<", "local -a", "export -f",
                         "function ") if b in body]
    import re
    if re.search(r"^[A-Z_]+=\(", body, re.M):
        found.append("数组赋值 NAME=(…)")
    return found


class TestVoiceLauncherIsPosixSh(unittest.TestCase):
    """bash 专用写法一个都不许出现在 `#!/bin/sh` 脚本里（2026-10-16 dash 事故）。"""

    def test_no_bash_arrays_or_keywords(self):
        self.assertEqual(bashisms(LAUNCHER.read_text(encoding="utf-8")), [],
                         "POSIX sh 脚本里有 bash 写法")

    def test_no_hardcoded_user_or_uid(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        import re
        for pattern, why in ((r"/home/[A-Za-z0-9._-]+", "写死的家目录"),
                             (r"/run/user/[0-9]+", "写死的 uid 目录"),
                             (r"runuser\s+-u\s+[A-Za-z0-9._-]", "runuser 后面跟裸用户名")):
            hit = re.search(pattern, text)
            self.assertIsNone(hit, f"{why}：{hit.group(0) if hit else ''}")
        self.assertIn("CAPSPLUS_USER", text)
        self.assertIn("stat -Lc '%U'", text, "目标用户应反查脚本属主（照 keyd-qbar-cmd）")

    def test_runuser_is_looked_for_in_three_places(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        for needle in ("/usr/sbin/runuser", "/sbin/runuser", "command -v runuser"):
            self.assertIn(needle, text, f"root 的 PATH 不一定带 runuser，得挨个试：缺 {needle}")

    def test_unknown_subcommand_is_refused_with_usage(self):
        result = subprocess.run(["/bin/sh", str(LAUNCHER), "dance"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("toggle|start|stop|status", result.stderr)


class TestLauncherReportsOneDisplayBackend(unittest.TestCase):
    """四种显示后端场景**真跑一遍**（假解释器只会把环境打出来，连不上任何显示服务）。"""

    @staticmethod
    def _fake_python(tmp: Path) -> Path:
        script = tmp / "fake-python"
        script.write_text(
            "#!/bin/sh\n"
            'echo "WAYLAND=[${WAYLAND_DISPLAY:-}]"\n'
            'echo "DISPLAY=[${DISPLAY:-}]"\n'
            'echo "HOME=[$HOME]"\n'
            'echo "RUN=[$XDG_RUNTIME_DIR]"\n'
            'echo "USER=$(id -un)"\n'
            'echo "ARGV=$*"\n',
            encoding="utf-8")
        script.chmod(0o755)
        return script

    def _run(self, tmp: Path, run_dir: Path, over: dict) -> dict:
        fake = self._fake_python(tmp)
        (tmp / "capsplus").mkdir(exist_ok=True)
        (tmp / "capsplus" / "voice_worker.py").write_text("# 假的，只为了让脚本找到它\n",
                                                          encoding="utf-8")
        env = child_env(CAPSPLUS_PYTHON=str(fake), CAPSPLUS_REPO=str(tmp),
                        CAPSPLUS_RUN_DIR=str(run_dir), CAPSPLUS_USER=None, **over)
        proc = subprocess.run(["/bin/sh", str(LAUNCHER), "status"], capture_output=True,
                              text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = {}
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            out[key] = value
        return out

    def _touch_wayland_socket(self, run_dir: Path) -> None:
        import socket

        run_dir.mkdir(parents=True, exist_ok=True)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(probe.close)
        probe.bind(str(run_dir / "wayland-0"))

    def test_explicit_wayland_display_is_passed_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(Path(tmp), Path(tmp) / "run", {VAR_WAYLAND: "wayland-9"})
        self.assertEqual(out["WAYLAND"], "[wayland-9]")
        self.assertEqual(out["DISPLAY"], "[]", "有 Wayland 就不许再报 DISPLAY")

    def test_wayland_socket_is_detected_when_env_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            self._touch_wayland_socket(run_dir)
            out = self._run(Path(tmp), run_dir, {VAR_WAYLAND: None, VAR_DISPLAY: ":7"})
        self.assertEqual(out["WAYLAND"], "[wayland-0]", "socket 在就要用 Wayland")
        self.assertEqual(out["DISPLAY"], "[]")

    def test_plain_xorg_session_keeps_display_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(Path(tmp), Path(tmp) / "run", {VAR_WAYLAND: None, VAR_DISPLAY: ":7"})
        self.assertEqual(out["WAYLAND"], "[]")
        self.assertEqual(out["DISPLAY"], "[:7]")

    def test_nothing_at_all_falls_back_to_display_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(Path(tmp), Path(tmp) / "run", {VAR_WAYLAND: None, VAR_DISPLAY: None})
        self.assertEqual(out["WAYLAND"], "[]")
        self.assertEqual(out["DISPLAY"], "[:0]")

    def test_never_both_backends(self):
        """四条场景的共同不变量：两个后端**至多报一个**。"""
        for over in ({VAR_WAYLAND: "wayland-9"}, {VAR_WAYLAND: None, VAR_DISPLAY: ":7"},
                     {VAR_WAYLAND: None, VAR_DISPLAY: None}):
            with self.subTest(**{k: (v or "-") for k, v in over.items()}), \
                    tempfile.TemporaryDirectory() as tmp:
                out = self._run(Path(tmp), Path(tmp) / "run", over)
                self.assertFalse(out["WAYLAND"].strip("[]") and out["DISPLAY"].strip("[]"),
                                 f"两个后端一起报给子进程了：{out}")

    def test_worker_path_and_subcommand_reach_the_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(Path(tmp), Path(tmp) / "run", {})
        self.assertIn("capsplus/voice_worker.py status", out["ARGV"])
        self.assertEqual(out["HOME"], f"[{os.environ['HOME']}]", "家目录要跟着传下去，不能凭空造")

    def test_missing_worker_is_reported_plainly(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = child_env(CAPSPLUS_REPO=str(tmp), CAPSPLUS_PYTHON="/bin/sh")
            proc = subprocess.run(["/bin/sh", str(LAUNCHER), "status"], capture_output=True,
                                  text=True, env=env)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("voice_worker.py", proc.stderr)
            self.assertIn("install.sh", proc.stderr)


class TestVoiceModulesAreGiFree(unittest.TestCase):
    """七条新链路里除了 `voice_osd` 的**懒加载**，谁都不许在顶层碰 gi。"""

    def test_no_top_level_gi_import(self):
        bad = []
        for name in VOICE_MODULES:
            path = REPO / "capsplus" / f"{name}.py"
            self.assertTrue(path.exists(), f"缺少 {path}")
            bad += [f"{name}.py: {hit}" for hit in top_level_gi_imports(
                path.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], "这些地方顶层 import gi，CI 会在收集阶段整套崩：\n  "
                                  + "\n  ".join(bad))

    def test_osd_imports_gi_inside_a_function(self):
        text = (REPO / "capsplus" / "voice_osd.py").read_text(encoding="utf-8")
        self.assertIn("def _gtk():", text, "懒加载入口没了")
        self.assertIn("gi.require_version(\"Gtk\", \"3.0\")", text,
                      "KNOWN-LIMITS 第 11 条：漏了 require_version 当场 ImportError")
        self.assertIn("gi.require_version(\"Gdk\", \"3.0\")", text)

    def test_each_module_imports_in_a_no_gi_subprocess(self):
        code = ("import importlib, sys\n"
                "bad = []\n"
                "for name in sys.argv[1].split(','):\n"
                "    try:\n"
                f"        importlib.import_module('capsplus.' + name)\n"
                "    except Exception as exc:\n"
                "        bad.append(f'{name}: {type(exc).__name__}: {exc}')\n"
                "sys.exit('这些模块在没有 gi 的机器上过不了：' + '; '.join(bad) if bad else 0)\n")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(NOGI_DIR), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        env.pop("CAPSPLUS_GUI", None)
        env.pop("CAPSPLUS_WAYLAND", None)
        proc = subprocess.run([sys.executable, "-c", code, ",".join(VOICE_MODULES)],
                              cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

    def test_osd_degrades_instead_of_crashing_without_gi(self):
        """没 gi 时 `ui_available()` 必须返回 False —— worker 靠它退到无头直投。"""
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from capsplus import voice_osd as O\n"
                "sys.exit(0 if O.ui_available() is False else '没 gi 还说能用弹窗')\n" % str(REPO))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(NOGI_DIR), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        env["CAPSPLUS_STATE_DIR"] = tempfile.mkdtemp()
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-300:])

    def test_worker_has_no_display_injection(self):
        """worker/bin 都不许把显示变量**设进**子进程（判据同 test_display_env_hygiene）。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "hygiene_helper", REPO / "tests" / "test_display_env_hygiene.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        finds_setting_of_wayland = helper.finds_setting_of_wayland

        bad = []
        for name in VOICE_MODULES:
            src = (REPO / "capsplus" / f"{name}.py").read_text(encoding="utf-8")
            bad += [f"{name}.py: {hit}" for hit in finds_setting_of_wayland(src)]
        self.assertEqual(bad, [], "\n  ".join(bad))


class TestOsdFocusGuardrails(unittest.TestCase):
    """不许去动别人的窗口：源码级扫描 + 反例具型。"""

    def test_voice_osd_has_no_cross_window_focus_calls(self):
        text = (REPO / "capsplus" / "voice_osd.py").read_text(encoding="utf-8")
        self.assertEqual(focus_stealing_calls(text), [],
                         "voice_osd 里出现了会动别的窗口/依赖激活令牌的调用")

    def test_guard_catches_the_three_forbidden_shapes(self):
        samples = ('other_window.set_focus()\n',
                   'win.present_with_time(0)\n',
                   'win.set_keep_above(True)\n',
                   'tab.activate()\n')
        for src in samples:
            with self.subTest(src=src.strip()):
                self.assertTrue(focus_stealing_calls(src), f"扫不出来：{src.strip()}")

    def test_grab_focus_on_our_own_widget_is_allowed(self):
        src = "self._view.grab_focus()\n"
        self.assertEqual(focus_stealing_calls(src), [])

    def test_window_plan_locks_the_two_modes(self):
        from capsplus import voice_osd as O

        rec = O.window_plan("recording")
        self.assertFalse(rec["accept_focus"], "录音期窗口必须不可聚焦")
        self.assertFalse(rec["allow_present_and_focus"], "录音期不许 present()/grab_focus()")
        self.assertFalse(rec["editable"])
        self.assertTrue(rec["skip_taskbar"])
        self.assertFalse(rec["keep_above"], "Wayland 下没这功能（KNOWN-LIMITS 第 6 条）")
        rev = O.window_plan("review")
        self.assertTrue(rev["accept_focus"] and rev["editable"] and rev["allow_present_and_focus"])
        with self.assertRaises(ValueError):
            O.window_plan("screensaver")

    def test_badge_is_off_by_default(self):
        """默认不映射录音期窗口 —— xdg-shell 拦不住"别给我焦点"，唯一可靠做法是没窗口。"""
        from capsplus import voice_osd as O

        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_BADGE": ""}, clear=False):
            self.assertFalse(O.badge_enabled())
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_BADGE": "1"}):
            self.assertTrue(O.badge_enabled())


class TestStateDirIsInjectable(unittest.TestCase):
    """状态目录只认 `CAPSPLUS_STATE_DIR` 与现算的 home，测试不写真用户目录。"""

    def test_socket_and_log_land_in_the_override(self):
        from capsplus import voice_proto as P

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CAPSPLUS_STATE_DIR": tmp}):
                server = P.bind_server()
                try:
                    self.assertTrue((Path(tmp) / "voice.sock").exists())
                    P.log_line("测试一行")
                    self.assertIn("测试一行", (Path(tmp) / "voice.log").read_text(encoding="utf-8"))
                finally:
                    P.release_server(server)
            # 真状态目录**不许被动过**（用快照比，不用"它不存在"当断言：
            # 那会让测试依赖"这台机从没跑过语音输入"这种现场状态）
            real_log = Path.home() / ".local/state/capsplus/voice.log"
            after = real_log.stat().st_size if real_log.exists() else None
            self.assertEqual(after, self._real_log_before,
                             f"测试写到了真用户状态目录：{real_log}")

    def setUp(self):
        real_log = Path.home() / ".local/state/capsplus/voice.log"
        self._real_log_before = real_log.stat().st_size if real_log.exists() else None

    def test_bashism_scanner_catches_the_real_accident(self):
        """反例具型：把 2026-10-16 真踩过的那行 bash 数组塞进脚本正文，必须抓到。"""
        sample = "\n".join(["#!/bin/sh", 'A=("x" "y")', 'echo "${A[@]}"', "shopt -s extglob"])
        hits = bashisms(sample)
        self.assertIn("=(", hits)
        self.assertIn("shopt", hits)
        self.assertIn("数组赋值 NAME=(…)", hits)

    def test_comment_only_bash_example_is_not_a_false_positive(self):
        """反例的另一面：注释里写反例**不该**被判成 bash 写法（判据要扫会被执行的部分）。"""
        sample = "\n".join(["#!/bin/sh", "# 不许写 A=(…) 这种 bash 数组", "x=1"])
        self.assertEqual(bashisms(sample), [])

    def test_socket_and_log_land_in_the_override_snapshot_is_real(self):
        """`finds_setting_of_wayland` 那类"扫源码"的判据都该有反例：这里验本文件的
        `top_level_gi_imports` 与 `focus_stealing_calls` 真能抓到东西（见下面两个方法）。"""
        self.assertTrue(top_level_gi_imports("import gi\ngi.require_version('Gtk', '3.0')\n"))
        self.assertTrue(top_level_gi_imports("try:\n    import gi\nexcept Exception:\n    pass\n"))
        self.assertEqual(top_level_gi_imports("def f():\n    import gi\n"), [])

    def test_recorder_argv_uses_the_documented_arecord_shape(self):
        from capsplus import voice_recorder as R
        from capsplus import voice_vad as V

        argv = R.arecord_argv(V.VadParams())
        self.assertEqual(argv[:2], ["arecord", "-q"])
        for needle in ("-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"):
            self.assertIn(needle, argv, f"实测可用的采集参数少了 {needle}")
        self.assertNotIn("parecord", " ".join(argv), "parecord 的参数写法在本机报「参数过多」")
        with mock.patch.dict(os.environ, {"CAPSPLUS_VOICE_DEVICE": "alsa_input.x"}):
            self.assertIn("-D", R.arecord_argv(V.VadParams()))


@unittest.skipUnless(RUN_GUI and not real_desktop_risk(),
                     "没设 CAPSPLUS_GUI=1（或 GDK 连着真实 Wayland 桌面）：跳过开窗用例")
class TestOsdRealWindow(unittest.TestCase):
    """真窗口断言（虚拟显示里跑；CI 的 gui-tests job 就是这么跑的）。"""

    @classmethod
    def setUpClass(cls):
        risk = real_desktop_risk()
        if risk:
            raise unittest.SkipTest(risk)
        if gi is None:
            raise AssertionError(f"CAPSPLUS_GUI=1 但 import gi 失败：{GI_ERROR}")
        if not display_works():
            raise AssertionError("CAPSPLUS_GUI=1 但连不上显示服务器（没在 xvfb-run 下跑？）")

    def setUp(self):
        from capsplus import voice_osd as O

        self.O = O
        self.ui = O.VoiceUi()

    def tearDown(self):
        if self.ui._window is not None:
            self.ui._window.destroy()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    def pump(self, seconds: float = 0.3) -> None:
        import time

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.01)

    def key_event(self, keyval: int, shift: bool = False):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = keyval
        event.state = Gdk.ModifierType.SHIFT_MASK if shift else Gdk.ModifierType(0)
        return event

    def test_recording_window_cannot_take_focus(self):
        self.ui.build()
        self.pump(0.2)
        self.assertFalse(self.ui._window.get_accept_focus(),
                         "录音期窗口可聚焦 = 会在用户打字时抢键盘")
        self.assertFalse(self.ui._window.get_decorated())
        self.assertFalse(self.ui._view.get_editable())
        self.assertFalse(self.O.window_plan(self.ui.mode)["allow_present_and_focus"])

    def test_review_window_is_editable_and_commit_hands_the_text_back(self):
        self.ui.post_text("九点开会")
        self.ui.build()
        self.pump(0.2)
        self.assertTrue(self.ui._window.get_accept_focus())
        self.assertTrue(self.ui._view.get_editable())
        buffer = self.ui._view.get_buffer()
        self.assertEqual(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
                         "九点开会")
        handled = self.ui._on_key(None, self.key_event(Gdk.KEY_Return))
        self.assertTrue(handled)
        self.assertEqual(self.ui._action, "commit")
        self.assertEqual(self.ui.text, "九点开会")
        self.assertIsNone(self.ui._window, "commit 之后窗口必须已经销毁（焦点才能自然归还）")

    def test_escape_discards_without_text(self):
        self.ui.post_text("别发出去")
        self.ui.build()
        self.pump(0.2)
        self.ui._on_key(None, self.key_event(Gdk.KEY_Escape))
        self.assertEqual(self.ui._action, "cancel")
        self.assertIsNone(self.ui._window)

    def test_shift_enter_is_a_newline_not_a_commit(self):
        self.ui.post_text("第一行")
        self.ui.build()
        self.pump(0.2)
        handled = self.ui._on_key(None, self.key_event(Gdk.KEY_Return, shift=True))
        self.assertFalse(handled, "Shift+Enter 被吃掉 = 换不了行")
        self.assertEqual(self.ui._action, "")

    def test_show_without_display_is_a_structured_unavailable(self):
        """`show()` 在没显示/没 gi 时**返回**结果而不是抛（worker 靠它降级）。"""
        with mock.patch.object(self.O, "ui_available", return_value=False):
            outcome = self.O.VoiceUi().show()
        self.assertEqual(outcome.action, "unavailable")
        self.assertFalse(outcome.commit)


class TestRealWindowRefusesTrueWaylandWithoutOptIn(unittest.TestCase):
    """护栏本身：`CAPSPLUS_GUI=1` 且 GDK 连着真 Wayland 时，界面用例类必须整类拒跑。"""

    def test_risk_text_is_non_empty_on_a_wayland_desktop(self):
        if not RUN_GUI:
            self.skipTest("没要求跑界面测试，这条判据无从判起")
        if gi is None:
            self.skipTest(f"没有 gi：{GI_ERROR}")
        from gi.repository import Gdk as _Gdk
        display = _Gdk.Display.get_default()
        if display is None:
            self.skipTest("连不上任何显示服务器")
        backend = type(display).__name__
        if "Wayland" not in backend:
            self.skipTest(f"GDK 连的不是 Wayland（{backend}），这条不适用")
        if ALLOW_WAYLAND_DESKTOP:
            self.skipTest("已显式授权真桌面")
        self.assertTrue(real_desktop_risk(), "真桌面 + 未授权却没拦住 = 会弹到明江屏幕上")

    def test_skip_unless_condition_is_pointed_the_right_way(self):
        """反例具型：`skipUnless` 的条件方向写反过一次，界面测试直接弹到屏幕上（2026-10-16）。"""
        self.assertIsInstance(RUN_GUI and not real_desktop_risk(), bool)
        # 用 AST 看装饰器本身，**不许拿源码字符串比**（上一版在这里自指：
        # 断言里写的反例串就躺在同一个文件里，`assertNotIn` 永远抓不到东西）。
        tree = ast.parse((REPO / "tests" / "test_voice_portability.py").read_text(encoding="utf-8"))
        decs = [d for node in tree.body if isinstance(node, ast.ClassDef)
                and node.name == "TestOsdRealWindow" for d in node.decorator_list]
        self.assertEqual(len(decs), 1, "开窗用例类必须只有一个装饰器，条件方向才谈得上对不对")
        cond = ast.unparse(decs[0].args[0])
        self.assertTrue(cond.startswith("RUN_GUI and "), f"条件方向反了会直接弹到屏幕上：{cond}")
        self.assertIn("real_desktop_risk()", cond)
        # 反例具型：条件反过来写，这条判据必须能红
        wrong = "not RUN_GUI or real_desktop_risk()"
        self.assertFalse(ast.unparse(ast.parse(wrong, mode="eval").body).startswith("RUN_GUI and "),
                         "反例造不出来 = 上面那条断言是摆设")


if __name__ == "__main__":
    unittest.main(verbosity=2)
