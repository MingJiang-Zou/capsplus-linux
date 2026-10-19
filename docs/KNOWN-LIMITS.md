# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）。
# 这是文档，不是代码；写在这里的每条都带"哪天验的 + 怎么验的"。

# 已知平台限制（实测，不是推测）

面向对象：想在别的机器上跑本仓的人。下面每一条都决定了"为什么代码长成这样"，
所以每条都写了**实测日期**与**复现方法**。日期集中在这两天：

- 本机事实：Ubuntu 26.04.1 LTS / GNOME Shell 50.1 / keyd v2.5.0（`keyd.rvaiya --version`）
  / Python 3.14（`python3 -V`）；桌面会话 `loginctl show-session 3 -p Type` → `Type=wayland`。

---

## 一、keyd 侧

### 1. `macro(...)` 单条最多 15 个按键事件，超了**整条静默丢掉**

| 宏内容 | 事件数 | 结果 |
| --- | --- | --- |
| `up ×14` / `up ×15` | 14 / 15 | ✅ 全发出来 |
| `up ×16` | 16 | ❌ 一个都不发 |
| `up ×20 / ×30 / ×40` | 20/30/40 | ❌ 只发 4 / 14 / 8 个（数量不单调，像缓冲区溢出） |
| `S-down ×7` | 14 | ✅ |
| `S-down ×8` | 16 | ❌ 一个都不发 |

- **实测日期**：2026-10-15。
- **怎么验的**：`sudo python3 tools/e2e/check_mappings.py` —— 用 `/dev/uinput` 造一个满键位的虚拟键盘，
  让 keyd 处理它，再用 `python-evdev` 读回 keyd 输出侧的按键事件，逐条比对计数。
  所以这里数的是**事件个数**（`up down` 算 2 个），不是"按几下"。
- **后果**：原版 Capslock+ 的"上/下 30 行"（`moveUp(30)`）在本机做不到 → 改 `pageup`/`pagedown`；
  "选中 10 行"（= 20 个事件）超界 → 压到 7 行（= 14 个事件）。`capsplus/settings_logic.py`
  的宏事件计数会在设置界面上出黄字提醒（单测 `tests/test_settings_logic.py` 锁住）。
- **注意**：keyd **不报错**，`capsplus apply` 也照样报成功 —— 光看日志发现不了。

### 2. `macro(...)` 行的行尾不许写注释

- **实测日期**：2026-10-15。
- **现象**：`e = macro(up up)  # 说明` 整行报 `invalid key or action`；keyd 的宏行解析器把行尾
  剩余内容（含 `#` 注释）都当宏 token 吃进去。
- **怎么验的**：手工往 `/etc/keyd/` 丢一条带行尾注释的宏行，`journalctl -u keyd` 里就出现那行 ERROR。
- **代码里的落点**：`capsplus/render.py` 对 macro 行**只输出** `键 = 值`，注释一律单独成行；
  `normalized_bindings()` 碰到带行尾注释的 macro 行直接抛 `ValueError`
  （`tests/test_render.py::test_normalized_bindings_rejects_macro_comment` 锁住）。

### 3. `command(...)` 以 **root** 执行

- **实测日期**：2026-10-15 起用于生产，2026-10-16 复核。
- **怎么验的**：`ps -o user,cmd -C keyd.rvaiya` → `root /usr/bin/keyd.rvaiya`（keyd 本身是 root 常驻进程，
  `command()` 是它 fork 出来的，所以也是 root）。
