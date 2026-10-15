# capsplus —— Linux（Ubuntu 26.04 / GNOME Wayland）版 Capslock+

把 Windows 上的 **Capslock+ v3.3.0** 的核心能力搬到本机：按住 CapsLock 变成一层导航/编辑键，
外带一个托盘和一个 Qbar 启动器。键位映射的**唯一真相源**是 `capsplus/mapping_data.py`，
keyd 配置、README 键表都由它渲染出来 —— 改映射不用改两处。

键盘层落点是 **keyd**（内核 evdev/uinput 层）。本机是纯 Wayland（GNOME 50，Xwayland 进程 0），
xcape / xmodmap / AutoKey 那类 X11 工具全部出局，keyd 是唯一通吃的路线。

## 目录

```
capsplus/       CLI 与映射真相源（render / apply / status）
  mapping_data.py   键位数据 + 分组注释（改这里）
  render.py         渲染 keyd 配置 / 归一化校验 / 生成 README 键表
  apply.py          备份 -> 安装 -> reload -> 查错 -> 必要时回滚
tray/           GTK3 + AyatanaAppIndicator3 托盘（capsplus-tray）
qbar/           GTK3 浮层启动器（capsplus-qbar）
  logic.py          不依赖 gi 的纯逻辑层（fuzzy / 算术 / 历史 / 搜索编排）
  search_config.py  快速链接与网页搜索的 TOML 配置（同样不 import gi）
bin/            capsplus / capsplus-tray / capsplus-qbar / keyd-qbar-cmd
assets/         自画的两个托盘 SVG（启用/暂停）
packaging/      应用菜单 .desktop（只放 Qbar 一条）、autostart .desktop、systemd --user unit
tests/          标准库 unittest（无 pytest 依赖）
tools/gen-readme.py  由模板 + 映射数据生成 README
```

## 安装

```bash
bash install.sh              # bin 软链 + 应用菜单入口 + autostart + 依赖检查
bash install.sh --no-autostart   # 不装开机自启（应用菜单入口照装）
```

装完后 GNOME **应用网格里能搜到「capsplus Qbar」和「capsplus 托盘」**，可以直接固定到 Dock
（`.desktop` 落在 `~/.local/share/applications/`，`Terminal=false`、`Categories=Utility;`）。
装完会跑一次 `desktop-file-validate`；本机没有这个命令就跳过并打印说明。

依赖（本机已装）：`python3-gi`、`gir1.2-gtk-3.0`、`gir1.2-ayatanaappindicator3-0.1`、
`fdfind`（fd 的 Debian 名）、`keyd`（二进制名 `keyd.rvaiya`）、GNOME 扩展
`ubuntu-appindicators@ubuntu.com`（托盘才有地方显示）。
TOML 解析用 Python 3.11+ 自带的 `tomllib`，**不需要额外装包**。

`install.sh` **不会**碰 `/etc/keyd` —— 配置由你自己跑 `capsplus apply` 并审 diff。

## 用法

```bash
capsplus render          # 渲染 keyd 配置到 stdout（看一眼再决定）
capsplus apply           # 备份 -> 装到 /etc/keyd/capslock-plus.conf -> reload -> 查错
capsplus apply --dry-run # 只渲染并自检，不碰系统
capsplus status          # 服务状态 + 当前配置摘要 + 与渲染是否一致 + 最近一条日志

capsplus-tray            # 托盘：打开 Qbar / 编辑快速链接 / 启停映射 / 重载 / 打开映射文件 / 最近日志 / 关于
capsplus-qbar            # Qbar：第一次起窗口，已在运行则切换显示/隐藏
capsplus-qbar --hidden   # 常驻不显示（给 keyd 唤起用）
```

`capsplus apply` 的输出语义：成功打印 `OK 已应用并 reload；备份: <路径>`；
keyd 报错时**自动回滚**到被本次替换掉的那一版、再 reload，然后以非 0 退出并把 `ERROR:` 行打出来。

### 交给 keyd 唤起 Qbar

keyd 的 `command(...)` 是 **root** 执行的，拉 GUI 必须降权 + 带会话环境，包装脚本已经写好：

```
command(/home/zmj/.local/bin/keyd-qbar-cmd)
```

`bin/keyd-qbar-cmd` 里就是这条（`runuser -u zmj -- env XDG_RUNTIME_DIR=… WAYLAND_DISPLAY=… DISPLAY=:0
DBUS_SESSION_BUS_ADDRESS=… capsplus-qbar`）。**加进 keyd 配置要你自己做**，我不动 `/etc/keyd`。

### 免密 sudo（可选，托盘按钮要它才好用）

