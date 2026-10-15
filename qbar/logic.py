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

try:                                      # 作为包导入（bin/ 与单测的调用方式）
    from .search_config import QbarConfig, load_config, parse_search_cmd
except ImportError:                       # 兜底：直接按文件路径跑时
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from qbar.search_config import QbarConfig, load_config, parse_search_cmd

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
    "cfg": "配置",
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


def browser_search_url(keyword: str, config: QbarConfig | None = None) -> str:
    """用配置里的默认引擎（`general.default_search`）拼搜索地址。

    没传 config 就用内置默认（必应）—— 老调用点保持可用。
    """
    if config is None:
        config = QbarConfig()
    link = config.default_link
    if link is None:                      # 理论上不会发生（links 至少一条），兜个底
        from urllib.parse import quote_plus
        return "https://www.bing.com/search?q=" + quote_plus(keyword)
    return link.target_url(keyword)


BUILTIN_PREFIX = "->"


def builtin_entries(text: str, config: QbarConfig | None = None) -> list[Entry]:
    """`-> xxx` 内置命令。返回候选（含 calc 的即时结果）。"""
    body = text[len(BUILTIN_PREFIX):].strip()
    default_name = (config or QbarConfig()).default_link
    default_name = default_name.name if default_name else "默认引擎"
    out: list[Entry] = []
    if not body:
        for name, desc in (("date", "今天日期"), ("time", "当前时间"),
                           ("calc ", "算术求值，如 -> calc 1+2*3"),
                           ("q ", f"网页搜索（用默认引擎：{default_name}）"),
                           ("link", "打开快速链接配置文件" if config is None
                            else f"打开 {config.path}")):
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
    elif word in ("q", "search", "搜"):
        action, keyword = parse_search_cmd(body)
        if action == "search":
            url = browser_search_url(keyword, config)
            out.append(Entry(f"-> q {keyword}", "url", 90.0,
                             f"网页搜索（{default_name}）: {url}", url))
        elif action == "hint":
            out.append(Entry("-> q", "cmd", 50.0,
                             f"输入关键词，如 -> q keyd 配置（引擎：{default_name}）", "q"))
    elif word == "link":
        path = (config or QbarConfig()).path
        out.append(Entry(f"-> link {path}", "cfg", 90.0, "打开快速链接配置文件（改完重开 Qbar 生效）",
                         str(path)))
    else:
        for name in ("date", "time", "calc", "q", "link"):
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


def _explicit_prefix(config: QbarConfig, text: str) -> bool:
    """输入是不是"明确写了 trigger"（`b 关键词` / `gh关键词`），而不是纯输入猜默认引擎。

    纯输入（如 `hello`）用默认引擎搜，但同时要保留 fuzzy 应用结果 ——
    否则用户打「firefox」就再也找不到应用了。
    判定口径必须和 QbarConfig.match() 一致：单字母 trigger 只认「`b 词`」这种带空格写法。
    """
    stripped = text.strip()
    if not stripped:
        return False
    head, sep, _rest = stripped.partition(" ")
    if sep:
        return config.find(head) is not None
    lowered = stripped.lower()
    return any(len(link.trigger) >= 2 and lowered.startswith(link.trigger.lower())
               for link in config.links)