- **后果**：`command()` 里不能直接写 GUI 程序 —— root 起的进程连不进用户的 Wayland 会话。
  本仓的做法是套一层 `bin/keyd-qbar-cmd`：用 `runuser` 降权回**这个脚本文件的属主**，
  再补上 `XDG_RUNTIME_DIR` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS`。
  用户名、uid、家目录都不写死（`CAPSPLUS_USER` 可强制指定）。
- **另外**：`command()` 的参数是按空白切的，**路径里不许有空格**；实测加引号也没用
  （引号会被原样当字符发出去），所以 `capsplus/env.py:resolve_bindir_token()` 遇到带空格的用户
  bin 目录直接报错，不给你写出坏配置。

### 4. 可执行文件名各发行版不一样，systemd 单元名反倒一样

- **实测日期**：2026-10-16（本机）+ 上游打包事实。
- **怎么验的**：
  ```bash
  systemctl cat keyd          # /usr/lib/systemd/system/keyd.service
  #   ExecStart=/usr/bin/keyd.rvaiya   ← 单元名 keyd，二进制名 keyd.rvaiya
  command -v keyd.rvaiya keyd
  ```
  Debian/Ubuntu 因为 `keyd` 这个包名被 onak 占了，上游 keyd 在 Debian 系装成 **`keyd.rvaiya`**；
  Arch / Fedora / openSUSE 装的就是 **`keyd`**。
- **代码里的落点**：`capsplus/env.py:resolve_keyd_bin()` 依次试 `keyd.rvaiya` → `keyd` → 兜底
  `/usr/bin/keyd`；`capsplus status` 会打印它是**怎么定下来的**（`keyd 程序 : …（PATH 里的 keyd.rvaiya）`）。
  `KEYD_UNIT` 恒为 `keyd`（`systemctl` / `journalctl -u` 用它）。想指到别处：环境变量 `CAPSPLUS_KEYD_BIN`。

### 5. 复合层会多吐一对 `Alt+Ctrl` 空点

- **实测日期**：2026-10-15。
- **怎么验的**：同 1（虚拟键盘读回事件流）：每个 `capl+alt` 动作前后都会出现
  `leftalt↓ leftctrl↓ leftalt↑ leftctrl↑`，与层名无关 —— keyd 处理复合层的修饰键状态所致。
- **影响**：少数程序看到 `Alt` 轻点会闪一下菜单栏。不想要这层就删 `capsplus/mapping_data.py` 的 `CAPL_ALT`。

---

## 二、Wayland / GNOME 侧

### 6. 窗口置顶（keep-above）没有客户端 API

- **实测日期**：2026-10-15。
- **现象**：`Gtk.Window.set_keep_above(True)` 调了不报错，但 Qbar 仍会被别的窗口盖住。
- **怎么验的**：
  ```bash
  python3 -c "…gi.require_version('Gdk','3.0'); print(Gdk.Display.get_default())"
  # → <GdkWaylandDisplay>  本机 GDK 走的是 Wayland 后端
  ```
  再叠加协议事实：xdg-shell **没有**"把我永远置顶"的请求，GNOME/mutter 也不接受客户端提层；
  要置顶只能走 `zwlr_layer_shell`（需要专门库，本仓没依赖，实测本机装不上）或合成器插件。
- **结论**：**做不到，不是 bug**。近似替代是 `Super+↑`（GNOME 最大化）之类快捷键。

### 7. `set_skip_taskbar_hint()` 在 GNOME 无效 —— 应用会出现在 Dock / 窗口列表里

- **实测日期**：2026-10-15。
- **现象**：`_NET_WM_STATE_SKIP_TASKBAR` 是 X11/EWMH 的东西，Wayland 侧没有对应请求；
  GNOME 的活动概览/Dock 由合成器自己按 toplevel 列表决定，客户端拦不住。
- **怎么验的**：起 Qbar 后看 Dock —— 图标会出现（这就是当时把 `.desktop` 条目删到只剩一条的背景）。
- **结论**：**做不到，不是 bug**。本仓的做法是少装应用菜单条目（`install.sh` 里只装 Qbar + 设置两条，
  托盘走 autostart），而不是靠 hint。

### 8. 合成按键送不进**刚起来**的 Wayland 原生窗口

- **实测日期**：2026-10-15。
- **现象**：keyd（evdev/uinput 层）发的键，对新起的原生 Wayland 窗口在"焦点还没落定"的那一瞬间不可达；
  所以"用键位往 Qbar 里输字符"这种验证在本机做不成全自动。
- **怎么验的**：虚拟键盘注入 → Qbar 输入框没收到字符，但同一个窗口用真键盘敲就正常；
  焦点事件顺序在 `~/.local/state/capsplus/qbar.log` 里能看到（`map` 早于 `focus-in`）。
- **后果（很重要，给贡献者）**：**界面行为只能靠"单测 + 人眼"两条腿**。
  所以本仓把能下沉的逻辑都放进不 import `gi` 的纯层（`capsplus/settings_logic.py`、
  `qbar/logic.py`、`qbar/search_config.py`）用单测锁，真窗口测试
  （`tests/test_settings_window.py`）只断言"控件建出来了、表格只读、点第 N 行编辑条就是第 N 行"
  这类可自动化的部分；打字进窗口这种就别试了。

---

## 三、Python / GTK 侧

### 9. `tomllib` 需要 Python ≥ 3.11

- **实测日期**：2026-10-16。
- **怎么验的**：`tomllib` 是 3.11 才进标准库的（`python3 -c "import tomllib"` 在 3.10 上 ModuleNotFoundError）。
  本仓**只依赖标准库**，不额外装包，所以 `install.sh` 一开头就卡版本并给出对应发行版的装法。

### 10. TOML 里带 `+` 的段名必须加引号

- **实测日期**：2026-10-15 踩的，2026-10-16 用 `tomllib` 复核。
- **怎么验的**：
  ```python
  tomllib.loads('[capl+alt]\nt = "up"\n')   # TOMLDecodeError: Expected ']' … (line 1, column 6)
  tomllib.loads('["capl+alt"]\nt = "up"\n') # OK {'capl+alt': {...}}
  ```
  TOML 裸键只允许 `A-Za-z0-9_-`。
- **代码里的落点**：写盘一律过 `capsplus/mapping_overrides.py:toml_table()`，界面不许自己拼表头；
  读侧三处（写/读/报错）都对齐，`tests/test_mapping_overrides.py` 锁住。

### 11. `gi.require_version("Gdk","3.0")` 漏了会当场 ImportError

- **实测日期**：2026-10-15（自己引入的回归）。
- **现象**：PyGObject 默认加载命名空间的最新版；不显式 `require_version("Gdk","3.0")` 就先去 require
  `Gtk 3.0`，之后 `from gi.repository import Gdk` 报
  `Requiring namespace 'Gdk' version '3.0', but '4.0' is already loaded`。
  入口一跑就死，而**单测全绿**（测试进程早就 require 过 3.0，永远不重演入口那条路径）。
- **怎么验的**：`tests/test_settings_window.py::test_real_entry_points_import_cleanly` ——
  **子进程**跑真入口 `bin/capsplus-settings` / `bin/capsplus-qbar`，stderr 出现 Traceback 就判红。

### 12. 没装 `python3-gi` 的机器：界面相关测试整片跳过，不是报错

- **实测日期**：2026-10-16。
- **怎么验的**：
  ```bash
  python3 -m venv --without-pip /tmp/nogi-venv        # 这个 venv 里没有 gi
  /tmp/nogi-venv/bin/python -m unittest discover -s tests
  # → Ran 263 tests … OK (skipped=35)
  ```
  或者用仓里的 `tools/ci/nogi/sitecustomize.py` 屏蔽 `gi`：`PYTHONPATH=tools/ci/nogi python3 -m unittest discover -s tests`。
  GitHub Actions 的 `ubuntu-latest` 默认就没有 `python3-gi`，CI 走的就是这条路
  （`.github/workflows/tests.yml` 不装 gi、不碰 `/etc`、不用 sudo）。

### 13. 托盘要 GNOME 的 AppIndicator 扩展才有地方显示

- **实测日期**：2026-10-15。
- **现象**：托盘是 `AyatanaAppIndicator3` 提供的 DBus 菜单，GNOME 原生没有指示器宿主，
  必须装 `ubuntu-appindicators@ubuntu.com` 扩展；扩展处于 INACTIVE 时图标不出现，
  但托盘进程还在跑（`~/.local/state/capsplus/tray.lock`）。
- **怎么验的**：`gnome-extensions list --show-disabled` 看 `ubuntuappindicators@ubuntu.com` 的开关；
  被禁掉时"应用菜单里的 capsplus 设置"是唯一不靠命令行的入口（所以 `install.sh` 特意装了那条 `.desktop`）。

---

## 四、跑本仓的验收脚本要什么权限

| 脚本 | 要什么 | 为什么 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 无（不要 root、不要显示服务器） | 没 gi / 没显示就自动跳过界面用例 |
| `capsplus render` / `capsplus apply --dry-run` | 无 | 只渲染 + 归一化自检 |
| `capsplus apply` | `sudo -n`（免密或能输密码） | 要写 `/etc/keyd/` 并 reload keyd |
| `sudo python3 tools/e2e/check_mappings.py` | root + `/dev/uinput` | 造虚拟键盘 |

## 打开 URL 必须带激活上下文，否则浏览器另起窗口（2026-10-16 实测）

**症状**：Qbar 打开网页时默认浏览器（本机 Microsoft Edge）**不回到现有窗口**，而是新起一个窗口，
之后 Qbar 打开的页面全堆在那个新窗口里；只有焦点恰好在浏览器上时，才像"正常地在原窗口开标签页"。

**根因**：`Gio.AppInfo.launch_default_for_uri(uri, None)` 的第二个实参是 launch context。传 `None`
等于这次启动不带 activation 上下文（时间戳/令牌），桌面侧无法把它当成"用户主动点开"，浏览器就按
外部新起处理。修法见 `qbar/qbar_app.py::_launch_context()`：
`Gdk.Display.get_default().get_app_launch_context()` + `set_timestamp(Gtk.get_current_event_time())`，
四处打开动作（`url` / `file` / `dir` / `.desktop` 应用）全部带上。

**判据**：`tests/test_launch_context.py`（AST 扫描，headless 也跑）。把任何一处改回传 `None`
立刻判红，实测红字：`line 372: launch_default_for_uri() 的 launch context 是 None`。

**还没验的**：Chromium 系常见的"强制新标签页"开关在本机 Edge 上查不到 ——
`microsoft-edge-stable --help` 只列了 `--new-window`（那恰恰是反效果），`--new-tab` 不在帮助里，
**所以没写进默认行为**。真要拿它当硬保证，得在各自机器上实测后再用 `[general] browser = "…"` 显式配
（模板里 `{url}` 会被替换）。若上面这个激活上下文的修法在你机器上没能完全解决，就按这条继续查开关。

### 2026-10-16 追加：光有激活上下文还不够，别再同时报两个显示后端

用户实测：带上下文的修法之后，Qbar 打开网址**仍然另起一个新的 Edge 窗口**。继续查出来的事实：

- Edge 主进程命令行**没有任何参数**（`/proc/<pid>/cmdline` 只有一个词），靠环境自己选了 Wayland，
  并把 `--ozone-platform=wayland` 传给 22 个子进程 ⇒ 它是**原生 Wayland 实例**。
- 本机 Edge **二进制里没有 `new-tab` 这个开关**（`strings` 只找到 `new-window`；`--help` 也只列 `--new-window`），
  所以没法用命令行强行"新标签页"。
- 我们自己的降权包装（`bin/keyd-qbar-cmd`，keyd 的 `command()` 走它）原先**同时**注入
  `WAYLAND_DISPLAY=wayland-0` **和** `DISPLAY=:0` ⇒ 子进程面对两个后端，与原生 Wayland 的浏览器实例
  容易走到不同显示服务上（Chromium 的窗口合并/实例复用都以同一后端为前提）。
  ⇒ 改成 **只报一个后端，且按实际存在的东西决定**：调用者已导出 `WAYLAND_DISPLAY` 就沿用；
  没有但 `$XDG_RUNTIME_DIR/wayland-0` 这个 socket 真存在 → 报 `WAYLAND_DISPLAY=wayland-0`；
  否则沿用 `DISPLAY`，最后才兜底 `:0`。四种场景实测：
  `wayland-1` / (环境空但 socket 在) `wayland-0` / 纯 Xorg `:1` / 全都没有 `:0` ✓
  判据：`tests/test_portability.py::TestQbarLauncherReportsOneDisplayBackend`
  （不许再出现无条件双注入，且脚本必须 `bash -n` 过）。

**状态**：这一步是我这边的根因修复，但"是不是因此在原窗口开标签页"**只有用户的眼睛能确认**
（Wayland 下我拿不到窗口，也不该往真屏幕注入按键）。若仍旧另起窗口，剩下的可能性集中在
Edge 自身的窗口策略（`edge://settings` 里"打开链接的方式"）与合成器的 activation 行为，
那时候的兜底是显式配 `[general] browser = "…"` 用固定命令打开。

