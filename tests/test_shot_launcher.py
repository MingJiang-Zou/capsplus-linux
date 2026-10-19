# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：`bin/capsplus-shot` 这个 **sh 启动器**（2026-10-19 的阻塞 bug）。

背景（真机铁证，别再重新论证）：keyd 的 `command(...)` 以 **root** 执行，而 `capsplus-shot`
要 GTK 窗 + 会话总线。root 直接跑会**静默失败**、与"按了 Caps+F1 没反应"完全对应：

    $ sudo -n env -u XDG_RUNTIME_DIR -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS \\
        capsplus-shot --region
    Authorization required, but no authorization protocol specified
    已取消（没抓图）        rc=0

修法照抄仓里两条先例（`bin/keyd-qbar-cmd`、`bin/capsplus-voice`）：root 就降权 + 注入会话环境，
用户本人直接 exec。这里钉的就是"照抄得对不对"：

* **静态**：POSIX sh（不许 bash 数组 —— 那会让 keyd 那边静默没反应，而 `bash -n` 看不出来）；
  用户名/uid/家目录一律不写死；runuser 三个位置都试；
* **行为**：用**假 `id`**（让启动器以为自己是 root）+ **假 runuser**（把 argv 打出来再 exec 真活儿）
  把 root 那条路真跑一遍 —— **不需要真 root**，也不需要真 Wayland socket；
  断言 exec 的 argv 里 `HOME` / `XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS` **三项都在**、
  显示后端**只出现一个**、四个命令的参数原样透传。
