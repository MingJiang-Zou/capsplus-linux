# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""部署**之前**的预检：把坏文件挡在登录之外（`tools/preflight-extension.sh` 的嘴）。

为什么要有这一层（2026-10-20 真事故，用户那天因此重登 3 次）：
  扩展目录里出现过**半截文件** —— 登录同步脚本与 gnome-shell 抢同一个文件，shell 读到了
  拷贝到一半的 extension.js：
    gnome-shell: Extension raise-browser@capsplus.example:
      TypeError: malformed UTF-8 character sequence at offset 8190
  （8190 ≈ 8192-2 = 一个三字节汉字被切成两半。）
  扩展 import 失败 ⇒ 进 ERROR 态 ⇒ Wayland 下**除了注销重登没有别的办法**（见 docs/SHOT-TOOL.md）。
  原子落盘（`install-extension.sh --sync`）堵住了"被读到写一半"，这一层堵的是另一半：
  **写进去的那份东西本身是坏的**（截断、拼错、导出形态不对），它一样会让 shell 进 ERROR。

三道闸，按"越便宜越先跑"排：
  ① 文件在不在 + 是不是**合法 UTF-8**（能报出坏在**第几字节** —— 8190 那类一眼看穿）
  ② `metadata.json` 合法、uuid 与目录名一致；导出形态是 GNOME 45+ 要的 `export default class`
  ③ `extension.js` 能**解析**（`node --check`）＋ 带 **mock Shell** 的 import 冒烟（真 import 一次）

退出码（**与 tools/ext-bench.sh 同一套语义**，别再发明第三套）：
  0 = 全绿，可以部署；1 = 有一道红，**一个字节都不许写**；2 = 环境不齐（没有 node / 读不了文件），
  同样**不许写** —— 预检失败时"放行"是最坏的默认值（安全侧默认）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXT_UUID = "raise-browser@capsplus.example"
REQUIRED = ("metadata.json", "extension.js")
OPTIONAL = ("stylesheet.css",)
SMOKE_SCRIPT = REPO / "tools" / "preflight-import-smoke.mjs"

# GNOME 45+ 的导出形态（CI 里 `.github/workflows/tests.yml` 也钉了同样两条）：
# 写成模块级 `export function enable` 会被 shell 报 `not a constructor` 且**静默不加载**。
# ⚠ 必须**钉在行首**（不是裸 `in src`）：这个仓的注释里就写着 `` `export default class` ``，
# 裸子串匹配会被**注释**喂成绿的 —— 2026-10-20 反例举证时真踩到了（当时是 import 冒烟兜住的）。
NEED_EXPORT = "export default class"
NEED_EXPORT_RE = re.compile(r"^[ \t]*export\s+default\s+class\s+\w", re.M)
BAD_EXPORT = re.compile(r"^export\s+function\s+(enable|disable)\b", re.M)


# ----------------------------------------------------------------- ① 文件与 UTF-8

def utf8_problem(raw: bytes):
    """`None` = 合法；否则 `(首个坏字节的下标, 原因)`。

    单独拎出来是因为**报出下标**才是可操作的：`offset 8190` 直接告诉人"这是个被切两半的汉字"，
    而"文件不是 UTF-8"这种话只会让人去猜。
    """
    try:
        raw.decode("utf-8")
        return None
    except UnicodeDecodeError as exc:
        return exc.start, exc.reason


def check_files(ext_dir: Path) -> list:
    """① 该有的文件在不在、是不是合法 UTF-8。返回 `[(ok, 文本)]`。"""
    out = []
    for name in REQUIRED:
        path = ext_dir / name
        if not path.is_file():
            out.append((False, f"缺文件 {name}（{ext_dir}）—— shell 加载会直接失败"))
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            out.append((False, f"读不了 {name}：{exc}"))
            continue
        if not raw:
            out.append((False, f"{name} 是 0 字节（半截文件写进去的典型形态）"))
            continue
        bad = utf8_problem(raw)
        if bad is not None:
            offset, reason = bad
            out.append((False, f"{name} 不是合法 UTF-8：第 {offset} 字节处（{reason}）"
                               f"—— 看这个下标是不是卡在某个多字节字符中间"
                               f"（8190 ≈ 8192-2 = 一个汉字被切两半）"))
            continue
        out.append((True, f"{name}：{len(raw)} 字节，UTF-8 合法"))
    for name in OPTIONAL:
        path = ext_dir / name
        if not path.is_file():
            out.append((True, f"{name} 不在（可选；少了它贴图按钮会退回裸控件观感）"))
    return out


