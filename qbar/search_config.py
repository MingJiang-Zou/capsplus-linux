"""Qbar 快速链接 / 网页搜索的配置层。

和 `qbar/logic.py` 一样**刻意不 import gi**：只依赖标准库（`tomllib` 是只读解析，
Python 3.11+ 自带），所以在没有显示服务器的环境里也能直接跑单测。

配置文件是 `~/.config/capsplus/qbar.toml`：

    [general]
    default_search = "b"     # 不带前缀的输入用哪个（填某个 link 的 trigger）
    browser = ""             # 留空 = 系统默认浏览器（xdg-open）
    hide_on_focus_out = true # 点别处就收起来（等价于再按一次 Caps+Q）

    [[link]]
    trigger = "b"
    name = "必应"
    url = "https://www.bing.com/search?q={q}"

匹配规则（`match_search` 实现，单测锁住）：
  - `b 关键词` 命中 trigger `b`；`gh 关键词` 命中 `gh`（**最长前缀优先**，`g` 不抢 `gh`）
  - 单字母 trigger **必须带空格**才算前缀：`b 词` 是搜索，`beta` 是普通输入
    （否则以 b/g/w 开头的词全被吃掉）
  - 纯输入（无前缀）→ 用 `default_search` 的引擎搜索
  - 前缀之后没跟内容（只敲了 `b `）→ 返回 None，由上层提示"输入关键词"
  - `looks_like_url()` 认得出的真网址优先，不被单字母 trigger 抢走
  - url 里没有 `{q}` = 固定链接，整条直接打开
  - 模板里的 `{q}` 做 URL 编码替换（空格 → `+`）；要原样嵌入用 `{q_raw}`（如 `gh cli`）

配置语法坏了**不许崩**：`QbarConfig.error` 带上原因，`links` 退回内置默认，界面照常能搜。

## 四种链接类型（`type`，2026-10-15 加）

    type = "search"  # 网站内搜索：目标里必须有 {q} 或 {q_raw}
    type = "url"     # 固定网址：目标里不许有 {q}
    type = "app"     # 本地应用：.desktop 名（firefox / firefox.desktop）或一条命令；
                     #          命令里可以用 {q} 当参数（原样，不做 URL 编码）
    type = "file"    # 文件或目录路径：`~` 会展开；固定路径，不支持 {q}

**老文件不写 `type` 也能用**（`resolved_type` 推断：有占位符=search，没有=url），
所以这次改动对现有配置逐字节无感；设置界面会把 `type` 显式写出来，读的人不用猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, quote_plus

# 占位符：{q} 走 URL 编码；{q_raw} 原样嵌入（给 `gh cli` 这类需要保留空格的地址）
QUOTED_PLACEHOLDER = "{q}"
RAW_PLACEHOLDER = "{q_raw}"

CONFIG_PATH = Path.home() / ".config/capsplus/qbar.toml"

# 链接类型：界面下拉、写盘、校验共用这一份（别在 GUI 里再抄一遍）
LINK_TYPES = ("search", "url", "app", "file")
LINK_TYPE_LABEL = {
    "search": "网站内搜索",
    "url": "固定网址",
    "app": "本地应用",
    "file": "文件或目录",
}

@dataclass(frozen=True)
class SearchLink:
    """一条快速链接：trigger + 显示名 + 目标 + 类型。

    字段名 `url` 是历史包袱，**四种类型都存在这一列**（TOML 里也是 `url =`），
    免得同一件事有两个键、读的人还得判断该看哪个。`target` 是它的别名。
    """
    trigger: str
    name: str
    url: str
    type: str = ""

    @property
    def target(self) -> str:
        return self.url

    @property
    def is_fixed(self) -> bool:
        """模板里没有 {q}/{q_raw} = 固定链接（站点首页 / 某篇文档）。"""
        return (QUOTED_PLACEHOLDER not in self.url) and (RAW_PLACEHOLDER not in self.url)

    @property
    def resolved_type(self) -> str:
        """没写 `type` 的老配置走推断：有占位符 = search，没有 = url（固定网址）。

        app / file **必须显式写** `type`，因为「`code`」这种目标既像命令又像路径，
        猜错就会去开浏览器 —— 宁可让人写清楚。
        """
        t = (self.type or "").strip().lower()
        if t in LINK_TYPES:
            return t
        return "search" if not self.is_fixed else "url"

    @property
    def type_label(self) -> str:
        return LINK_TYPE_LABEL.get(self.resolved_type, self.resolved_type)

    def target_url(self, keyword: str = "") -> str:
        """把关键词填进模板。`{q}` 编码、`{q_raw}` 原样；固定链接忽略关键词。"""
        if self.is_fixed:
            return self.url
        return (self.url
                .replace(QUOTED_PLACEHOLDER, quote_plus(keyword))
                .replace(RAW_PLACEHOLDER, quote(keyword)))


@dataclass
class SearchMatch:
    """`match_search` 的命中结果：一条可直接打开的候选（上层包成 Entry）。

    `kind` / `payload` 是给上层决定**怎么打开**用的：网址走浏览器，
    应用/文件走本地路径或命令（见 `open_plan`）。老调用点只读 `url`，不受影响。
    """
    entry_title: str          # 如「必应搜索：keyd 配置」
    subtitle: str             # 如 `b keyd 配置 -> https://...`
    url: str                  # 已填好关键词的目标地址；固定链接 = 模板本身
    link: SearchLink
    keyword: str = ""
    kind: str = "url"         # url / app / exec / file / dir
    payload: str = ""         # 真正要打开的东西；留空 = 用 url 本身


# 内置默认引擎。trigger 定得顺手且不重复：单字母给最常用的，两字母给站点。
# `g` 是 Google、`gh` 是 GitHub —— 靠"最长前缀优先"区分，不需要用户操心。
BUILTIN_LINKS = [
    SearchLink("b", "必应", "https://www.bing.com/search?q=" + QUOTED_PLACEHOLDER),
    SearchLink("g", "Google", "https://www.google.com/search?q=" + QUOTED_PLACEHOLDER),
    SearchLink("bd", "百度", "https://www.baidu.com/s?wd=" + QUOTED_PLACEHOLDER),
    SearchLink("w", "维基中文", "https://zh.wikipedia.org/w/index.php?search=" + QUOTED_PLACEHOLDER),
    SearchLink("gh", "GitHub", "https://github.com/search?q=" + QUOTED_PLACEHOLDER),
    SearchLink("zh", "知乎", "https://www.zhihu.com/search?type=content&q=" + QUOTED_PLACEHOLDER),
    SearchLink("bl", "B站", "https://search.bilibili.com/all?keyword=" + QUOTED_PLACEHOLDER),
    SearchLink("tr", "翻译（有道）", "https://dict.youdao.com/search?q=" + QUOTED_PLACEHOLDER),
    SearchLink("ghcli", "GitHub CLI 仓库", "https://github.com/cli/" + RAW_PLACEHOLDER),
    SearchLink("ghp", "GitHub 我的仓库", "https://github.com/pulls"),
]

DEFAULT_TRIGGER = "b"


# ---------------------------------------------------------------------------
# 四种类型：怎么打开 / 怎么校验 / 怎么预览（全是纯函数，界面前端只负责摆控件）
# ---------------------------------------------------------------------------
def fill_raw(template: str, keyword: str) -> str:
    """把 {q}/{q_raw} 都按**原样**填进目标（应用命令行用，URL 编码在参数里没意义）。"""
    return template.replace(QUOTED_PLACEHOLDER, keyword).replace(RAW_PLACEHOLDER, keyword)


def expand_path(text: str) -> str:
    """路径里的 `~` 展开成家目录；**相对路径按家目录算**。

    Qbar 进程的当前目录没人猜得到（托盘拉起 / keyd 拉起 / 手敲都不一样），
    所以 `notes` 一律算成 `~/notes`；空串原样返回（校验层先拦掉"缺少 url"）。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else Path.home() / path)