托盘的「暂停映射 / 重载配置」调用 `sudo -n`，没有免密会直接报错（不会挂在密码提示上）。
要免密就在 `/etc/sudoers.d/capsplus` 里放宽这几条**具体命令**（别用 `NOPASSWD: ALL`）：

```
zmj ALL=(root) NOPASSWD: /usr/bin/keyd.rvaiya
zmj ALL=(root) NOPASSWD: /usr/bin/systemctl start keyd, /usr/bin/systemctl stop keyd
zmj ALL=(root) NOPASSWD: /usr/bin/cp * /etc/keyd/capslock-plus.conf
zmj ALL=(root) NOPASSWD: /usr/bin/journalctl -u keyd *
```

这文件归你自己按下去（它属于"动 sudo 面"），我不替你装。

## 键位表（由 `capsplus/mapping_data.py` 生成，`tools/gen-readme.py` 重新生成）

短按 Caps 仍是**原大小写切换**（`overload(capl, capslock)`），与原版一致。按住 Caps 进入 `capl` 层：

@@CAPL_TABLE@@

再按住 Alt 进入 `capl+alt` 增强层（原版规律：Alt = 步长更大 / 粒度更粗）：

@@ALT_TABLE@@

## Qbar 用法

**网页搜索是 Qbar 的主力用法**：敲 `b 关键词` 直接搜，敲 `gh 关键词` 去 GitHub 搜，
不带前缀的输入按默认引擎搜。搜什么、有哪些引擎，全由 `~/.config/capsplus/qbar.toml` 决定，
改完不用重启（每次打开 Qbar 都会重读）。

| 输入 | 行为 |
| --- | --- |
| `b 关键词` | 用 trigger 为 `b` 的引擎搜索（默认必应），回车即开浏览器 |
| `gh 关键词` | **最长前缀优先**：`gh` 命中 GitHub，不会被 `g` 抢走 |
| `纯关键词` | 不带前缀 → 用 `general.default_search` 指的引擎搜索（这条排第一，应用/文件紧随其后）|
| `ghp` | 固定链接（模板里没有 `{q}`）→ 直接打开，不要关键词 |
| `b ` / `b` | 只敲了前缀 → 提示「输入关键词」 |
| `-> q 关键词` | 老写法，保留：等价于默认引擎搜索 |
| 直接打字 | fuzzy 子序列匹配：`.desktop` 应用 + `~/project`、`~`、`/mnt/data`、`/mnt/back` 下的文件/目录（fdfind，1.5s 超时） |
| `=` 开头 | 算术求值，如 `=1+2*3`；结果回车即复制。用 `ast` 白名单求值，**不用 eval** |
| `-> date` / `-> time` | 日期 / 时间（回车复制） |
| `-> calc <表达式>` | 算术，同上 |
| `-> link` | 打开快速链接配置文件 |
| `http(s)://…` / `xxx.com` | 浏览器打开（真网址优先，不会被单字母 trigger 抢走） |

键盘：`Esc` 关闭、`↑/↓` 选择、`Enter` 打开。历史记在
`~/.local/state/capsplus/qbar-history.json`，最近用过的会往前排。
单实例走 `~/.local/state/capsplus/qbar.sock`：已在运行时再启动 = 切换显示/隐藏。

### 快速链接配置 `~/.config/capsplus/qbar.toml`

首次打开 Qbar 时自动生成一份带中文注释的默认配置；托盘右键「编辑快速链接」也能直接打开它。
要加一个新引擎，往文件末尾追加一段 `[[link]]` 就行：

```toml
[[link]]
trigger = "gh"                              # 前缀，最长优先；单字母必须带空格才能当前缀用
name = "GitHub"                             # 显示名（结果行里出现）
url = "https://github.com/search?q={q}"     # {q} = 关键词做 URL 编码填进来
```

| 字段 | 位置 | 说明 |
| --- | --- | --- |
| `default_search` | `[general]` | 不带前缀的输入用哪个引擎。填某个 link 的 `trigger`；填了不存在的值会在结果里提示，并退回第一条可用引擎 |
| `browser` | `[general]` | 留空 = 系统默认浏览器（xdg-open）；填了 = 用命令模板，如 `"firefox --new-tab {url}"` |
| `trigger` | `[[link]]` | 前缀，**不许重复、不许含空格**；重复的会被跳过并在结果里提示 |
| `name` | `[[link]]` | 结果行里的引擎名，如「必应搜索：关键词」 |
| `url` | `[[link]]` | URL 模板。`{q}` = 关键词 URL 编码（空格变 `+`）；`{q_raw}` = 原样嵌入（给 `ghcli cli` 这类要保留空格的路径）；**两个都不放 = 固定链接**，整条直接打开 |

