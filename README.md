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
qbar/           GTK3 浮层启动器（capsplus-qbar）+ 不依赖 gi 的纯逻辑层 qbar/logic.py
bin/            capsplus / capsplus-tray / capsplus-qbar / keyd-qbar-cmd
assets/         自画的两个托盘 SVG（启用/暂停）
packaging/      autostart .desktop 与 systemd --user unit 模板
tests/          标准库 unittest（无 pytest 依赖）
tools/gen-readme.py  由模板 + 映射数据生成 README
```

## 安装

```bash
bash install.sh              # bin 软链到 ~/.local/bin + 装 autostart + 依赖检查
bash install.sh --no-autostart
```

依赖（本机已装）：`python3-gi`、`gir1.2-gtk-3.0`、`gir1.2-ayatanaappindicator3-0.1`、
`fdfind`（fd 的 Debian 名）、`keyd`（二进制名 `keyd.rvaiya`）、GNOME 扩展
`ubuntu-appindicators@ubuntu.com`（托盘才有地方显示）。

`install.sh` **不会**碰 `/etc/keyd` —— 配置由你自己跑 `capsplus apply` 并审 diff。

## 用法

```bash
capsplus render          # 渲染 keyd 配置到 stdout（看一眼再决定）
capsplus apply           # 备份 -> 装到 /etc/keyd/capslock-plus.conf -> reload -> 查错
capsplus apply --dry-run # 只渲染并自检，不碰系统
capsplus status          # 服务状态 + 当前配置摘要 + 与渲染是否一致 + 最近一条日志

capsplus-tray            # 托盘：启停映射 / 重载配置 / 打开 Qbar / 打开映射文件 / 最近日志 / 关于
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

| 组合键 | 功能 |
| --- | --- |
| `Caps+e` / `Caps+d` / `Caps+s` / `Caps+f` | 光标 上/下/左/右 |
| `Caps+a` / `Caps+g` | 按词左/右跳 |
| `Caps+t` / `Caps+b` | 大段上/下滚（10 行 / 增强：翻页） |
| `Caps+i` / `Caps+k` / `Caps+j` / `Caps+l` | 选中 上/下/左/右 |
| `Caps+h` | 选中到词首 |
| `Caps+u` / `Caps+o` | 选中到行首/行尾 |
| `Caps+y` / `Caps+n` | 大段选中（7 行 / 增强：选中翻页） |
| `Caps+dot` | 选中到词尾 |
| `Caps+comma` | 选中当前词 |
| `Caps+w` / `Caps+r` | 删词 / 删字符 |
| `Caps+backspace` | 删整行 |
| `Caps+slash` | 删到行尾 |
| `Caps+p` / `Caps+semicolon` | 行首 / 行尾 |
| `Caps+c` / `Caps+x` / `Caps+v` | 复制 / 剪切 / 粘贴（系统剪贴板） |
| `Caps+q` | 打开 Qbar（启动器） |
| `Caps+space` | 回车 |
| `Caps+enter` | 到行尾并回车 |

再按住 Alt 进入 `capl+alt` 增强层（原版规律：Alt = 步长更大 / 粒度更粗）：

| 组合键 | 功能 |
| --- | --- |
| `Caps+Alt+e` / `Caps+Alt+d` / `Caps+Alt+s` / `Caps+Alt+f` | 光标大步移动（3 行 / 5 字符） |
| `Caps+Alt+a` / `Caps+Alt+g` | 按词跳 3 次 |
| `Caps+Alt+t` / `Caps+Alt+b` | 上/下翻页（近似的 30 行） |
| `Caps+Alt+i` / `Caps+Alt+k` | 选中上/下 3 行 |
| `Caps+Alt+j` / `Caps+Alt+l` | 选中左/右 5 字符 |
| `Caps+Alt+h` | 选中左 3 个词 |
| `Caps+Alt+u` / `Caps+Alt+o` | 选中到文首/文尾（C-home / C-end） |
| `Caps+Alt+y` / `Caps+Alt+n` | 选中上/下翻页 |
| `Caps+Alt+dot` | 选中到词尾 ×3 |
| `Caps+Alt+comma` | 选中整行（home 起、S-end 止） |
| `Caps+Alt+r` | 删到词尾 |
| `Caps+Alt+w` | 删到词首 |
| `Caps+Alt+backspace` | 全选删除（等价清空全文） |
| `Caps+Alt+slash` | 删到文尾 |
| `Caps+Alt+p` / `Caps+Alt+semicolon` | 文首 / 文尾 |

## Qbar 用法

| 输入 | 行为 |
| --- | --- |
| 直接打字 | fuzzy 子序列匹配：`.desktop` 应用 + `~/project`、`~`、`/mnt/data`、`/mnt/back` 下的文件/目录（fdfind，1.5s 超时） |
| `=` 开头 | 算术求值，如 `=1+2*3`；结果回车即复制。用 `ast` 白名单求值，**不用 eval** |
| `-> date` / `-> time` | 日期 / 时间（回车复制） |
| `-> calc <表达式>` | 算术，同上 |
| `-> q <关键词>` | 浏览器搜索 |
| `http(s)://…` / `xxx.com` | 浏览器打开 |

键盘：`Esc` 关闭、`↑/↓` 选择、`Enter` 打开。历史记在
`~/.local/state/capsplus/qbar-history.json`，最近用过的会往前排。
单实例走 `~/.local/state/capsplus/qbar.sock`：已在运行时再启动 = 切换显示/隐藏。

## 与原版 Capslock+ 的差异

未实现（**已知差异，不是 bug**）：

- **独立剪贴板 B 组**（`Caps+Alt+C/X/V`）：Linux 没有系统级第二剪贴板（keyd 只发按键，
  要第二剪贴板得靠 copyq 这类程序）；keyd 侧不做。
- **Qbar 的 `->` 快速添加设置项**、**文件夹上下级浏览**：没做，Qbar 只做搜索/打开。
- **WinBind 切窗口**：Wayland 下客户端没有"切到指定窗口"的 API（GNOME 只能 Super+数字近似）。
- **TabScript（JS 脚本）**：没做。
- **F2 数学板 / F3 翻译 / F4 透明 / F6 置顶 / F12 切剪贴板**：没做；其中"置顶/透明"在 Wayland
  下没有客户端 API（合成器不给）。
- **热字符串（自动替换）**：没做（espanso 那条线在 Wayland 支持也有限）。

与原版一致的部分：短按 Caps = 大小写切换；`capl` / `capl+alt` 两层的分层思路与键位取舍。
**步长有两处受 keyd 宏上限压缩**（见下面「硬件与 keyd 的边界」）：选中 10 行 → 7 行；
30 行 → 翻页键。

多出来的部分：托盘（启停映射、重载、看日志）、Qbar 走 fdfind 搜真实文件、系统剪贴板直接可用。

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
python3 -m unittest discover -s tests -v     # 40 个用例，纯标准库
python3 tools/gen-readme.py                  # 重新生成 README（含键表）
```

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