def open_plan(link: SearchLink, keyword: str = "") -> tuple[str, str]:
    """这条链接该怎么打开。返回 `(方式, 载荷)`：

      url   载荷 = 最终网址（search 已填好关键词）
      app   载荷 = .desktop 名或一条命令（上层再判断是哪一个）
      file  载荷 = 展开 `~` 后的本地路径

    刻意不在这里判断"desktop 名到底存不存在"：那要扫 /usr/share/applications，
    属于上层（Qbar 已经有应用表）的事，纯函数保持可以离线跑单测。
    """
    kind = link.resolved_type
    if kind in ("search", "url"):
        return ("url", link.target_url(keyword))
    if kind == "app":
        return ("app", fill_raw(link.url, keyword))
    return ("file", expand_path(link.url))


def link_problems(link: SearchLink, seen: tuple[str, ...] = ()) -> list[str]:
    """一条链接的全部毛病（中文，界面直接显式显示）。没问题返回 []。

    `seen` 是**同一份配置里已用过的 trigger**（小写），用来抓完全重复的 trigger ——
    重复会让"长前缀优先"变成掷硬币，必须拦住而不是静默挑一条。
    """
    out: list[str] = []
    trigger = (link.trigger or "").strip()
    name = (link.name or "").strip()
    target = (link.url or "").strip()
    if not trigger:
        out.append("缺少 trigger")
    elif any(ch.isspace() for ch in trigger):
        out.append(f"trigger 不能含空格: {trigger!r}")
    elif seen and trigger.lower() in seen:
        out.append(f"trigger 重复: {trigger}")
    if not name:
        out.append("缺少 name")
    if not target:
        out.append("缺少 url")
    raw_type = (link.type or "").strip().lower()
    if raw_type and raw_type not in LINK_TYPES:
        out.append(f"类型不认识: {raw_type}（只能 {'/'.join(LINK_TYPES)}）")
        return out
    if not target:
        return out
    if raw_type == "search" and link.is_fixed:
        out.append("网站内搜索的目标里要有 {q} 或 {q_raw}")
    elif raw_type == "url" and not link.is_fixed:
        out.append("固定网址不许带 {q}（要搜索就选 search）")
    elif raw_type == "file":
        if not link.is_fixed:
            out.append("文件或目录是固定路径，不支持 {q}")
    elif raw_type == "app":
        if "\n" in target:
            out.append("应用命令不许换行")
    return out