## 网页端"把问题塞进 URL 就自动问"只有站方给口子才成立（2026-10-16 实测）

这条决定了 Qbar 为什么要有第五种链接类型 `ask`（`问 AI（复制+打开）`）。

### DeepSeek 网页端：**支持**（所以 `ds` / `dsp` 走 `search`）

从 `chat.deepseek.com` 自己的 `main.js` 里读出来的（不是猜的参数名）：

```js
r = searchParams.get("q")?.trim() ?? e.get("prompt")
{ prompt: r, autoSend: "1" === searchParams.get("autosend") && r.length <= 800, ... }
```

三个条件都成立才会自动发送：**只有根路径 `/` 生效**、`autosend=1`、**问题长度 ≤ 800 字**。
所以内置 `ds` = `https://chat.deepseek.com/?q={q}&autosend=1`（一条回车就问完），
`dsp` = 同一地址不带 `autosend`（只预填，你自己按回车）。

### 千问（通义）网页端：**不支持**（所以 `qw` 走 `ask`）

扒 `https://www.qianwen.com/chat` 的 `@ali/qianwen-web/4.10.0` async chunk，三条证据：

1. **业务侧只读夸克集成参数**：能从代码里认出来的入参是
   `biz_id` / `channel_id` / `file` / `filename` / `qk_biz` / `qk_module` / `qk_params`
   —— 那是"从夸克客户端跳过来"的集成通道，不是给外部链接预填问题用的。