# ----------------------------------------------------------------- ② 元数据与导出形态

def check_metadata(ext_dir: Path) -> list:
    """② `metadata.json` 的 JSON、uuid、shell-version。"""
    path = ext_dir / "metadata.json"
    if not path.is_file():
        return []                                   # check_files 已经报过缺文件
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [(False, f"metadata.json 不是合法 JSON：{exc}")]
    out = []
    uuid = meta.get("uuid")
    if uuid != ext_dir.name:
        out.append((False, f"metadata.json 的 uuid={uuid!r} 与目录名 {ext_dir.name!r} 不一致"
                           f" ⇒ shell 认不出来"))
    else:
        out.append((True, f"metadata.json：uuid 与目录名一致（{uuid}）"))
    if not meta.get("shell-version"):
        out.append((False, "metadata.json 的 shell-version 是空的 ⇒ 这份 shell 不会加载它"))
    else:
        out.append((True, f"shell-version={meta['shell-version']}"))
    return out


def check_export_shape(src: str) -> list:
    """② GNOME 45+ 的导出形态 —— 与 CI 那两条**同一套判据**（别只留一份在 CI 里）。"""
    out = []
    if not NEED_EXPORT_RE.search(src):
        out.append((False, f"extension.js 里找不到**行首的** `{NEED_EXPORT}` 声明 ⇒ shell 会报 "
                           f"`not a constructor` 并**静默不加载**（现象与没装一样）"))
    else:
        out.append((True, f"导出形态：有行首的 `{NEED_EXPORT}` 声明"))
    hit = BAD_EXPORT.search(src)
    if hit:
        out.append((False, f"extension.js 里有模块级 `export function {hit.group(1)}` "
                           f"（第 {src[:hit.start()].count(chr(10)) + 1} 行）—— 旧形态，shell 不认"))
    else:
        out.append((True, "导出形态：没有模块级 `export function enable/disable`"))
    return out


# ----------------------------------------------------------------- ③ 解析与 import 冒烟

def node_path(which=None):
    """本机有没有 node。**不装新东西**：没有就报环境不齐（退出 2），而不是假装验过了。"""
    return (which or shutil.which)("node")


def _run(argv, timeout=60):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"起不了 {argv[0]}：{exc}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"超时（{timeout}s）：{argv}")


def check_js_parses(js_path: Path, node: str, runner=None) -> list:
    """③ `node --check`。

    必须**先拷成 `.mjs`**：extension.js 是 ESM（有 `export default class`），
    而 `node --check x.js` 按 CommonJS 解析，会对着 `export` 报语法错 —— 那是假红
    （CI 里也是这么做的：`cp "$f" /tmp/ext-check.mjs && node --check …`）。
    """
    runner = runner or _run
    with tempfile.TemporaryDirectory(prefix="capsplus-preflight-") as tmp:
        probe = Path(tmp) / "candidate.mjs"
        probe.write_bytes(js_path.read_bytes())
        proc = runner([node, "--check", str(probe)])
    if proc.returncode == 0:
        return [(True, "JS 解析：node --check 通过（按 ESM 解析）")]
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = "\n      ".join(detail[-4:]) if detail else "(没有输出)"
    return [(False, f"JS 解析：node --check 失败 ⇒ shell 会报 SyntaxError 且静默不加载\n"
                    f"      {tail}")]