三种 `url` 各举一例（默认配置里都能看到）：

```toml
url = "https://www.bing.com/search?q={q}"     # b 中文   → q=%E4%B8%AD%E6%96%87
url = "https://github.com/cli/{q_raw}"        # ghcli cli → github.com/cli/cli
url = "https://github.com/pulls"              # ghp      → 固定链接，直接开
```

内置默认引擎的 trigger（都可以在配置文件里改）：

| trigger | 名称 | trigger | 名称 |
| --- | --- | --- | --- |
| `b` | 必应（默认搜索） | `zh` | 知乎 |
| `g` | Google | `bl` | B站 |
| `bd` | 百度 | `tr` | 翻译（有道） |
| `w` | 维基中文 | `ghcli` | GitHub CLI 仓库（`ghcli cli` → github.com/cli/cli） |
| `gh` | GitHub | `ghp` | GitHub 我的仓库（固定链接） |

**单字母 trigger 只在带空格时当前缀**：`b 关键词` 是搜索，而 `beta` 是普通输入
（否则任何以 b/g/w 开头的词都会被当成"前缀 + 关键词"，用户就再也搜不到这些词和应用了）。

`looks_like_url()` 认得出的真网址（`https://…`、`example.com/path`）**永远优先**，
不会因为首字母撞上某个 trigger 就被当成搜索词。

配置文件写坏了**不会崩**：结果列表最上面会出现一行「⚠ 快速链接配置有误」，
副标题写清原因（如 `TOML 语法错误: … at line 3`），回车打开配置去改；
同时自动退回内置默认引擎，Qbar 照常能搜。

## 与原版 Capslock+ 的差异

**明确不做**（2026-10-15 明江拍板；不是"待办"，别再来回讨论）：

- **独立剪贴板 B 组**（`Caps+Alt+C/X/V`）—— **明确不做**。Linux 没有系统级第二块剪贴板：
  X11/Wayland 的选区模型里 `PRIMARY`（选中即复制、中键粘贴）是唯一"第二剪贴板"，
  它不是可写入的独立槽位；keyd 又只发按键、不碰剪贴板。要做只能靠 copyq 这类常驻程序
  自建槽位，那是另一个软件的事，与本项目的键位映射层无关。
- **WinBind 切窗口** —— **明确不做**。Wayland 下客户端**没有**"切换到指定窗口"的 API
  （合成器不给，`wlr-foreign-toplevel` 一类协议 GNOME 也没有），唯一近似是 GNOME 的
  `Super+数字` 按固定顺序切，既绑不住"这个窗口"也无法按标题/进程找，做了也是假功能。

其余未实现（**已知差异，不是 bug**）：

- **Qbar 的 `->` 快速添加设置项**、**文件夹上下级浏览**：没做，Qbar 只做搜索/打开。
- **TabScript（JS 脚本）**：没做。
- **F2 数学板 / F3 翻译 / F4 透明 / F6 置顶 / F12 切剪贴板**：没做；其中"置顶/透明"在 Wayland
  下没有客户端 API（合成器不给）。
- **热字符串（自动替换）**：没做（espanso 那条线在 Wayland 支持也有限）。

与原版一致的部分：短按 Caps = 大小写切换；`capl` / `capl+alt` 两层的分层思路与键位取舍。
**步长有两处受 keyd 宏上限压缩**（见下面「硬件与 keyd 的边界」）：选中 10 行 → 7 行；
30 行 → 翻页键。

多出来的部分：托盘（启停映射、重载、看日志）、Qbar 走 fdfind 搜真实文件、系统剪贴板直接可用、
**可配置的网页搜索与快速链接**（原版 Qbar 的搜索是写死的）。

## 回滚

1. **自动回滚**：`capsplus apply` 抓到 `ERROR:` 就自己回滚 + reload，并把错误行打出来。
2. **手工回滚**：备份在 `~/.local/state/capsplus/keyd-conf-backup/`（保留 10 份，超出部分
   `mv` 进焚化炉 `/tmp/attic-<日期>/`，不用 `rm`）。
   ```bash
   ls -t ~/.local/state/capsplus/keyd-conf-backup/ | head
   sudo cp ~/.local/state/capsplus/keyd-conf-backup/capslock-plus.conf.<时间戳> /etc/keyd/capslock-plus.conf
   sudo keyd.rvaiya reload
   ```
3. **整份撤掉**：`sudo mv /etc/keyd/capslock-plus.conf /tmp/attic-$(date +%Y%m%d)/ && sudo systemctl restart keyd`
4. **救命组合**：同时按 `backspace` + `escape` + `enter` 会终止 keyd（配置改坏时不至于锁死机器）。