def duplicate_triggers(links: list[SearchLink]) -> list[str]:
    """完全重复的 trigger（大小写不敏感），保持首次出现的顺序。"""
    seen: set[str] = set()
    dup: list[str] = []
    for link in links:
        key = (link.trigger or "").strip().lower()
        if not key:
            continue
        if key in seen and key not in [d.lower() for d in dup]:
            dup.append((link.trigger or "").strip())
        seen.add(key)
    return dup


def preview_pair(link: SearchLink, sample: str = "") -> tuple[str, str]:
    """界面上的实时预览：返回 `(输入什么, 得到什么)`。

    search 类没填示例词时用「关键词」占位，这样预览行永远读得通。
    """
    _kind, payload = open_plan(link, sample)
    sample = sample.strip()
    if link.resolved_type == "search":
        return (f"{link.trigger} {sample or '关键词'}", payload)
    if link.resolved_type == "app" and not link.is_fixed:
        return (f"{link.trigger} {sample or '参数'}", payload)
    return (link.trigger, payload)


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------
@dataclass
class QbarConfig:
    """一次读取的结果。`error` 非空 = 配置有问题（界面要提示，但功能不瘫）。"""
    links: list[SearchLink] = field(default_factory=lambda: list(BUILTIN_LINKS))
    default_search: str = DEFAULT_TRIGGER
    browser: str = ""
    # 失焦即隐藏（默认开）：点别处 = 等价于再按一次 Caps+Q，
    # 这样下次 Caps+Q 一定是"从隐藏到显示"，一次就能唤起。
    hide_on_focus_out: bool = True
    path: Path = CONFIG_PATH
    error: str = ""

    @property
    def default_link(self) -> SearchLink | None:
        """default_search 指向的那条；找不到就退回第一条（且可选）。"""
        for link in self.links:
            if link.trigger == self.default_search:
                return link
        return self.links[0] if self.links else None

    def find(self, trigger: str) -> SearchLink | None:
        """按 trigger 查（大小写不敏感）。"""
        needle = trigger.strip().lower()
        for link in self.links:
            if link.trigger.lower() == needle:
                return link
        return None

    def match(self, text: str) -> "SearchMatch | None":
        """把输入解析成一条搜索/跳转候选。没命中返回 None（上层继续 fuzzy 搜应用/文件）。

        顺序很重要：**先长 trigger 后短 trigger**，否则 `g` 会抢走 `gh 关键词`。
        """
        text = text.strip()
        if not text:
            return None

        # ① 带空格：第一段是不是某个 trigger？
        head, sep, rest = text.partition(" ")
        if sep:
            link = self.find(head)
            if link is not None:
                keyword = rest.strip()
                if not keyword:
                    return None          # 只敲了「b 」→ 上层提示"输入关键词"
                return self._build(link, keyword)

        # ② 不带空格：从最长的 trigger 开始试
        #    单字母 trigger **必须带空格**才算前缀 —— 否则任何以 b/g/w 开头的普通词
        #    都会被当成"前缀 + 关键词"（实测：纯输入 `beta` 被解析成 `b` + `eta`，
        #    用户就永远搜不到 beta 这个词/这个应用了）。
        lowered = text.lower()
        for link in sorted(self.links, key=lambda l: len(l.trigger), reverse=True):
            trig = link.trigger.lower()
            if not trig:
                continue
            if lowered == trig:
                # 光敲 trigger：固定链接本身就是完整命令（`ghp` 打开我的仓库页），
                # 其余的是"缺关键词"，返回 None 让上层提示。
                return self._build(link, "") if link.is_fixed else None
            if len(trig) < 2:
                continue
            if lowered.startswith(trig):
                keyword = text[len(link.trigger):].strip()
                if not keyword:
                    return None
                return self._build(link, keyword)

        # ③ 纯输入（无前缀）→ 默认引擎搜索
        link = self.default_link
        if link is None:
            return None
        return self._build(link, text)

    def match_link(self, link: SearchLink, keyword: str = "") -> SearchMatch:
        """对**指定的那一条**做同样的拼装（设置界面的实时预览、"只敲了前缀"两种情况用）。

        公开这个入口是为了让上层不必去碰 `_build` 这个下划线方法。
        """
        return self._build(link, keyword)

    def _build(self, link: SearchLink, keyword: str) -> SearchMatch:
        kind, payload = open_plan(link, keyword)
        if kind == "url":
            if link.is_fixed:
                return SearchMatch(f"{link.name}（固定链接）", link.url, link.url, link, "",
                                   kind, payload)
            return SearchMatch(f"{link.name}搜索：{keyword}",
                               f"{link.trigger} {keyword} -> {link.target_url(keyword)}",
                               link.target_url(keyword), link, keyword, kind, payload)
        # 应用 / 文件：把"要执行的东西"原样当副标题显出来，用户一眼看得懂会跑什么
        label = LINK_TYPE_LABEL.get(link.resolved_type, link.resolved_type)
        return SearchMatch(f"{link.name}（{label}）", payload, payload, link, keyword, kind, payload)

    def no_keyword_hint(self, text: str) -> "SearchLink | None":
        """`b` / `b ` 这种"只敲了前缀没跟内容"的情况，返回该 trigger 对应的 link。

        固定链接不算"缺关键词"（它本来就整条打开），所以这里不返回。
        """
        stripped = text.strip()
        if not stripped:
            return None
        head, sep, rest = stripped.partition(" ")
        if sep:
            if rest.strip():
                return None
            link = self.find(head)
            return None if link is None or link.is_fixed else link
        for link in sorted(self.links, key=lambda l: len(l.trigger), reverse=True):
            if stripped.lower() == link.trigger.lower():
                return None if link.is_fixed else link
        return None