"""

import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

LAUNCHER = REPO / "bin" / "capsplus-shot"
ENTRY = REPO / "capsplus" / "shot_entry.py"

VAR_WAYLAND = "WAYLAND_DISPLAY"
VAR_DISPLAY = "DISPLAY"


def code_lines(text: str) -> str:
    """去掉注释后的正文 —— 本文件的注释里写着 `A=(…)` 当反面教材，扫全文会误报。"""
    out = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


def bashisms(text: str) -> list:
    """bash 专用写法（`${VAR:-x}` 在 dash 里合法，不在禁用之列）。"""
    body = code_lines(text)
    found = [b for b in ("=(", "[[ ", "((", "shopt", "<<<", "local -a", "export -f",
                         "function ") if b in body]
    if re.search(r"^[A-Z_]+=\(", body, re.M):
        found.append("数组赋值 NAME=(…)")
    return found


def child_env(**over) -> dict:
    """干净的子进程环境：显式带上的变量就是被测的那几个，其余尽量摘掉（避免真会话环境漏进来）。"""
    env = dict(os.environ)
    for key in (VAR_WAYLAND, VAR_DISPLAY, "CAPSPLUS_USER", "CAPSPLUS_RUN_DIR", "CAPSPLUS_REPO",
                "CAPSPLUS_PYTHON", "CAPSPLUS_RUNUSER"):
        env.pop(key, None)
    env.update({k: v for k, v in over.items() if v is not None})
    return env


class TestLauncherIsPosixSh(unittest.TestCase):
    def test_shebang_is_posix_sh(self):
        first = LAUNCHER.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first.strip(), "#!/bin/sh",
                         "必须 POSIX sh：dash 下 bash 数组是语法错，而 keyd 那边不报错、只是按键没反应")

    def test_parses_with_the_shell_named_in_its_shebang(self):
        proc = subprocess.run([shutil.which("sh") or "/bin/sh", "-n", str(LAUNCHER)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_bashisms(self):
        self.assertEqual(bashisms(LAUNCHER.read_text(encoding="utf-8")), [],
                         "POSIX sh 脚本里出现了 bash 写法")

    def test_gpl_header_sits_on_lines_2_to_5(self):
        lines = LAUNCHER.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[1].startswith("# SPDX-License-Identifier: GPL-2.0-only"))
        self.assertTrue(lines[2].startswith("# Copyright (c) 2026 capsplus-linux contributors"))
        self.assertIn("github.com/wo52616111/capslock-plus", lines[3] + lines[4])

    def test_no_hardcoded_user_uid_or_home(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        for pattern, why in ((r"/home/[A-Za-z0-9._-]+", "写死的家目录"),
                             (r"/run/user/[0-9]+", "写死的 uid 目录"),
                             (r"runuser\s+-u\s+[A-Za-z0-9._-]+\s", "runuser 后面跟裸用户名")):
            hit = re.search(pattern, text)
            self.assertIsNone(hit, f"{why}：{hit.group(0) if hit else ''}")
        self.assertIn("CAPSPLUS_USER", text)
        self.assertIn("stat -Lc '%U'", text, "目标用户应反查脚本属主（照 keyd-qbar-cmd）")

    def test_runuser_is_looked_for_in_three_places_and_overridable(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        for needle in ("/usr/sbin/runuser", "/sbin/runuser", "command -v runuser"):
            self.assertIn(needle, text, f"root 的 PATH 不一定带 runuser，得挨个试：缺 {needle}")
        self.assertIn("CAPSPLUS_RUNUSER", text, "判据要用假桩捕获 argv，需要一个覆盖口")

    def test_the_python_entry_lives_in_the_repo(self):
        self.assertTrue(ENTRY.exists(), "启动器 exec 的 python 入口必须在仓里")
        self.assertIn("shot_entry.py", LAUNCHER.read_text(encoding="utf-8"))


class TestLauncherArgvAndEnv(unittest.TestCase):
    """把启动器**真跑一遍**：假 `id`（假装是 root）+ 假 runuser（打印 argv 后 exec 真活儿）。"""

    def _fake_tools(self, tmp: pathlib.Path) -> pathlib.Path:
        """造 bin 目录：假 id / 假 runuser / 假 python。返回该目录。"""
        tools = tmp / "tools"
        tools.mkdir(exist_ok=True)

        fake_id = tools / "id"
        fake_id.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  -u) if [ $# -ge 2 ]; then echo \"${FAKE_UID:-1000}\"; else echo \"${FAKE_ROOT_UID:-0}\"; fi ;;\n"
            "  -un) echo \"${FAKE_USER:-nobody}\" ;;\n"
            "  *) echo 0 ;;\n"
            "esac\n", encoding="utf-8")
        fake_id.chmod(0o755)

        runuser = tools / "runuser"
        runuser.write_text(
            "#!/bin/sh\n"
            "echo \"RUNUSER_ARGV=$*\"\n"
            "shift 3\n"          # 丢掉 `-u NAME --`，剩下的就是真正的命令
            "exec \"$@\"\n", encoding="utf-8")
        runuser.chmod(0o755)

        python = tools / "fake-python"
        python.write_text(
            "#!/bin/sh\n"
            "echo \"PY_ARGV=$*\"\n"
            "echo \"HOME=[${HOME:-}]\"\n"
            "echo \"RUN=[${XDG_RUNTIME_DIR:-}]\"\n"
            "echo \"DBUS=[${DBUS_SESSION_BUS_ADDRESS:-}]\"\n"
            "echo \"WAYLAND=[${WAYLAND_DISPLAY:-}]\"\n"
            "echo \"DISPLAY=[${DISPLAY:-}]\"\n", encoding="utf-8")
        python.chmod(0o755)
        return tools

    def _run(self, tmp: pathlib.Path, over: dict, args=("--region",), as_root=True) -> dict:
        tools = self._fake_tools(tmp)
        over = dict(over)
        run_dir = over.pop("CAPSPLUS_RUN_DIR", str(tmp / "run"))     # 允许某个用例换运行目录
        over.setdefault("CAPSPLUS_REPO", str(REPO))
        over.setdefault("CAPSPLUS_RUNUSER", str(tools / "runuser"))
        over.setdefault("CAPSPLUS_PYTHON", str(tools / "fake-python"))
        env = child_env(CAPSPLUS_RUN_DIR=run_dir,
                        FAKE_UID=str(os.getuid()), FAKE_USER=os.environ.get("USER", "nobody"),
                        FAKE_ROOT_UID="0" if as_root else str(os.getuid()), **over)
        env["PATH"] = f"{tools}{os.pathsep}{env.get('PATH', '')}"
        proc = subprocess.run(["/bin/sh", str(LAUNCHER), *args], capture_output=True, text=True,
                              env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, f"启动器退出码 {proc.returncode}：{proc.stderr}")
        out = {}
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            out[key] = value
        return out

    def test_root_path_drops_privileges_and_injects_the_session_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: None, VAR_DISPLAY: None}, as_root=True)
        argv = out["RUNUSER_ARGV"]
        self.assertIn("-u ", argv, "root 路径必须先降权（runuser -u <user> --）：" + argv)
        self.assertIn(" -- ", argv)
        self.assertIn("shot_entry.py", out["PY_ARGV"], "降权后 exec 的必须是 python 入口")
        self.assertIn("--region", out["PY_ARGV"], "参数要原样透传")
        # 三项会话环境一个都不能少（少 DBUS = 信号发不出去 / 抓帧永远超时）
        self.assertTrue(out["HOME"].strip("[]"), "HOME 没传下去")
        self.assertTrue(out["RUN"].strip("[]"), "XDG_RUNTIME_DIR 没传下去")
        self.assertTrue(out["DBUS"].strip("[]"), "DBUS_SESSION_BUS_ADDRESS 没传下去")
        self.assertIn("XDG_RUNTIME_DIR=", argv)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=", argv)

    def test_root_path_argv_is_the_documented_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: "wayland-0"}, as_root=True)
        parts = out["RUNUSER_ARGV"].split()
        self.assertEqual(parts[0], "-u")
        self.assertEqual(parts[2], "--", "必须是 `runuser -u <user> -- env …` 这个形状")
        self.assertIn("env", parts[3])

    def test_user_path_does_not_go_through_runuser(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: "wayland-0"}, as_root=False)
        self.assertNotIn("RUNUSER_ARGV", out, "用户自己敲的时候不该绕 runuser（照 capsplus-voice）")
        self.assertIn("shot_entry.py", out["PY_ARGV"])
        self.assertTrue(out["RUN"].strip("[]"))

    def test_all_four_commands_pass_through(self):
        cases = {
            "--region": ["--region"],
            "--capture": ["--capture", "0", "0", "320", "200", "--out", "/tmp/x.png"],
            "--annotate": ["--annotate", "/tmp/a.png"],
            "--pin": ["--pin", "/tmp/a.png", "--at", "10", "20"],
        }
        for name, args in cases.items():
            with self.subTest(cmd=name), tempfile.TemporaryDirectory() as tmp:
                out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: "wayland-0"}, args=args, as_root=True)
                self.assertIn(" ".join(args), out["PY_ARGV"],
                              f"{name} 的参数在降权那一步丢了：{out['PY_ARGV']}")

    def test_custom_run_dir_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp) / "weird-run"
            out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: None, VAR_DISPLAY: None,
                                                "CAPSPLUS_RUN_DIR": str(run_dir)}, as_root=True)
        self.assertEqual(out["RUN"], f"[{run_dir}]", "CAPSPLUS_RUN_DIR 要生效（判据与特殊 seat 布局都靠它）")

    def test_missing_entry_is_reported_plainly(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = child_env(CAPSPLUS_REPO=str(tmp), CAPSPLUS_PYTHON="/bin/sh")
            proc = subprocess.run(["/bin/sh", str(LAUNCHER), "--region"], capture_output=True,
                                  text=True, env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("shot_entry.py", proc.stderr)
        self.assertIn("install.sh", proc.stderr)


class TestOneDisplayBackend(unittest.TestCase):
    """显示后端**只报一个**（同时给两个会让 Chromium 系另起实例 / GTK 连错后端）。"""

    def _run(self, tmp: pathlib.Path, over: dict) -> dict:
        tools = TestLauncherArgvAndEnv()._fake_tools(tmp)
        env = child_env(CAPSPLUS_REPO=str(REPO), CAPSPLUS_RUN_DIR=str(tmp / "run"),
                        CAPSPLUS_RUNUSER=str(tools / "runuser"),
                        CAPSPLUS_PYTHON=str(tools / "fake-python"),
                        FAKE_UID=str(os.getuid()), FAKE_USER=os.environ.get("USER", "nobody"),
                        FAKE_ROOT_UID="0", **over)
        env["PATH"] = f"{tools}{os.pathsep}{env.get('PATH', '')}"
        proc = subprocess.run(["/bin/sh", str(LAUNCHER), "--region"], capture_output=True,
                              text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return dict(line.partition("=")[::2] for line in proc.stdout.splitlines() if "=" in line)

    def _touch_wayland_socket(self, run_dir: pathlib.Path) -> None:
        import socket
        run_dir.mkdir(parents=True, exist_ok=True)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(probe.close)
        probe.bind(str(run_dir / "wayland-0"))

    def test_explicit_wayland_display_is_passed_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: "wayland-9", VAR_DISPLAY: ":7"})
        self.assertEqual(out["WAYLAND"], "[wayland-9]")
        self.assertEqual(out["DISPLAY"], "[]", "有 Wayland 就不许再报 DISPLAY")

    def test_wayland_socket_is_detected_when_env_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp) / "run"
            self._touch_wayland_socket(run_dir)
            out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: None, VAR_DISPLAY: ":7"})
        self.assertEqual(out["WAYLAND"], "[wayland-0]")
        self.assertEqual(out["DISPLAY"], "[]")

    def test_plain_xorg_session_keeps_display_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: None, VAR_DISPLAY: ":7"})
        self.assertEqual(out["WAYLAND"], "[]")
        self.assertEqual(out["DISPLAY"], "[:7]")

    def test_nothing_at_all_falls_back_to_display_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(pathlib.Path(tmp), {VAR_WAYLAND: None, VAR_DISPLAY: None})
        self.assertEqual(out["WAYLAND"], "[]")
        self.assertEqual(out["DISPLAY"], "[:0]")

    def test_never_both_backends(self):
        for over in ({VAR_WAYLAND: "wayland-9", VAR_DISPLAY: ":7"},
                     {VAR_WAYLAND: None, VAR_DISPLAY: ":7"},
                     {VAR_WAYLAND: None, VAR_DISPLAY: None}):
            with self.subTest(**{k: (v or "-") for k, v in over.items()}), \
                    tempfile.TemporaryDirectory() as tmp:
                out = self._run(pathlib.Path(tmp), over)
                self.assertFalse(out["WAYLAND"].strip("[]") and out["DISPLAY"].strip("[]"),
                                 f"两个后端一起报给子进程了：{out}")


class TestGuardHasTeeth(unittest.TestCase):
    """反例具型：把"坏写法"喂给上面那几把尺子，必须被抓到（抓不住 = 没有判据）。"""

    def test_bashism_scanner_catches_the_real_accident(self):
        self.assertIn("=(", bashisms('A=(one two)\necho "$A"\n'))
        self.assertIn("数组赋值 NAME=(…)", bashisms("ARR=(a b)\n"))
        self.assertIn("[[ ", bashisms("if [[ -n $x ]]; then :; fi\n"))

    def test_bashism_scanner_ignores_mentions_inside_comments(self):
        self.assertEqual(bashisms("# 反面教材：A=(…) 会 syntax error\necho ok\n"), [],
                         "注释里提到反例不该被算成实现")

    def test_two_backend_argv_would_be_caught(self):
        bad = "WAYLAND=[wayland-9]\nDISPLAY=[:7]\n"
        out = dict(line.partition("=")[::2] for line in bad.splitlines())
        self.assertTrue(out["WAYLAND"].strip("[]") and out["DISPLAY"].strip("[]"),
                        "这条假输出正是要被抓的形态")

    def test_hardcoded_path_pattern_would_be_caught(self):
        for bad in ("USER_HOME=/home/someone\n", "RUN_DIR=/run/user/1000\n",
                    "runuser -u someone -- env\n"):
            hit = (re.search(r"/home/[A-Za-z0-9._-]+", bad)
                   or re.search(r"/run/user/[0-9]+", bad)
                   or re.search(r"runuser\s+-u\s+[A-Za-z0-9._-]+\s", bad))
            self.assertIsNotNone(hit, f"写死的形态应当被抓到：{bad.strip()}")


if __name__ == "__main__":
    unittest.main()