## 硬件与 keyd 的边界（都是 2026-10-15 用虚拟键盘实测出来的，不是推测）

### 宏长度上限：≤ 15 个按键事件

keyd 的一条 `macro(...)` 里累计发出的按键事件**超过 15 个就不可靠**：

| 宏内容 | 事件数 | 实测结果 |
| --- | --- | --- |
| `up ×14` | 14 | ✅ 14 个 |
| `up ×15` | 15 | ✅ 15 个 |
| `up ×16` | 16 | ❌ 一个都不发 |
| `up ×20 / ×30 / ×40` | 20/30/40 | ❌ 只发 4 / 14 / 8 个（乱发，数量不单调，像缓冲区溢出）|
| `S-down ×7` | 14 | ✅ 7 次选中 |
| `S-down ×8` | 16 | ❌ 一个都不发 |

因此原版 Capslock+ 的 30 次重复（`moveUp(30)` / `selectUp(30)`）在本机**做不到**，
按语义等价改成了翻页键；10 行的选中（= 20 个事件）也超界，压到 7 行：

| 键 | 原版 | 本机实现 | 说明 |
| --- | --- | --- | --- |
| `Caps+T/B` | 上/下 10 行 | `macro(up/down ×10)` | 一致（10 ≤ 15）|
| `Caps+Y/N` | 选中 10 行 | `macro(S-up/S-down ×7)` | 压到 7 行（14 个事件）|
| `Caps+Alt+T/B` | 上/下 30 行 | `pageup` / `pagedown` | 一页 ≈ 30 行 |
| `Caps+Alt+Y/N` | 选中 30 行 | `S-pageup` / `S-pagedown` | 选中一页 |

### 复合层会多吐一对 `Alt+Ctrl` 空点

按住 `Caps+Alt+任意键` 时，keyd 在真正发映射键的前后各吐一次
`leftalt↓ leftctrl↓ leftalt↑ leftctrl↑`（实测每个 `capl+alt` 动作都如此，与层名无关）。
这是 keyd 处理复合层的修饰键状态所致，**不是配置错误**；影响面：少数程序看到 `Alt` 轻点会闪一下
菜单栏。不想要这一层就删掉 `capsplus/mapping_data.py` 里的 `CAPL_ALT` 段。

## 三条硬规矩（踩过，别忘）

1. **`macro(...)` 行尾不许有注释** —— keyd 的宏行解析器会吃掉行尾剩余内容，把注释当宏 token，
   整行报 `invalid key or action`。渲染器 `render()` 只输出 `键 = 值`，注释一律单独成行；
   `normalized_bindings()` 碰到带行尾注释的 macro 行会直接抛异常。
3. **宏别超过 15 个事件**（见上一节），也别相信 `apply` 报的成功 —— 宏超界 keyd 不报错、只是静默不发。
2. **`capsplus apply` 的三步顺序**：先裁保留窗口腾位 → 再快照（= 这次替换掉的那一版）→
   才安装。顺序错了会出现"日志说已回滚、文件还是坏的"（2026-10-15 演练抓到的真 bug，
   `tests/test_apply.py` 里有回归断言）。

## 开发

```bash
python3 -m unittest discover -s tests -v     # 94 个用例，纯标准库（无 pytest）
python3 tools/gen-readme.py                  # 重新生成 README（含键表）
```

`tests/test_qbar_search.py` 专门锁快速链接的行为：前缀最长匹配、单字母 trigger 必须带空格、
默认引擎、URL 编码（中文/空格/`&`）、`{q_raw}`、固定链接、坏 TOML 容错、
`-> q` 兼容、真网址不被抢、改完配置重读生效。

**真机端到端验收**（需要 root + `/dev/uinput`；造一个满键位虚拟键盘让 keyd 处理，再读回
keyd 虚拟键盘发出的键，逐条比对期望）：

```bash
sudo python3 tools/e2e/check_mappings.py     # 30 条映射全绿 = 配置真的生效
```

它比 `capsplus render` 强的地方：render 只证明"配置能解析"，这个能证明"按下去真的发出这些键"。
只测无破坏性的移动/选中类；剪贴板、回车、删整行类会打到当前焦点窗口，请人肉验。

- 改键位只改 `capsplus/mapping_data.py`，然后 `capsplus render` 看结果、`capsplus apply` 落地。
- `qbar/logic.py` 刻意不 import `gi`，所以 fuzzy / 算术 / 历史 / .desktop 解析都能在
  没有显示服务器的环境下直接跑单测。