# ---------------------------------------------------------------------------
# 配置文件的文本（首次运行生成 / 设置界面写回，都走同一个 writer）
# ---------------------------------------------------------------------------
HEADER = [
    "# capsplus Qbar 快速链接配置",
    "#",
    "# 改完不用重启 Qbar：每次打开 Qbar 都会重读这个文件。",
    "# （用「capsplus-settings」界面改也行，它写回的就是这个文件）",
    "# trigger 冲突时**长前缀优先**（`gh 关键词` 走 gh，不会被 g 抢走）。",
    "# 单字母 trigger（b/g/w…）必须**带空格**才算前缀：`b 关键词` 是搜索，",
    "# 而 `beta` 这种普通输入不会被 `b` 吃掉（没有这条，以 b/g/w 开头的词全废）。",
    "# url 里放 {q} 表示把关键词做 URL 编码填进去；想原样嵌入（保留空格）用 {q_raw}；",
    "# 两者都不放 = 固定链接，整条直接打开（如站点首页、某篇文档）。",
    "# 注：# 后面必须留一个空格，否则 tomllib 会把它当成键值对报错。",
    "#",
    "# type = 这条链接的类型（不写就按老规则推断：有 {q} = search，没有 = url）：",
    "#   search 网站内搜索（目标必须有 {q}/{q_raw}）   url 固定网址（目标不许有 {q}）",
    "#   app    本地应用：.desktop 名（firefox）或一条命令，命令里 {q} 当参数（原样）",
    "#   file   文件或目录路径：~ 会展开；固定路径，不支持 {q}",
    "# 提醒：设置界面会重写 [general] 与每条 [[link]]，**写在条目中间的注释会被模板注释顶掉**；",
    "#       自己的说明请写在文件开头（开头那段注释界面会原样带回去）。",
]


