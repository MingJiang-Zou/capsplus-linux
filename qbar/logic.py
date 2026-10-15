"""qbar 的纯逻辑层：fuzzy 匹配、算术求值、内置命令、条目模型、历史排序。

这一层**不 import gi**，所以能在没有显示服务器的环境下直接被 unittest 跑。
"""

from __future__ import annotations

import ast
import json
import operator
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 条目模型
# ---------------------------------------------------------------------------
KIND_LABEL = {
    "app": "应用",
    "file": "文件",
    "dir": "目录",
    "cmd": "命令",
    "calc": "计算",
    "url": "网址",
    "hist": "历史",
}


@dataclass
class Entry:
    """一条搜索结果。kind 决定标签与打开方式。"""
    title: str
    kind: str
    score: float = 0.0
    subtitle: str = ""
    payload: str = ""          # 应用=desktop 文件路径 / 文件=绝对路径 / 命令=命令名 / url=URL

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)


# ---------------------------------------------------------------------------
# fuzzy 子序列匹配
# ---------------------------------------------------------------------------
def fuzzy_score(query: str, text: str) -> float | None:
    """子序列匹配打分：命中返回分数，未命中返回 None。

    规则（够用就停，不上算法库）：
      - 查询字符必须按序出现在文本里（大小写不敏感）
      - 连续命中加分，词边界（空格/._-// 之后）命中加分，越早命中越好
      - 查询为空返回 0（列出全部，由调用方按历史排序）
    """
    if not query:
        return 0.0
    q = query.lower()
    t = text.lower()
    score = 0.0
    pos = 0
    prev_hit = -2
    for ch in q:
        idx = t.find(ch, pos)
        if idx < 0:
            return None
        if idx == prev_hit + 1:
            score += 6.0                      # 连续
        elif idx == 0 or t[idx - 1] in " /._-":
            score += 4.0                      # 词边界
        score += 1.0
        score -= idx * 0.01                   # 越早越好（弱权重，只做同分排序）
        prev_hit = idx
        pos = idx + 1
    score += max(0.0, 3.0 - len(t) / 40.0)    # 短名优先
    return score


def fuzzy_filter(query: str, candidates: list[Entry], limit: int = 60) -> list[Entry]:
    """按 fuzzy 打分过滤排序。entry.score 是"先验加成"（历史/内建权重），与匹配分相加。"""
    scored: list[Entry] = []
    for entry in candidates:
        hit = fuzzy_score(query, entry.title)
        alt = fuzzy_score(query, entry.subtitle) if hit is None and entry.subtitle else None
        best = hit if hit is not None else alt
        if best is None:
            continue
        scored.append(Entry(entry.title, entry.kind, entry.score + best, entry.subtitle, entry.payload))
    scored.sort(key=lambda e: (-e.score, e.title))
    return scored[:limit]


# ---------------------------------------------------------------------------
# =算术 安全求值（严禁 eval）
# ---------------------------------------------------------------------------
_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class CalcError(ValueError):
    """表达式非法。"""


def safe_calc(expr: str):
    """只允许数字常量与 + - * / // % ** 及括号、一元正负号。其余一律拒绝。"""
    expr = expr.strip()
    if not expr:
        raise CalcError("空表达式")
    if len(expr) > 200:
        raise CalcError("表达式过长")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"语法错误: {exc.msg}") from exc

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalcError("只允许数字")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 64:
                raise CalcError("指数过大")
            return _ALLOWED_BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(node.op)](ev(node.operand))
        # 名字 / 属性访问 / 调用 / 下标 / 比较 ... 全部拒绝
        raise CalcError(f"不允许的语法: {type(node).__name__}")

    try:
        return ev(tree)
    except ZeroDivisionError as exc:
        raise CalcError("除以零") from exc


def format_calc(value) -> str:
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return f"{value:.10g}"
    return str(value)


# ---------------------------------------------------------------------------
# 内置命令 / URL 判定
# ---------------------------------------------------------------------------
DOMAIN_RE = re.compile(r"^[\w-]+(\.[\w-]+)+(/\S*)?$")
URL_RE = re.compile(r"^https?://\S+$", re.I)


def looks_like_url(text: str) -> str | None:
    text = text.strip()
    if URL_RE.match(text):
        return text
    if " " not in text and DOMAIN_RE.match(text) and not text.startswith("->"):
        return "https://" + text
    return None