2. **唯一的 `?q=` 是搜索构造器**：代码里拼的是 `https://www.quark.cn/s?q=…`，
   也就是"拿 q 去夸克搜索"，跟聊天输入框没有半点关系。
3. **明确写死不支持**：代码里直接抛
   `"Workbench URL auto-send is not supported"`。

补一条口径：`chat.qwen.ai` 那边也拿不到 —— 它的 HTML 只暴露埋点脚本，应用包是运行时注入的，
抓不到可静态分析的 chunk（2026-10-16 试过）。ChatGPT / Gemini / ChatGLM 同理没有这个口子。

### 于是本仓的做法：不依赖站点配合的那条路

`ask` 类型 = **回车把问题原文写进剪贴板 + 打开目标网页**，用户在输入框 `Ctrl+V` 再回车。
多按一下粘贴，但**换掉了对站方的依赖**，站方哪天加了预填也不用改行为（把那条改成 `search` 即可）。

代码落点：`qbar/search_config.py`（`LINK_TYPES` / `open_plan` / `link_problems` / `preview_pair`）、
`qbar/logic.py`（`Entry.clip` + `ask_payload_text` / `split_ask_payload` 的历史建模）、
`qbar/qbar_app.py`（`_activate` 的 `ask` 支）。判据：`tests/test_qbar_ask_type.py`
（含"问题里带换行 / 逗号 / `|` / 中文长句"的往返、`_activate` 分支的源码级体检、
以及 mock 掉 GTK 之后的"剪贴板收到问题原文 + `_open_url` 收到期望 URL"）。

**隐私副作用**：`ask` 会把问题原文写进剪贴板，并且原样（不 URL 编码）落进
`qbar-history.json` 与 `qbar.log` —— 见 `docs/PRIVACY.md` 第一节表格与第四节。

## 「焦点不在浏览器时网页另起新窗口」是 Wayland 激活令牌问题（2026-10-16 定位到根因）

**现象**：Qbar 打开网址时，焦点在浏览器 → 在原窗口开标签页；焦点不在浏览器 → Edge 新画一扇窗。

**已经排除的**（都实测过，别再往这些方向找）：
- 不是"另起进程"：打开探针网址前后，`msedge` **主进程数始终是 1**（`/proc` 逐个比对，渲染进程 +若干）
  ⇒ 单实例转发是正常的，新窗口是同一个浏览器实例画的。
- 不是双报显示后端：`bin/keyd-qbar-cmd` 原先同时注入 `DISPLAY` 与 `WAYLAND_DISPLAY`，那确实是 bug（已修，
  现在按 socket 探测、只报一个），但**修完现象不变** ⇒ 不是主因。
- 不是 Edge 的偏好开关：扫它 `Default/Preferences` 里 139 个含 window/tab/startup/focus 的键，
  只有窗口几何与 `spawn_ntp_on_last_tab` 之类，**没有"外部链接开在哪扇窗"的策略键**；`/etc/opt/edge/` 不存在。

**根因**：GTK3 **没有把 Wayland 激活令牌交给被启动程序的通道** —— 实测
`libgdk-3.so.0` / `libgtk-3.so.0` 里字符串 `XDG_ACTIVATION_TOKEN` 出现 **0 次**（GTK4 才有这套）。
没有令牌，`mutter` 不允许 Edge 把已有窗口拉到前台；Edge 为保证用户看得见，只能新建窗口
（新建窗口在映射时就可以是激活的，不需要令牌）。焦点已在浏览器时不需要激活 ⇒ 正常开标签页。

**可行的修法（尚未实施，成本都标出来）**：
1. **装一个 GNOME Shell 扩展替我们激活窗口**：扩展跑在 mutter 里，有特权，
   `global.display.get_tab_list(Meta.TabList.NORMAL_ALL, null)` 找到浏览器窗口再 `activate(time)`，
   Qbar 随后打开 URL ——此时焦点已在浏览器，走的就是"开标签页"那条路。
   **API 可行性已在真机验证**：Ubuntu 自带且启用的 `tiling-assistant@ubuntu.com` 用的正是这两个调用
   （GNOME Shell 50.1）。代价：**新扩展必须重启 shell 才会被发现**（实测 `gnome-extensions enable`
   对刚放进 `~/.local/share/gnome-shell/extensions/` 的扩展报「不存在」；Wayland 不能 `Alt+F2 r`
   热重启 ⇒ 需要一次注销重登），外加开发 ~120 行 JS + Qbar 侧 ~15 行与 GNOME 升级的维护成本。
   **仓内实现（已写完）**：`extensions/raise-browser@capsplus.example/`（D-Bus **信号** `RaiseBrowser` / `Probe`；
   这台机的 GJS 没有 `export_object_path`、`register_object` 只剩 C 绑定 ⇒ 只能走信号）+ `tools/install-extension.sh`（复制进用户扩展目录并加进 `enabled-extensions`）+
   `tools/verify-raise-browser.py`（**A/B 实测**：用 `Probe` 报的窗口数做差，+1 = 另起新窗、0 = 原窗口
   开标签页 —— 不依赖人眼，绕开"Wayland 下我拿不到窗口"这个死结）。Qbar 侧由
   `[general] raise_browser`（默认 true）控制，调用超时 0.35s、失败静默跳过（实测扩展不在时 7ms 返回）。
   判据：`tests/test_raise_browser.py`。
2. **给浏览器开远程调试口**（`--remote-debugging-port=9222` + `PUT /json/new?url=`）：能确定性地"在原窗口开标签"，
   不需要重登；代价是 **本机任意进程都能读写你的浏览器标签**，安全上不如方案 1，不做默认。