def toml_str(value: str) -> str:
    """TOML 基本字符串转义（界面里用户可能敲引号/反斜杠，不许写出坏配置）。"""
    out = (value or "")
    out = out.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


def render_default_toml() -> str:
    """生成默认 qbar.toml 的正文（含中文注释与全部默认引擎）。"""
    return render_config_toml(list(BUILTIN_LINKS), DEFAULT_TRIGGER, "", True)


def render_config_toml(links: list[SearchLink], default_search: str = DEFAULT_TRIGGER,
                       browser: str = "", hide_on_focus_out: bool = True,
                       header: str | None = None) -> str:
    """把配置渲染成 TOML 文本：**顺序就是传进来的顺序**，注释来自模板（tomllib 只读）。

    `header` = 文件开头那段注释（设置界面会传原文件的开头注释回来，用户手写的说明不丢）。
    """
    lines = list(header.splitlines()) if header else list(HEADER)
    lines += [
        "",
        "[general]",
        f'default_search = {toml_str(default_search)}     # 不带前缀的输入用哪个引擎（填某个 link 的 trigger）',
        f'browser = {toml_str(browser)}             # 留空 = 系统默认浏览器（xdg-open）；填了 = 用命令模板',
        '                         # 模板里 {url} 会被替换成真实地址，如 "firefox --new-tab {url}"',
        f'hide_on_focus_out = {"true" if hide_on_focus_out else "false"} # 点别处就收起来（等价于再按一次 Caps+Q，下次按一下就能唤起）',
        "",
    ]
    for link in links:
        lines.append("[[link]]")
        lines.append(f'trigger = {toml_str(link.trigger)}')
        lines.append(f'name = {toml_str(link.name)}')
        lines.append(f'type = {toml_str(link.resolved_type)}')
        lines.append(f'url = {toml_str(link.url)}')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def leading_comments(text: str) -> str:
    """文件**开头**那段注释（连续的 # 行 / 空行）。写回时原样带回去 = 用户说明不丢。

    只认开头：条目中间的注释要保真就得自己做一份保留注释的 TOML 解析器，
    代价远大于收益（界面会重写整个 [[link]] 块，见文件头那句提醒）。
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            out.append(line)
            continue
        break
    return "\n".join(out).strip("\n")


def save_config(links: list[SearchLink], default_search: str, browser: str,
                hide_on_focus_out: bool, path: Path | None = None) -> Path:
    """原子写回 qbar.toml：先写 .tmp 再 replace；开头注释从原文件带回来。"""
    target = Path(path) if path else CONFIG_PATH
    header = ""
    try:
        header = leading_comments(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        header = ""
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_config_toml(links, default_search, browser, hide_on_focus_out, header or None)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return target


def ensure_config_file(path: Path | None = None) -> Path:
    """配置文件不在就写一份默认的（父目录一起建）。返回路径。"""
    target = Path(path) if path else CONFIG_PATH
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(render_default_toml(), encoding="utf-8")
        tmp.replace(target)
    return target


# ---------------------------------------------------------------------------
# 读取与校验
# ---------------------------------------------------------------------------
def load_config(path: Path | None = None, create: bool = True) -> QbarConfig:
    """读配置。任何解析/取值错误都**不抛异常**，而是落进 `QbarConfig.error` 并退回内置默认。

    create=True 时文件不存在会自动生成一份默认的（首次运行体验）。
    """
    import tomllib

    target = Path(path) if path else CONFIG_PATH
    if create and not target.exists():
        try:
            ensure_config_file(target)
        except OSError as exc:
            return QbarConfig(path=target, error=f"无法写入默认配置: {exc}")

    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return QbarConfig(path=target)                      # 不生成也不报错，用默认
    except OSError as exc:
        return QbarConfig(path=target, error=f"读不了文件: {exc}")
    except tomllib.TOMLDecodeError as exc:
        return QbarConfig(path=target, error=f"TOML 语法错误: {exc}")
    except UnicodeDecodeError as exc:
        return QbarConfig(path=target, error=f"不是 UTF-8 文本: {exc}")

    if not isinstance(raw, dict):
        return QbarConfig(path=target, error="顶层不是一个表")

    general = raw.get("general", {})
    if general is None:
        general = {}
    if not isinstance(general, dict):
        return QbarConfig(path=target, error="[general] 不是一个表")

    errors: list[str] = []

    default_search = general.get("default_search", DEFAULT_TRIGGER)
    if not isinstance(default_search, str) or not default_search.strip():
        errors.append("default_search 必须是非空字符串")
        default_search = DEFAULT_TRIGGER
    default_search = default_search.strip()

    browser = general.get("browser", "")
    if not isinstance(browser, str):
        errors.append("browser 必须是字符串")
        browser = ""

    hide_on_focus_out = general.get("hide_on_focus_out", True)
    if not isinstance(hide_on_focus_out, bool):
        errors.append("hide_on_focus_out 必须是 true / false")
        hide_on_focus_out = True

    raw_links = raw.get("link", [])
    if raw_links is None:
        raw_links = []
    if isinstance(raw_links, dict):          # 只写了一个 [[link]] 时 tomllib 给的是 dict
        raw_links = [raw_links]
    if not isinstance(raw_links, list):
        return QbarConfig(path=target, default_search=default_search, browser=browser,
                          error="link 必须是 [[link]] 数组")

    links: list[SearchLink] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_links, 1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 条 link 不是表")
            continue

        def text_of(key: str, default: object = "") -> str:
            value = item.get(key, default)
            return value if isinstance(value, str) else ""

        # `target` 是 `url` 的别名（app / file 类用"目标"更顺），两个键都认、只存一份
        # 变量名绝不能用 `target`：外层的 `target` 是配置文件路径，被顶掉就把
        # `QbarConfig.path` 变成了网址（2026-10-15 单测真的红过这条）
        destination = text_of("url") or text_of("target")
        link = SearchLink(text_of("trigger"), text_of("name"), destination.strip(), text_of("type"))

        problems = link_problems(link, tuple(seen))
        if problems:
            for msg in problems:
                errors.append(f"第 {index} 条 link {msg}")
            continue
        seen.add(link.trigger.strip().lower())
        links.append(SearchLink(link.trigger.strip(), link.name.strip(), link.url.strip(),
                                (link.type or "").strip().lower()))

    if not raw_links:
        errors.append("没有定义任何 [[link]]，已用内置默认引擎")
    elif not links:
        errors.append("所有 link 都不可用，已用内置默认引擎")
    if not links or not raw_links:
        links = list(BUILTIN_LINKS)

    if not any(link.trigger == default_search for link in links):
        errors.append(f"default_search={default_search!r} 没有对应的 link")

    return QbarConfig(links=links, default_search=default_search, browser=browser,
                      hide_on_focus_out=hide_on_focus_out,
                      path=target, error="；".join(errors))


def open_command(browser: str, url: str) -> str:
    """返回打开 url 的 shell 命令：配了 browser 就用模板，否则走系统默认（xdg-open）。

    `{url}` 占位会被**引号包住**的地址替换（关键词里可能有 `&`、空格，不能裸着进 shell）。
    """
    browser = (browser or "").strip()
    quoted = shell_quote(url)
    if not browser:
        return f"xdg-open {quoted}"
    if "{url}" in browser:
        return browser.replace("{url}", quoted)
    return f"{browser} {quoted}"


def shell_quote(text: str) -> str:
    import shlex
    return shlex.quote(text)


# ---------------------------------------------------------------------------
# 指令式输入（search_cmd）
# ---------------------------------------------------------------------------
SEARCH_CMD_WORDS = ("q", "search", "搜")


def parse_search_cmd(body: str) -> tuple[str, str]:
    """解析 `-> q 关键词` 这类指令体。返回 (动作, 关键词)。

    动作取值：`list`（只敲了 `-> q`）/ `hint`（敲了 `q` 没跟关键词）/ `search` / `none`。
    不走 QbarConfig.match()，因为 `-> q` 是**显式指定走默认引擎**：
    此时关键词不该再被当作 trigger 解析（`-> q b 关键词` 应该搜 "b 关键词" 本身）。
    """
    words = re.split(r"\s+", body.strip())
    if not words or words[0] == "":
        return ("list", "")
    if words[0] not in SEARCH_CMD_WORDS:
        return ("none", "")
    keyword = body.strip()[len(words[0]):].strip()
    if not keyword:
        return ("hint", "")
    return ("search", keyword)