def browser_search_url(keyword: str) -> str:
    from urllib.parse import quote_plus
    return "https://www.bing.com/search?q=" + quote_plus(keyword)


BUILTIN_PREFIX = "->"


def builtin_entries(text: str) -> list[Entry]:
    """`-> xxx` 内置命令。返回候选（含 calc 的即时结果）。"""
    body = text[len(BUILTIN_PREFIX):].strip()
    out: list[Entry] = []
    if not body:
        for name, desc in (("date", "今天日期"), ("time", "当前时间"),
                           ("calc ", "算术求值，如 -> calc 1+2*3"), ("q ", "浏览器搜索")):
            out.append(Entry(f"-> {name}", "cmd", 50.0, desc, name.strip()))
        return out

    word, _, rest = body.partition(" ")
    word = word.strip()
    rest = rest.strip()
    if word == "date":
        out.append(Entry("-> date", "cmd", 90.0, datetime.now().strftime("%Y-%m-%d"), "date"))
    elif word == "time":
        out.append(Entry("-> time", "cmd", 90.0, datetime.now().strftime("%H:%M:%S"), "time"))
    elif word == "calc":
        if rest:
            try:
                value = format_calc(safe_calc(rest))
                out.append(Entry(f"= {value}", "calc", 95.0, f"{rest} （回车复制结果）", value))
            except CalcError as exc:
                out.append(Entry("-> calc", "cmd", 10.0, f"算不了: {exc}", ""))
        else:
            out.append(Entry("-> calc", "cmd", 50.0, "输入表达式，如 -> calc 1+2*3", "calc"))
    elif word == "q":
        if rest:
            out.append(Entry(f"-> q {rest}", "url", 90.0, "浏览器搜索", browser_search_url(rest)))
    else:
        for name in ("date", "time", "calc", "q"):
            if name.startswith(word):
                out.append(Entry(f"-> {name}", "cmd", 30.0, "", name))
    return out


# ---------------------------------------------------------------------------
# 历史（最近使用优先）
# ---------------------------------------------------------------------------
def history_key(kind: str, payload: str) -> str:
    """历史条目的键 = 类型 + \\t + 载荷（载荷里不会有 \\t）。"""
    return f"{kind}\t{payload}"