3. 接受现状：把这条当已知限制；反正焦点本来就在浏览器时行为是对的。

### 本机 GJS 的 D-Bus 可用性（2026-10-16 逐条实测，别再照网上老写法抄）

| 用法 | 本机结果 |
| --- | --- |
| `Gio.DBus.session.export_object_path` / `unexport_object_path` | **undefined**（GJS 便捷函数已摘除） |
| `Gio.DBus.session.return_value` / `return_error_literal` | **undefined** |
| `Gio.DBus.session.register_object` | 存在，但是**原始 C 绑定**：`At least 5 arguments required`，vtable 是 C 结构，JS 造不出来 ⇒ 实际不可用 |
| `Gio.DBus.session.signal_subscribe` | 可用，**7 个实参且回调在最后一位**（`sender, interface, member, object_path, arg0, user_data, callback`） |
| `Gio.DBus.session.emit_signal` | 可用，**5 个实参**（`destination, object_path, interface, member, parameters`），`parameters` 传 `None` 可以 |
| `Gio.DBus.session.signal_remove` | **undefined** ⇒ `disable()` 里要按 `typeof` 兜着，不能因为它不存在就把停用流程抛断 |
| PyGI（Python 侧）`Gio.DBus.session` | **不存在**（GJS 专属便捷对象）⇒ 用 `Gio.bus_get_sync(Gio.BusType.SESSION, None)` |
| 跨实现通路 | Python-GI `emit_signal` → GJS `signal_subscribe` **实测收到** ✓ |

扩展加载失败是**静默**的：现象与没装一样。所以两处都留了话——
`export default class` 缺失时 shell 自己报 `TypeError: extensionModule.default is not a constructor`，
其余失败由扩展自己的 `console.error` 写进 journal；排查一律
`journalctl --user --since '-10 min' | grep raise-browser`。
另外：改过扩展代码后 `disable/enable` **不会重新 import**（状态卡在 ERROR），必须重登。

### 发信号必须**跑主循环**，`sleep` 等于没发（2026-10-16 实测，代价最大的一条）

`Gio.DBusConnection.emit_signal()` 只是把消息**入队**，真正写出去靠 GLib 主循环派发。
同一台机、同一段代码，三种写法的实测结果：

| 写法 | 结果 |
| --- | --- |
| `emit_signal(...)` 之后 `GLib.usleep(80_000)` / `time.sleep(0.8)` | 扩展**收不到**，journal 里一条 `probe` 都没有 ⇒ 等于没发 |
| `MainContext.iteration(False)` 空转 120ms | 同样收不到 |
| `GLib.MainLoop()` + `timeout_add(ms, quit)` 跑起来 | **每次都收到** ✓ A/B 由此测出 +0（原窗口开标签页） |

这条为什么值钱：Qbar 里原本写的就是第一种（`GLib.usleep`），所以扩展明明已经 ACTIVE、
`capsplus status` 也说正常，明江手动测出来仍然是"新窗口"——**发送方自己把包咽回去了**。
判据层面还有个教训：那条分支没人跑过，所以改名/改常量时工作树破了也全绿
（现已加"未导入的大写常量"门 + 泵门，见 `tests/test_raise_browser.py`）。

### 扩展在 shell 进程里 `emit_signal` 上不了总线（因此判活读 journal）

实测：外部（Python-GI 或 gjs 进程）发 `Probe` → 扩展收得到并写 journal ✓
但扩展内部回发 `ProbeReport` → `dbus-monitor` 抓 **0 条**，且 journal 无任何 `JS ERROR`。
结论：**别把"活没活"的通道设计成扩展回包**，一律读 journal
（`[raise-browser] probe default=… windows=N on-current=M`），Qbar 的判活就是这么做的
（后台线程：发 Probe → 跑主循环 400ms → `journalctl --user --since …`）。

---

## 语音输入的上屏：Wayland 下"打中文"三路全断（2026-10-17 本机逐条实测）

本节解释的是 `capsplus/voice_inject.py` 为什么长成那样（只有一个默认后端 `clipboard`，
且它**先探测再动手**）。本机环境：Ubuntu 26.04.1 / GNOME Shell 50.1 / 会话 `Type=wayland`。

> **2026-10-19 逐条复核**（事故调查顺带做的，别再把老结论当现状）：
> **1~8 仍是限制**——1、2 当天重测（数一样），3~8 读现在的代码逐条确认没变；
> **第 9 条**是"未实测清单"本身，其中 ②b 已被**部分实测**（见 CHANGELOG 2026-10-19 条目）；
> **第 10 条改了两处**（"`focus_out` 收不收得到"、"贴上又删是哪一种"都有真机证据了）；
> **新增第 11 条**（剪贴板这条路自己的平台事实）。

### 1. mutter 没实现 `zwp_virtual_keyboard` 与 `zwp_input_method` ⇒ `wtype` 在 GNOME 必死

- **实测日期**：2026-10-17（**2026-10-19 复核，数一样**：`libmutter-18.so.0` 50.1-0ubuntu2.4）。
- **怎么验的**：对 `libmutter-18.so.0` 数协议名字串 —— `zwp_virtual_keyboard=0`、`zwp_input_method=0`，
  只有 `zwp_text_input_manager_v3=1`（那是给输入法客户端用的，不是注入通道）。
- **后果**：`wtype` 依赖前两个协议，装上也用不了；别拿它当退路。

### 2. `libei`（本机 `libeis1` 1.5.0-3）导出符号里 **keysym 数 = 0**

- **怎么验的**：`nm -D` 只读段数符号 —— keysym 类 0 条，只有 evdev 键码那条路。
  **2026-10-19 复核**：`nm -D /usr/lib/x86_64-linux-gnu/libei.so.1`（1.5.0）里含 `keysym` 的导出
  符号仍是 **0** 条。
- **后果**：想靠 libei/portal"输入 Unicode 字符"在本机不成立（打不出汉字）。

### 3. uinput 只有键码 ⇒ `ydotool type` / `dotool type` **打不出中文**