class SearchEngine:
    def __init__(self, apps: list[Entry], history: History, file_finder: FileFinder,
                 config: QbarConfig | None = None, config_path: Path | None = None):
        self.apps = apps
        self.history = history
        self.file_finder = file_finder
        # config=None 时惰性读一次磁盘（空输入列表也会读，代价只有一次 TOML 解析）
        self.config = config if config is not None else load_config(config_path)
        self.config_path = self.config.path

    def reload_config(self) -> QbarConfig:
        """重读配置文件（每次打开 Qbar 调一次：改完配置不用重启进程）。"""
        self.config = load_config(self.config_path)
        return self.config

    def _history_entries(self) -> list[Entry]:
        out = []
        for key, item in self.history.data.items():
            kind, _, payload = key.partition("\t")
            out.append(Entry(payload or key, kind or "hist", self.history.score(key),
                             "最近使用", payload))
        return out

    # ------------------------------------------------------------------
    # 快速链接 / 网页搜索（配置驱动）
    # ------------------------------------------------------------------
    def _config_entry(self) -> list[Entry]:
        """配置有问题时返回一行醒目提示（回车打开配置去改）；没问题返回空列表。"""
        if not self.config.error:
            return []
        return [Entry("⚠ 快速链接配置有误", "cfg", 400.0,
                      f"{self.config.error}（已退回内置默认引擎；回车打开 {self.config_path}）",
                      str(self.config_path))]

    def _search_entries(self, text: str) -> tuple[list[Entry], bool]:
        """把输入解析成搜索/跳转候选。

        返回 `(候选, 是否独占)`：独占 = 用户明确写了前缀或网址，结果就这一条
        （`b 关键词` 回车即搜，不许被应用/文件挤到后面）；不独占 = 纯输入猜默认引擎搜索，
        此时仍要并上 fuzzy 结果 —— 否则「firefox」这类纯输入就再也找不到应用了。
        """
        out: list[Entry] = []

        # 真网址优先：`example.com/path` 不被单字母 trigger 抢走
        url = looks_like_url(text)
        if url:
            out.append(Entry(url, "url", 200.0, "浏览器打开", url))
            return out, True

        hit = self.config.match(text)
        if hit is not None:
            out.append(Entry(hit.entry_title, "url", 200.0, hit.subtitle, hit.url))
            explicit = _explicit_prefix(self.config, text)
            return out, explicit

        hint = self.config.no_keyword_hint(text)
        if hint is None:
            return out, False
        # 固定链接没有"关键词"一说：直接给整条
        if hint.is_fixed:
            out.append(Entry(f"{hint.name}（固定链接）", "url", 200.0, hint.url, hint.url))
            return out, True
        out.append(Entry(f"{hint.name}：输入关键词", "cmd", 200.0,
                         f"如「{hint.trigger} 关键词」；回车打开 {hint.name} 首页", hint.url))
        return out, True

    def search(self, text: str) -> SearchOutcome:
        text = text.strip()
        out = self._config_entry()           # 配置坏了就把那行提示顶在最前面
        explicit = False

        if text.startswith("=") and len(text) > 1:
            try:
                value = format_calc(safe_calc(text[1:]))
                out.append(Entry(f"= {value}", "calc", 100.0, f"{text[1:]} （回车复制结果）", value))
            except CalcError as exc:
                out.append(Entry("= 算不了", "calc", 5.0, str(exc), ""))
            return SearchOutcome(out, needs_files=False)

        if text.startswith(BUILTIN_PREFIX):
            return SearchOutcome(out + builtin_entries(text, self.config), needs_files=False)

        found, explicit = self._search_entries(text)
        out += found

        need_files = True
        if not text:
            # 空输入：列出应用（按历史排序），不去扫盘
            need_files = False
        elif explicit:
            # 用户写了明确前缀/网址：结果就这一条，回车即搜
            return SearchOutcome(out, needs_files=False)

        pool = self.apps + self._history_entries()
        out += fuzzy_filter(text, pool)
        out += fuzzy_filter(text, [Entry("-> " + c, "cmd", 0.0)
                                   for c in ("date", "time", "calc", "q", "link")])
        return SearchOutcome(out, needs_files=need_files)

    def open_config(self) -> Path:
        """确保配置文件存在（不在就写一份默认的）并返回路径。"""
        from .search_config import ensure_config_file
        return ensure_config_file(self.config_path)

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


# ---------------------------------------------------------------------------
# Qbar 显示切换决策（纯函数，便于单测）
# ---------------------------------------------------------------------------
def toggle_action(visible: bool, active: bool) -> str:
    """Caps+Q（或 socket）被触发时该做什么。

    Wayland 下没有 keep-above 协议：Qbar 被别的窗口盖住时它**仍然是 visible**，
    只是没有键盘焦点。此时若按老逻辑"visible 就 hide"，用户看到的是"窗口没了、
    再按一下还是没反应"。所以：

    - 没显示        -> "show"（首次唤起）
    - 显示但没焦点  -> "raise"（拉回前台并聚焦，而不是藏起来）
    - 显示且有焦点  -> "hide"（用户就是想收起来）
    """
    if not visible:
        return "show"
    return "hide" if active else "raise"


# 窗口显示后多久之内的失焦不算"用户点别处"（毫秒）。
# 冷启动时 present() 与焦点落到输入框之间有个几十毫秒的空档，Wayland 下
# 这期间可能先来一次 focus-out；没有这个防抖，Qbar 会刚开就自己收掉。
FOCUS_OUT_GRACE_MS = 300


def should_hide_on_focus_out(visible: bool, focused: bool, ms_since_show: int,
                             flag: bool = True) -> bool:
    """窗口失焦时该不该 hide()（纯函数，便于单测）。

    明江的口径（2026-10-15）：**点别处就真隐藏**，跟再按一次 Caps+Q 一个效果 ——
    否则下次唤起要先"隐藏"再"显示"，得按两次。所以这里默认就是隐藏，而不是拉回前台。

    - flag=False            -> 保持老行为（不隐藏，交给 toggle 的 raise 去拉回前台）
    - 窗口没显示            -> 没什么可藏的，返回 False
    - 还带焦点（模拟/测试用）-> 不隐藏
    - 显示后不足防抖窗口    -> 不隐藏（躲掉刚显示那一下的瞬时失焦）
    """
    if not flag or not visible or focused:
        return False
    return ms_since_show >= FOCUS_OUT_GRACE_MS