class History:
    """qbar 使用历史。计数 + 最近时间戳，两者合成一个小加成。"""
    def __init__(self, path: Path, max_items: int = 200):
        self.path = Path(path)
        self.max_items = max_items
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        items = sorted(self.data.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
        payload = dict(items[: self.max_items])
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def bump(self, key: str) -> None:
        item = self.data.setdefault(key, {"count": 0, "ts": 0})
        item["count"] += 1
        item["ts"] = datetime.now().timestamp()
        self.save()

    def score(self, key: str) -> float:
        item = self.data.get(key)
        if not item:
            return 0.0
        return min(12.0, 2.0 * item.get("count", 0)) + 6.0 * self._recency(item.get("ts", 0))

    @staticmethod
    def _recency(ts: float) -> float:
        if not ts:
            return 0.0
        age_h = max(0.0, (datetime.now().timestamp() - ts) / 3600.0)
        for hours, weight in ((1, 1.0), (24, 0.6), (24 * 7, 0.3), (24 * 30, 0.1)):
            if age_h <= hours:
                return weight
        return 0.0


# ---------------------------------------------------------------------------
# .desktop 应用
# ---------------------------------------------------------------------------
def parse_desktop_file(path: Path) -> Entry | None:
    """解析一个 .desktop 文件 -> Entry（失败返回 None）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_entry = False
    name = exec_line = icon = comment = ""
    nodisplay = hidden = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "Name" and not name:
            name = value
        elif key == "Exec" and not exec_line:
            exec_line = value
        elif key == "Icon" and not icon:
            icon = value
        elif key == "Comment" and not comment:
            comment = value
        elif key == "NoDisplay" and value.lower() == "true":
            nodisplay = True
        elif key == "Hidden" and value.lower() == "true":
            hidden = True
    if not name or not exec_line or nodisplay or hidden:
        return None
    return Entry(name, "app", 0.0, comment or exec_line, str(path))


def scan_desktop_files(dirs: list[Path]) -> list[Entry]:
    """扫描应用目录。同名应用以先出现的目录优先（用户目录排前面）。"""
    seen: dict[str, Entry] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            entry = parse_desktop_file(path)
            if entry and entry.title not in seen:
                seen[entry.title] = entry
    return list(seen.values())


# ---------------------------------------------------------------------------
# fdfind 后端访问器
# ---------------------------------------------------------------------------
FDFIND = "fdfind"


class FileFinder:
    """用 fdfind 子在后台线程里搜文件。

    刻意用线程 + subprocess.run(timeout=) 而不是 GLib 子进程管线：
    实现短、超时语义明确（超时 = 杀进程、返回已拿到的部分结果），
    结果通过 GLib.idle_add 回主线程。窗口绝不会被搜索卡住。
    """

    def __init__(self, roots: list[Path], binary: str = FDFIND,
             max_entries: int = 40, timeout_s: float = 1.5):
        self.roots = [Path(r) for r in roots]
        self.binary = binary
        self.max_entries = max_entries
        self.timeout_s = timeout_s

    def available(self) -> bool:
        from shutil import which
        return which(self.binary) is not None

    def _args(self, query: str) -> list[str]:
        args = [self.binary, "--color", "never", "--max-results", str(self.max_entries * 2)]
        if query:
            args += ["--ignore-case", query]
        else:
            args += ["--max-depth", "2"]
        args += [str(r) for r in self.roots if r.exists()]
        return args

    def run(self, query: str) -> list[Entry]:
        """同步执行一次搜索（在 worker 线程里调用）；超时/缺 fd 都返回已拿到的部分。"""
        import subprocess
        args = self._args(query)
        if len(args) <= 1 or not args[-1].startswith("/"):
            return []
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  timeout=self.timeout_s, errors="replace")
            lines = proc.stdout.splitlines()
        except (subprocess.TimeoutExpired, OSError):
            return []
        out: list[Entry] = []
        for line in lines[: self.max_entries]:
            line = line.strip()
            if not line:
                continue
            path = Path(line)
            is_dir = line.endswith("/") or path.is_dir()
            title = path.name or line
            out.append(Entry(title, "dir" if is_dir else "file", 0.0, str(path.parent), str(path)))
        return out


# ---------------------------------------------------------------------------
# 搜索结果编排（同步部分纯逻辑，方便单测）
# ---------------------------------------------------------------------------
@dataclass
class SearchOutcome:
    entries: list[Entry] = field(default_factory=list)
    needs_files: bool = True     # 是否需要异步去搜文件


class SearchEngine:
    def __init__(self, apps: list[Entry], history: History, file_finder: FileFinder):
        self.apps = apps
        self.history = history
        self.file_finder = file_finder

    def _history_entries(self) -> list[Entry]:
        out = []
        for key, item in self.history.data.items():
            kind, _, payload = key.partition("\t")
            out.append(Entry(payload or key, kind or "hist", self.history.score(key),
                             "最近使用", payload))
        return out

    def search(self, text: str) -> SearchOutcome:
        text = text.strip()
        out: list[Entry] = []
        need_files = True

        if text.startswith("=") and len(text) > 1:
            try:
                value = format_calc(safe_calc(text[1:]))
                out.append(Entry(f"= {value}", "calc", 100.0, f"{text[1:]} （回车复制结果）", value))
            except CalcError as exc:
                out.append(Entry("= 算不了", "calc", 5.0, str(exc), ""))
            return SearchOutcome(out, needs_files=False)

        url = looks_like_url(text)
        if url:
            out.append(Entry(url, "url", 100.0, "浏览器打开", url))
            return SearchOutcome(out, needs_files=False)

        if text.startswith(BUILTIN_PREFIX):
            return SearchOutcome(builtin_entries(text), needs_files=False)

        if not text:
            # 空输入：列出应用（按历史排序），不去扫盘
            need_files = False
        pool = self.apps + self._history_entries()
        out += fuzzy_filter(text, pool)
        out += fuzzy_filter(text, [Entry("-> " + c, "cmd", 0.0)
                                   for c in ("date", "time", "calc", "q")])
        return SearchOutcome(out, needs_files=need_files)
        out += fuzzy_filter(text, [Entry("-> " + c, "cmd", 0.0)
                                   for c in ("date", "time", "calc", "q")])
        return SearchOutcome(out, needs_files=need_files)

    def merge_files(self, current: list[Entry], files: list[Entry], text: str) -> list[Entry]:
        """把异步文件结果并进现有列表（去重后按分数排序）。"""
        merged = fuzzy_filter(text, current + files)
        seen, uniq = set(), []
        for entry in merged:
            key = (entry.kind, entry.payload or entry.title)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(entry)
        return uniq