- 逐字符映射到键盘布局，汉字没有键位；上游也认：`ydotool` issue **#249 / #22 至今 open**。
- **所以**：注入只能发**键鼠事件**（Ctrl+V / 中键），文本本身走 `wl-copy`。
  本仓**不许**再实现"逐字打字"的任何变体。

### 4. 本机这版 ydotool 吃的确实是 evdev 键码（不是键名）

- **怎么验的**：发键码 `37`，`keyd monitor` 报回来的键名是 `k`（`KEY_K=37`）⇒ 映射对得上。
- 于是 `29`=左 Ctrl、`47`=`V`，注入序列就是 `ydotool key 29:1 47:1 47:0 29:0`。
- **但数字不许写死**：键码一律从 `/usr/include/linux/input-event-codes.h` 现读
  （`voice_inject.read_keycodes()`），头文件里查不到的键直接判"未验证"，**不许猜**。

### 5. 客户端只认 `YDOTOOL_SOCKET`；`YDOTOOL_SOCKET_PATH` 被**静默忽略**

- **症状**（踩过，浪费过一小时）：设了 `YDOTOOL_SOCKET_PATH=/run/ydotool.sock` 仍报
  `failed to connect socket '/run/user/<uid>/.ydotool_socket'` —— 环境变量根本没进它眼里。
- **怎么验的**：`ydotool` 不带参数的用法文本最后一行写着真名（`YDOTOOL_SOCKET`）。
- **口径**：要改 socket 位置就设 `YDOTOOL_SOCKET`；本仓不依赖它（守护就把 socket 建在
  `$XDG_RUNTIME_DIR` 下的默认名，客户端只要带着 `XDG_RUNTIME_DIR` 就能找到）。

### 6. `ydotool key --help` **不能**用来探测语法

- **实测**：把 `YDOTOOL_SOCKET` 指到一个不存在的 socket 后，`key abc` / `key ctrl+a` /
  `key 30:1 30:0` 全都报**同一句** socket 错（rc=2）—— 它**先连 daemon 再解析参数**，
  参数根本没被读过。
- **于是探测换成两条能落地的**（见 `voice_inject.py`）：① 读**已安装那个二进制自己**的只读段
  里的用法文本（`Syntax: <keycode>:<pressed>` / `0x40 - Mouse down`）来判断它认键码还是键名；
  ② daemon 活着与否用**空参数** `ydotool key` 判 —— 0 个键码 = 0 个事件，不会往用户屏幕上打东西。

### 7. 「主选区 + 中键粘贴」被**否决**（代码里留着，但别当默认用）

- 中键是按**指针位置**粘贴，不是按光标位置：指针停在哪儿就贴到哪儿，在浏览器还会**开新标签页**。
- 语音输入的语义是"贴到我刚才打字的地方"，指针位置与它无关 ⇒ **默认后端只能是 `clipboard`**。
  `primary` 只在"光标本来就在指针底下"这种人手里有意义。

### 8. 注入守护的形状：为什么是 **system 级 root 单元**，不能用包默认的 user 单元

- 现状（本机运维事实，2026-10-17）：单元 `/etc/systemd/system/ydotoold.service`（root），
  socket 建在 `$XDG_RUNTIME_DIR` 下的 `.ydotool_socket`，属 **`root:input 0660`**；
  `/dev/uinput` 是 `root:input 660`（Debian `80-uinput.rules` 生效）；本用户已加入 `input` 组。
- **关键一条**：**组变更对已经在跑的会话无效** —— 走包默认的 user 单元就得等注销重登，
  而"不许打断用户干活"是本仓红线（KNOWN-LIMITS 上面几条就是为此立的）。
  root 级守护不需要重登；而 **`runuser` / `sudo -u` 会重读组** ⇒ 实际按键路径当场可用。
- **实测**：`voice_inject.run_probe()` 经 runuser 跑 → `verified=True`；从**改动前的旧会话进程**里跑
  → 报"未验证"。**别拿后者下结论**（这就是 `Probe.ydotool_daemon is None/False` 要如实报出来的原因）。
- 包自带的 user 单元 `ydotool.service` 已 `disable`：两个守护会同 socket 撞车，报
  `Another ydotoold is running with the same socket`。

### 9. 未实测（本节撑不起来的部分，别顺着往下信）

① 弹窗销毁后焦点是否真还给原文档（**已部分实测，见下面第 10 条**：X11 侧量到了信号到达的时刻，
但"焦点给了谁"这条客户端问不到）；② `Ctrl+V` 在 Edge / Chrome / GNOME 终端 / LibreOffice / VS Code
里的实际落点与适用面；③ **ibus / libpinyin 在场时 Enter 会不会被输入法吃掉**（头号风险，
`voice_osd.key_action` 已备但没在真输入法下跑过）；④ 打字中途按 `Caps+M`，键声会不会被能量门误判成
语音；⑤ 跨线程碰 GTK 会**段错误 rc=139**（试过并在代码里禁止，真 Wayland 下的长期稳定性未测）。

### 10. "焦点交给了谁"客户端问不到 —— 所以上屏的触发条件只能是"事实 + 上下限"（2026-10-17 实测）

- **怎么验的**：Xvfb + `GDK_BACKEND=x11` + `CAPSPLUS_GUI=1` 下把 `voice_osd` 记的生命周期时间线打出来
  （毫秒、相对 Enter）：`map@-5, focus_in@-2.5, focus_out@-2.2, focus_in@-2.1, finish@0, unmap@0.03,
  destroy@0.06`；同一份窗口销毁后 `get_visible()` / `get_realized()` / `get_mapped()` 全是 False。
