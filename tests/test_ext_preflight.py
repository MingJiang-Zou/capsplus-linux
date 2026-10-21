# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""判据：扩展的**部署前预检**（`tools/preflight-extension.sh` / `capsplus/extpreflight.py`）。

为什么这一层要有判据（2026-10-20 真事故）：坏文件进了扩展目录 ⇒ shell import 失败 ⇒ 进 ERROR 态
⇒ Wayland 下只能注销重登才能恢复。用户那天为此重登了 3 次。所以"挡住坏文件"这件事本身
必须有**会红过的**判据 —— 而且最要紧的一条不是"预检报了红"，是
**"报红的时候一个字节都没写"**：只报不挡等于没挡。

本文件里最重的一条是 `TestSyncRefusesToWrite`：它复制一份**假仓 + 假 HOME**，往假仓里注入三种
真出过的故障（半截汉字 / 语法错 / 导出形态错），然后真跑 `install-extension.sh --sync`，断言
  ① 退出码非 0；② 部署目录里每个文件的 size/md5/mtime 与跑之前**逐字节一致**，且文件清单不变。
`rm` 一律不用；临时目录交给 `mkdtemp` + `rmtree`（测试夹具，不是"删除用户数据"）。
"""

import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from capsplus import extpreflight as EP          # noqa: E402

EXT_DIR = REPO / "extensions" / EP.EXT_UUID
PREFLIGHT = REPO / "tools" / "preflight-extension.sh"
SYNC = REPO / "tools" / "install-extension.sh"
NODE = shutil.which("node")


def fingerprint(root: pathlib.Path) -> dict:
    """目录指纹：每个文件 → (size, md5, mtime_ns)。"""
    out = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            st = path.stat()
            out[str(path.relative_to(root))] = (len(raw),
                                                hashlib.md5(raw).hexdigest(),
                                                st.st_mtime_ns)
    return out


# --------------------------------------------------------------------- 纯函数那一层

class TestUtf8Gate(unittest.TestCase):
    def test_valid_utf8_passes(self):
        self.assertIsNone(EP.utf8_problem("合法中文 abc".encode("utf-8")))

    def test_half_a_chinese_character_is_caught_with_an_offset(self):
        """反例具型：把一个汉字只留第一个字节（三字节 UTF-8 里切掉后面两个）。"""
        raw = "前缀".encode("utf-8") + "汉".encode("utf-8")[:1]
        bad = EP.utf8_problem(raw)
        self.assertIsNotNone(bad, "半截汉字必须判非法")
        offset, _reason = bad
        self.assertEqual(offset, 6, "报的下标要正好是那个坏字节（'前缀'=6 字节）")

    def test_truncated_file_is_caught(self):
        """真事故的形态：整个文件是拷贝中的半截。"""
        raw = ("// " + "中文注释" * 100).encode("utf-8")[:97]
        self.assertIsNotNone(EP.utf8_problem(raw))

    def test_empty_file_is_reported_by_check_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp) / EP.EXT_UUID
            d.mkdir()
            (d / "metadata.json").write_text("{}", encoding="utf-8")
            (d / "extension.js").write_bytes(b"")
            rows = EP.check_files(d)
            self.assertTrue(any(not ok and "0 字节" in text for ok, text in rows),
                            f"0 字节文件要单独报出来：{rows}")


class TestExportShape(unittest.TestCase):
    GOOD = "export default class Foo {\n  enable() {}\n  disable() {}\n}\n"

    def test_good_shape_passes(self):
        self.assertTrue(all(ok for ok, _ in EP.check_export_shape(self.GOOD)))

    def test_module_level_export_function_is_red(self):
        """2026-10-16 那次事故的形态：shell 报 `not a constructor` 且**静默不加载**。"""
        bad = "export function enable() {}\nexport function disable() {}\n"
        rows = EP.check_export_shape(bad)
        self.assertTrue(any(not ok and "export default class" in text for ok, text in rows), rows)
        self.assertTrue(any(not ok and "export function" in text for ok, text in rows), rows)

    def test_class_without_default_keyword_is_red(self):
        rows = EP.check_export_shape("class Foo { enable() {} }\n")
        self.assertTrue(any(not ok for ok, _ in rows))

    def test_a_comment_mentioning_it_does_not_count(self):
        """**反例具型**（2026-10-20 反例举证时真踩到）：注释里写着 `` `export default class` ``，
        裸子串匹配就会被喂成绿的 —— 判据必须钉在**行首**。"""
        src = ("// 声明成 `export default class` 才算对（这句是注释）\n"
               "class Foo { enable() {} disable() {} }\n")
        rows = EP.check_export_shape(src)
        self.assertTrue(any(not ok and "行首" in text for ok, text in rows),
                        f"注释里的字样把判据喂绿了：{rows}")

    def test_indented_declaration_still_counts(self):
        """行首判定不能太紧：顶层声明前面有空格也应该认（只要求不是注释）。"""
        self.assertTrue(all(ok for ok, _ in EP.check_export_shape(
            "  export default class Foo { enable() {} }\n")))


class TestMetadataGate(unittest.TestCase):
    def _dir_with_meta(self, tmp, meta, name=None):
        d = pathlib.Path(tmp) / (name or EP.EXT_UUID)
        d.mkdir()
        (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        return d

    def test_uuid_mismatch_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dir_with_meta(tmp, {"uuid": "别的@uuid", "shell-version": ["50"]})
            rows = EP.check_metadata(d)
            self.assertTrue(any(not ok and "uuid" in text for ok, text in rows), rows)

    def test_empty_shell_version_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dir_with_meta(tmp, {"uuid": EP.EXT_UUID, "shell-version": []})
            rows = EP.check_metadata(d)
            self.assertTrue(any(not ok and "shell-version" in text for ok, text in rows), rows)

    def test_broken_json_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp) / EP.EXT_UUID
            d.mkdir()
            (d / "metadata.json").write_text("{ 不是 json", encoding="utf-8")
            rows = EP.check_metadata(d)
            self.assertTrue(any(not ok and "JSON" in text for ok, text in rows), rows)

    def test_real_repo_metadata_passes(self):
        self.assertTrue(all(ok for ok, _ in EP.check_metadata(EXT_DIR)))


# --------------------------------------------------------------------- node 那一层

@unittest.skipIf(NODE is None, "本机没有 node：JS 解析与 import 冒烟跑不了")
class TestNodeGates(unittest.TestCase):
    def test_real_extension_parses_and_imports(self):
        rows = EP.check_js_parses(EXT_DIR / "extension.js", NODE)
        self.assertTrue(all(ok for ok, _ in rows), rows)
        rows = EP.check_import_smoke(EXT_DIR / "extension.js", NODE)
        self.assertTrue(all(ok for ok, _ in rows), rows)

    def test_syntax_error_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "extension.js"
            p.write_text("export default class A { enable( { }\n", encoding="utf-8")
            rows = EP.check_js_parses(p, NODE)
            self.assertTrue(any(not ok for ok, _ in rows), rows)

    def test_module_that_throws_at_import_time_is_red(self):
        """import 冒烟要能抓住"模块级代码会抛" —— 语法没错但一起来就炸。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "extension.js"
            p.write_text("throw new Error('模块级就炸了');\n"
                         "export default class A { enable() {} disable() {} }\n",
                         encoding="utf-8")
            self.assertTrue(all(ok for ok, _ in EP.check_js_parses(p, NODE)), "语法本身是合法的")
            rows = EP.check_import_smoke(p, NODE)
            self.assertTrue(any(not ok for ok, _ in rows), rows)

    def test_missing_enable_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "extension.js"
            p.write_text("export default class A { disable() {} }\n", encoding="utf-8")
            rows = EP.check_import_smoke(p, NODE)
            self.assertTrue(any(not ok for ok, _ in rows), rows)

    def test_text_decoder_rejects_half_a_character(self):
        """冒烟脚本自己也要按**严格 UTF-8** 解一次（fatal: true），不能悄悄替换成 U+FFFD。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "extension.js"
            p.write_bytes("export default class A { enable() {} disable() {} }\n// 汉".encode("utf-8")[:-2])
            rows = EP.check_import_smoke(p, NODE)
            self.assertTrue(any(not ok for ok, _ in rows), rows)


@unittest.skipIf(NODE is None, "本机没有 node：import 冒烟跑不了")
class TestSmokeCleansUpAfterItself(unittest.TestCase):
    """冒烟脚本**两条路**都不许留下 `/tmp/capsplus-smoke-*`。

    2026-10-20 实测踩到：清场原先写在 `finally` 里，而失败路径走的是 `catch { … process.exit(1) }`
    —— `process.exit()` 立刻终止进程，`finally` 根本轮不到执行 ⇒ 每一条失败路径留一个临时目录
    （跑几轮全量单测就攒了 85 个）。本仓为这类事吃过亏（`/tmp/capsplus-extjs-*` 一天 141 个），
    所以这条必须**有判据**，不能只写在注释里。
    """

    GLOB = "capsplus-smoke-*"

    def _left_behind(self):
        parent = pathlib.Path(tempfile.gettempdir())
        return {p.name for p in parent.glob(self.GLOB)}

    def test_success_path_leaves_nothing(self):
        before = self._left_behind()
        EP.check_import_smoke(EXT_DIR / "extension.js", NODE)
        self.assertEqual(self._left_behind() - before, set(), "成功那次留下了临时目录")

    def test_failure_path_leaves_nothing(self):
        before = self._left_behind()
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "extension.js"
            bad.write_text("export default class A { disable() {} }\n", encoding="utf-8")
            rows = EP.check_import_smoke(bad, NODE)
            self.assertTrue(any(not ok for ok, _ in rows), "夹具该让冒烟红")
        self.assertEqual(self._left_behind() - before, set(),
                         "失败那次留下了临时目录（process.exit 会跳过 finally，清场要写在它之前）")

    def test_half_char_failure_path_leaves_nothing(self):
        """UTF-8 那一路（TextDecoder fatal 抛异常）也要清干净。"""
        before = self._left_behind()
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "extension.js"
            bad.write_bytes("export default class A {}\n// 汉".encode("utf-8")[:-2])
            EP.check_import_smoke(bad, NODE)
        self.assertEqual(self._left_behind() - before, set())


# --------------------------------------------------------------------- 端到端：真的不写

class TestSyncRefusesToWrite(unittest.TestCase):
    """**本文件的重点**：预检报红时，`--sync` 必须一个字节都不写。

    做法：造一个**假仓**（复制 tools/preflight-extension.sh + capsplus/ + 扩展）与一个**假 HOME**
    （`$HOME/.local/share/gnome-shell/extensions/<uuid>` 里先放一份**完好**的部署副本）。
    这样真跑 `install-extension.sh --sync` 也只可能碰假 HOME —— 用户真那一份部署副本全程零接触。
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="capsplus-preflight-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake_repo = self.tmp / "repo"
        self.fake_home = self.tmp / "home"
        # 假仓：预检要靠 REPO_DIR 找到 capsplus/ 与 tools/，所以照目录形状复制
        (self.fake_repo / "tools").mkdir(parents=True)
        (self.fake_repo / "extensions").mkdir(parents=True)
        shutil.copytree(REPO / "capsplus", self.fake_repo / "capsplus",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("preflight-extension.sh", "install-extension.sh",
                     "preflight-import-smoke.mjs"):
            shutil.copy2(REPO / "tools" / name, self.fake_repo / "tools" / name)
        self.src = self.fake_repo / "extensions" / EP.EXT_UUID
        shutil.copytree(EXT_DIR, self.src)
        # 假 HOME：先放一份完好副本，跑完要证明它**逐字节没动**
        self.dst = (self.fake_home / ".local" / "share" / "gnome-shell" /
                    "extensions" / EP.EXT_UUID)
        shutil.copytree(EXT_DIR, self.dst)
        # 允许 preflight-extension.sh 被 sh 调用（CI 里 checkout 可能丢了可执行位）
        os.chmod(self.fake_repo / "tools" / "preflight-extension.sh",
                 stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    def _run_sync(self):
        env = dict(os.environ)
        env["HOME"] = str(self.fake_home)
        env["CAPSPLUS_NOTIFY"] = "0"      # 反例会走失败分支：不许把通知拍到屏幕上
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        return subprocess.run(["sh", str(self.fake_repo / "tools" / "install-extension.sh"),
                               "--sync"], capture_output=True, text=True, timeout=180, env=env)

    def _assert_refused_and_untouched(self, label):
        before = fingerprint(self.dst)
        proc = self._run_sync()
        after = fingerprint(self.dst)
        self.assertNotEqual(proc.returncode, 0,
                            f"{label}：预检该红、--sync 该非零退出，实际 rc=0\n{proc.stdout}")
        self.assertEqual(before, after,
                         f"{label}：部署副本被动了！预检红了还写文件是最坏的形态\n"
                         f"差异：{ {k: (before.get(k), after.get(k)) for k in set(before) | set(after) if before.get(k) != after.get(k)} }")
        return proc

    def _fault_half_char(self):
        """反例具型 ①：把正文里一个汉字只留第一个字节 —— 其余文件完好，专打 UTF-8 这一闸。"""
        p = self.src / "extension.js"
        raw = p.read_bytes()
        idx = next(k for k in range(len(raw)) if 0xE0 <= raw[k] <= 0xEF)
        p.write_bytes(raw[:idx + 1] + raw[idx + 3:])       # 三字节汉字 → 只剩第一个字节
        self.assertIsNotNone(EP.utf8_problem(p.read_bytes()), "夹具没造出非法 UTF-8")

    def _fault_syntax(self):
        """反例具型 ②：UTF-8 合法、但 JS 解析不过（尾上挂一个坏函数）。"""
        p = self.src / "extension.js"
        p.write_text(p.read_text(encoding="utf-8") + "\nfunction 坏的( {\n", encoding="utf-8")
        self.assertIsNone(EP.utf8_problem(p.read_bytes()), "夹具该保持合法 UTF-8")

    def _fault_export_shape(self):
        """反例具型 ③：2026-10-16 那次 —— 少了 `export default class`，shell 静默不加载。"""
        p = self.src / "extension.js"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "export default class", "class", 1), encoding="utf-8")

    def test_healthy_source_is_written_and_preflight_is_green(self):
        """正例：完好的源**应该**写得进去 —— 否则上面那三条"没写"就毫无意义（全都写不进去）。"""
        self.assertTrue(all(ok for ok, _ in EP.preflight(self.src)[1]),
                        EP.preflight(self.src)[1])
        # 先把部署副本弄脏，否则"内容相同就不写"那条会让这次同步什么都不做、正例测不出东西
        (self.dst / "extension.js").write_text("// 脏的旧副本\n", encoding="utf-8")
        proc = self._run_sync()
        self.assertEqual(proc.returncode, 0, f"完好源却被挡住：\n{proc.stdout}\n{proc.stderr}")
        self.assertEqual((self.dst / "extension.js").read_bytes(),
                         (self.src / "extension.js").read_bytes(),
                         "同步之后部署副本必须与源逐字节一致")

    def test_sync_is_idempotent_when_content_is_identical(self):
        """内容一样就**一个字节都不写**（原子落盘那条的一部分，mtime 也不许动）。"""
        proc = self._run_sync()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        before = fingerprint(self.dst)
        proc = self._run_sync()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(before, fingerprint(self.dst), "内容相同却又写了（mtime 变了）")

    def test_half_a_chinese_character_is_refused_and_nothing_written(self):
        self._fault_half_char()
        proc = self._assert_refused_and_untouched("半截汉字")
        self.assertIn("UTF-8", proc.stdout + proc.stderr)

    def test_syntax_error_is_refused_and_nothing_written(self):
        self._fault_syntax()
        proc = self._assert_refused_and_untouched("语法错")
        self.assertIn("node --check", proc.stdout + proc.stderr)

    def test_wrong_export_shape_is_refused_and_nothing_written(self):
        self._fault_export_shape()
        proc = self._assert_refused_and_untouched("导出形态错")
        self.assertIn("export default class", proc.stdout + proc.stderr)

    def test_preflight_writes_nothing_even_on_the_healthy_source(self):
        """预检自己**从不写**：它只读源、只读部署副本（这一条把"预检=只读"钉住）。"""
        before = fingerprint(self.dst)
        proc = subprocess.run(["sh", str(self.fake_repo / "tools" / "preflight-extension.sh"),
                               "--ext-dir", str(self.src)],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(before, fingerprint(self.dst))


class TestPreflightScriptShape(unittest.TestCase):
    """两个新脚本自己的形状：CI 那条 `for f in tools/*.sh` 的 shebang 检查也得过。"""

    SCRIPTS = (PREFLIGHT,)
    SMOKE = REPO / "tools" / "preflight-import-smoke.mjs"

    def _code(self, path):
        return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                         if not ln.lstrip().startswith("#"))

    def test_parses_with_its_own_shebang(self):
        for path in self.SCRIPTS:
            interp = path.read_text(encoding="utf-8").splitlines()[0].lstrip("#!").strip() or "/bin/sh"
            self.assertTrue(interp.endswith("sh"), f"{path.name} 的 shebang 应该是 /bin/sh")
            proc = subprocess.run([interp, "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{path.name}: {proc.stderr}")

    def test_no_bashisms(self):
        for path in self.SCRIPTS:
            code = self._code(path)
            for bad in ("=(", "shopt", "[[ ", "${A[@]}"):
                self.assertNotIn(bad, code, f"{path.name} 里有 bash 专用写法：{bad}")
            self.assertNotRegex(code, r"(?<!\$)\(\( ", f"{path.name} 用了 bash 的 (( )) 算术命令")

    def test_preflight_never_kills_anything(self):
        for path in self.SCRIPTS:
            code = self._code(path)
            self.assertNotIn("pkill", code)
            self.assertNotIn("killall", code)

    def test_verdict_is_not_reimplemented_in_sh(self):
        """判词只许有一份（Python 那侧）；sh 里再 grep 一遍就是第二份真相。"""
        for path in self.SCRIPTS:
            text = path.read_text(encoding="utf-8")
            self.assertIn("capsplus.extpreflight", text)
            self.assertNotIn("malformed", text, "sh 里在重复实现判词")

    def test_smoke_script_cleans_up_its_temp_dir(self):
        text = self.SMOKE.read_text(encoding="utf-8")
        self.assertIn("rmSync", text, "冒烟脚本必须把临时目录收掉（本仓踩过 /tmp 堆 141 个的坑）")
        self.assertIn("fatal: true", text, "必须按严格 UTF-8 解一次，半截文件才会现形")


if __name__ == "__main__":
    unittest.main(verbosity=2)