def check_import_smoke(js_path: Path, node: str, runner=None) -> list:
    """③ 带 mock Shell 的 import 冒烟：把 `gi://` / `resource://` 换成桩，真 `import()` 一次。

    能验什么：模块级代码会不会抛、导出的是不是一个带 `enable`/`disable` 的类。
    **不能**验什么（别把它当运行时验收）：桩不是真 gi，任何"这个 API 存不存在 / 签名对不对"
    它都看不出来 —— 那要 `tools/ext-bench.sh` 起真 shell（见 docs/SHOT-TOOL.md）。
    """
    runner = runner or _run
    if not SMOKE_SCRIPT.is_file():
        return [(False, f"缺 {SMOKE_SCRIPT}（import 冒烟跑不了）")]
    proc = runner([node, str(SMOKE_SCRIPT), str(js_path)])
    text = (proc.stdout or "").strip()
    if proc.returncode == 0 and "SMOKE_OK" in text:
        detail = text.split("SMOKE_OK", 1)[1].strip(" ：:") or "通过"
        return [(True, "import 冒烟：" + detail)]
    detail = (proc.stderr or text or "(没有输出)").strip()
    return [(False, "import 冒烟：带桩 import 失败 ⇒ 模块级代码会抛，扩展起不来\n"
                    "      " + "\n      ".join(detail.splitlines()[-5:]))]


# ----------------------------------------------------------------- 汇总

def preflight(ext_dir, which=None, runner=None) -> tuple:
    """跑完三道闸。返回 `(rc, rows)`；rc 语义见模块头注（0/1/2）。"""
    ext_dir = Path(ext_dir)
    rows = []
    if not ext_dir.is_dir():
        return 2, [(False, f"扩展目录不存在：{ext_dir}")]
    node = node_path(which)
    if node is None:
        return 2, [(False, "本机没有 node ⇒ JS 解析与 import 冒烟都跑不了。"
                           "按安全侧：**不放行**（装 node 或改走 CI，别在这里放过去）")]

    rows += check_files(ext_dir)

    js_path = ext_dir / "extension.js"
    if js_path.is_file():
        # UTF-8 这一闸红的时候**不要**再去解文本：`read_text` 会直接抛 UnicodeDecodeError，
        # 调用方看到的是一段 traceback，而不是"坏在第几字节"（2026-10-20 判据抓出来的）。
        # 后面几道闸依赖文本，跳过它们并**说出为什么跳过**，比崩掉有用。
        bad = utf8_problem(js_path.read_bytes())
        if bad is None:
            rows += check_metadata(ext_dir)
            rows += check_export_shape(js_path.read_text(encoding="utf-8"))
            rows += check_js_parses(js_path, node, runner)
            rows += check_import_smoke(js_path, node, runner)
        else:
            rows.append((False, f"extension.js 不是合法 UTF-8（第 {bad[0]} 字节）"
                                f"⇒ 元数据 / 导出形态 / node --check / import 冒烟这四道闸没有意义，"
                                f"跳过；先把文件修成合法 UTF-8"))

    rc = 0 if all(ok for ok, _ in rows) else 1
    return rc, rows


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m capsplus.extpreflight",
        description="raise-browser 扩展的部署前预检（红 = 一个字节都不许写）")
    ap.add_argument("--ext-dir", default=str(REPO / "extensions" / EXT_UUID),
                    help="要预检的扩展目录（默认仓内那份；给反例时指一个坏副本）")
    ap.add_argument("--quiet", action="store_true", help="只打结论行（给脚本用）")
    args = ap.parse_args(argv)

    rc, rows = preflight(args.ext_dir)
    if not args.quiet:
        print(f"预检：{args.ext_dir}")
        for ok, text in rows:
            print(f"  {'✓' if ok else '✗'} {text}")
    if rc == 0:
        print("PREFLIGHT_OK 预检全绿：这份扩展文件可以部署")
    elif rc == 2:
        print("PREFLIGHT_SKIP 环境不齐 ⇒ 按安全侧不放行（这**不是**「文件坏了」）", file=sys.stderr)
    else:
        print("PREFLIGHT_FAIL 预检红 ⇒ **不部署**。修好上面标 ✗ 的再跑一次", file=sys.stderr)
    return rc


if __name__ == "__main__":                                    # pragma: no cover
    sys.exit(main())