- **三条结论**：
  1. **X11 下 `unmap` 与 `destroy` 同步**（Enter 之后 0.03ms 就到）⇒ "等到 unmap 事件"在这个平台上
     **等于没等**。所以注入另外要有**下限**（`voice_worker.INJECT_FLOOR_MS = 120`）：改成"只等事件"
     反而会把上屏提得比现状更早、更危险。
  2. **`focus_out` 不一定来**：Xvfb 没有窗口管理器，这一轮就没收到（只有 unmap）。mutter 下**释放
     事实收得到**（2026-10-19 09:15 真机那行 `inject_diag gate=floor … visible=0 mapped=0 unmap=1
     destroy=1`——`gate=floor` 只有 `fact["released"]` 为真才会被选中，只是它比下限 120ms 更早到），
     但**是哪一种**（`unmap` 还是 `focus_out`）仍未区分。这正是 `gate=cap` 那条分支继续存在的理由。
  3. **客户端拿不到"焦点给了谁"**：xdg-shell 没这个查询，GTK3 也没有；`set_accept_focus(False)` 只是
     X11 hint 的等价物。于是"焦点已回到原文档"**不可能被证实、只可能被证伪**（我们还 mapped /
     还可见 ⇒ 一定没交出去）。
- **因此定成**：注入的必要条件是时间线里出现 `unmap` 或 `focus_out`（`voice_osd.RELEASE_KINDS`），
  下限 120ms，**上限兜底 400ms**（`INJECT_WAIT_CAP_MS`）；等满还拿不到任何信号就带着诊断注入，
  并把这一次写成**未确认** —— 状态行、`voice.log` 的 `inject_diag gate=cap unconfirmed=1`、
  `capsplus-voice status` 的「上屏结论」三处同步，**不许写成"上屏结束"**。判据：
  `tests/test_voice_focus_gate.py`（四条，各带能红的反例具型）+
  `tests/test_voice_mainloop.py::TestReviewRunsOnMainThread`（真窗口那条钉的是**顺序**而不是时长）。
- **"贴上又删"是三种里的哪一种 —— 2026-10-19 定了：三种都不是。** 真机 09:15 那一轮给出的是
  `visible=0 mapped=0`（⇒ **不是**①"贴进我们自己正在消失的窗口"）、`gate=floor`（⇒ 释放事实到了），
  以及 `inject_done … backend=clipboard ok=0 code=step_failed steps=0` 且两条耗时读数相减恰好 =
  `STEP_TIMEOUT_S`（5.0s）⇒ **第一步"写剪贴板"就超时被判失败，`Ctrl+V` 从未执行**（⇒ 也谈不上
  ②"目标应用自己的行为"、③"粘出来的是旧内容"——粘贴这个动作根本没发生）。所以这条功能**从落地起
  一次都没真贴上过字**：那次"贴上去了又消失"里的前半句不成立 —— 至于用户当时看到的闪动究竟是
  什么，本轮**没有再取证**，别顺着往下写。
  区分量仍在 `inject_diag` / `restore_diag` / `inject_done`（现在多了 `copy_ms=`）这几行里。

### 11. 剪贴板这条路自己的两条平台事实（2026-10-19 实测，不是推测）

这一条解释 `voice_inject.run_step()` 为什么把命令分成"守护步 / 普通步"两类来等，以及为什么写完
剪贴板必须**回读自证**（经过见 CHANGELOG 2026-10-19 条目）。

- **`wl-copy` 会 fork 一个常驻进程替它守剪贴板数据，而那个进程继承了 stderr**。读本机
  `wl-clipboard` 2.2.1 的 `src/wl-copy.c`（`did_set_selection_callback`）：fork **之前**只
  `dup2(/dev/null, STDOUT_FILENO)`（源码注释原话：*so the stdout file descriptor isn't kept alive*）
  与 `dup2(/dev/null, STDIN_FILENO)`，**stderr 原样留着**。而
  `subprocess.run(..., capture_output=True)` 把 stdout **和 stderr** 都接过来 ⇒ **管道 EOF 永不到来**，
  一条毫秒级就该返回的命令被判成超时（本次事故：5 秒）。⇒ **会 fork 常驻进程的命令，不许用"等管道
  EOF"判成败。** 同源事实：`--clear` 在同一个回调里提前 `exit(0)`、**根本不 fork**。
- **父进程 rc=0 ≠ 选区所有权到手**：`wl-copy` 退 0 只说明"数据交给了它自己的守护进程"。要判"真的
  写上了"必须**回读**（`wl-paste` 逐字节比对，`verify_clipboard_write`）；回读不是我们的文本 ⇒ 报
  `owner_rejected`（不是笼统的 `step_failed`）且**不发粘贴**。
- **"无键盘焦点的进程设不了选区" —— 这个担心在本机是错的（实测排除）**：本机**没有** data-control
  协议（45 个全局里 `data_control` 命中 0，只有核心 `wl_data_device_manager` v3 +
  `zwp_primary_selection_device_manager_v1`），所以此前只是"担心"；2026-10-19 11:07 从一个**无键盘
  焦点、无 activation token** 的 shell 里 `wl-copy --type image/png < 5.62MiB.png`：父进程毫秒级返回、
  **选区当场设成**（deskflow 日志 `sent clipboard size=5880052`）。⇒ `owner_rejected` 仍要报（那是
  运行期事实），但它**不是"必然发生"的预期**，别拿它当已知结论去解释别的现象。

---

## 设置页能编辑的键位 = 上游声明的**全表**，没绑定的键显示成"空位"（2026-10-19 核实 + 单测）

- **原来为什么"有些快捷键没有"**：设置页那两列（"层/键/动作/说明/来源"）不是手写的清单，
  它由 `capsplus/render.py:binding_rows()` 从 `capsplus/mapping_data.py:SECTIONS` **派生**出来，
  `capsplus/mapping_overrides.py:layer_rows()` 也是同一份派生。所以设置页缺的键 = 表里就没声明的键，
  两者差集**恒为空**（2026-10-19 实测：`binding_rows` 的键序列与 `SECTIONS` 逐层相等）。
- **缺口在表，不在界面**。量出来的数是（2026-10-19，读 `~/.dsh/refs/capslock-v3.3.0/lib_keysSet.ahk`
  的 `keySchemeInit_capslox()`，逐个 `keyset.caps_*` / `keyset.caps_lalt_*` 数）：

  | 层 | 上游声明 | 本仓默认绑定 | 差（= 设置页原来没有的） |
  | --- | --- | --- | --- |
  | `capl` | 64 | 31 | **33**：`m`、`z`、`grave`、`1`–`0`、`minus`、`equal`、`tab`、`leftbrace`、`rightbrace`、`backslash`、`apostrophe`、`rightalt`、`f1`–`f12` |
  | `capl+alt` | 52 | 25 | **27**：`c`、`m`、`q`、`v`、`x`、`z`、`grave`、`1`–`0`、`minus`、`equal`、`tab`、`leftbrace`、`rightbrace`、`backslash`、`apostrophe`、`enter`、`space`、`rightalt` |

- **现在怎么做的**：`mapping_data.EDITABLE_KEYS` 是"设置页要列出的键位全表"（顺序照上游声明：
  字母 → `grave` → 数字 `1..9,0` → 符号 → `f1`–`f12`；F 键只有基础层有，上游 `caps_lalt_*` 里没有）。
  不在 `SECTIONS` 里的那些就是**空位**：动作列留空、说明列写"空位"。
- **空位不渲染，也不吞键**：`render()` 只读 `SECTIONS`，所以空位不进 `conf`。
  按 keyd 的层语义，层里没绑定的键是**放行**（man「COMPOSITE LAYERS」原句：
  *"pressing control+alt+f1 preserves those modifiers, emitting exactly what was pressed,
  as there is no explicit binding for f1 on the composite layer"*）—— 也就是说
  按住 Caps 按数字键，本来就照原样出数字，默认行为一个字都没变。
  用户在设置页给空位填了动作，才由覆盖层把这条**追加到该段段尾**（`merge_overrides` 的既有规则，
  与渲染出来的位置一致）。
- **为什么不直接把数字键/F 键写成默认绑定**：仓里 `tests/test_render.py:TestReferenceEquivalence`
  断言"渲染出来的绑定集 == 参考配置的绑定集"（两向差集都必须为空），**任何新增默认绑定都会让它红**；
  参考配置在仓外、且它是"上游移植"的对账物，不能为了新功能去改它。所以"新增组合"只能走用户覆盖层。
  要不要放开这条（改成"参考配置 ⊆ 渲染 + 参考那几条一字不改"）是**另一个决定**，得先问人。
- **判据**：`tests/test_editable_keys.py`（21 条）。①设置页列出的键集 == `EDITABLE_KEYS`；
  ②数字/F 键走"校验 → 覆盖层 → 合并 → 渲染"真的生成绑定、且只多出这一条；
  ③空位不进默认渲染、既有绑定的**顺序与字面值**与参考配置逐条一致（比集合相等更严）；
  ④表里每个键名都在 keyd 自己的 `list-keys` 里（有 keyd 才跑）。
  反例具型：把 `1`/`0`/`f1`/`f12`/`m`/`z` 任一个从表里删掉，①必须红。
- **真机验法**（我这边不许动 `/etc/keyd`，只能他按）：设置页第③页 → 点 `capl / 1` 那行 →
  编辑条填 `C-c` → 「改这一格」→「应用」→ 按住 Caps 按 `1` 应触发复制；`f1` 同理。
  不想要了就选中那行点「恢复默认」，再「应用」。
- **还没跟上的一处**：`README.md` / `README.template.md` 里那张"组合键 → 功能"表仍只列**真实绑定**
  （`KEY_TABLE_NOTES` 生成的），没提空位与数字/F 键 —— 这次没改（文件不在本轮写范围内）。

## 「先 Caps+Z 再 N」这种两段组合：keyd 有机制，但**本仓现在配不出来**（2026-10-19 读 man，未真机实测）

- **keyd 没有"按键序列"**（没有 record/playback、没有 leader key）：这点先排除，别指望 `macro` 能做到
  （`macro` 是发出一串键，不是等一串键）。
- **有对应的机制**：`oneshot(<layer>)` —— man「ACTIONS」原句 *"If tapped, activate the supplied layer
  for the duration of the next keypress."* 轻按一下激活某层、**下一次按键**在该层里解释，
  形状上就是"先 Z 再 N"。另一条更确定的写法是 `z = layer(新层)`（按住 Z 的同时按 N）或
  `[capl+z]` 复合层；`chord_timeout` 那套 `z+n = 动作` 是"两个键几乎同时按下"，也不是先后关系。
- **但今天配不出来，两道闸**：
  1. 设置页只允许改 `capl` / `capl+alt`（`mapping_overrides.ALLOWED_SECTIONS`），而
     `oneshot(x)` / `layer(x)` 点名的层**必须在渲染出的配置里真实存在**（man「Bindings」一节：
     `<layer>` 是 an existing layer 的名字）；
  2. 于是要么往 `SECTIONS` 加第三层（比如 `[capl_z]` 及其键位矩阵），要么接受复合层
     `[capl+z]` —— **两者都是"新增默认绑定"，会撞上 `TestReferenceEquivalence`**（见上一节），
     要先把那条判据的口径定下来。
- **真机还没验的那一点**（别顺着往下信）：`oneshot` 层是在 `[capl]` 里被点着的，
  而"按住 Caps"这个层紧接着就松开了 —— **oneshot 的待命状态跨不跨得过成员层的释放**，man 没写、
  我也没测（本机不许动 `/etc/keyd`）。真要有这条路，第一步就是拿一条最小配置问这句话。
- **要验的话怎么验**（他有 sudo，我这边只做只读核实）：临时往 `/etc/keyd/` 里放一条最小实验配置
  → `sudo keyd reload` → 试"轻按 Caps+Z 松手、再按 N"，看 N 是被吃掉还是照原样出；
  验完 `sudo rm` 那个文件再 reload 就回到现状（`capsplus apply` 只管自己那份 `capslock-plus.conf`）。
  救命组合仍然是 `backspace+escape+enter`。
