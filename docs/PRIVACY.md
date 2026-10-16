# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）。
# 这是文档：本项目的数据落地位置、保留策略，以及公开分享前该清掉什么。

# 隐私：本项目在你机器上留下什么

先给结论：**本项目不联网、不上传任何东西**（2026-10-16 实测：全仓没有 `urllib.request` /
`http.client` / `socket.AF_INET` / `requests` 之类调用；唯一的"网络动作"是把拼好的搜索 URL
交给浏览器打开，那之后就是浏览器的事了）。

但它会在你家目录里**写文件**，其中有两处真的会记下你搜过什么。下面逐条列出位置、内容、
保留策略与清理办法。

## 一、写在哪、写什么

运行时目录：`~/.local/state/capsplus/`（XDG 状态目录，`capsplus` 一个子目录）
配置目录：`~/.config/capsplus/`
系统目录：`/etc/keyd/capslock-plus.conf`（只有你自己跑 `capsplus apply` 才会写）

| 文件 / 目录 | 谁写的 | 里面是什么 | 含不含"你输入的内容" | 保留策略 |
| --- | --- | --- | --- | --- |
| `qbar.log` | Qbar（`qbar/qbar_app.py`） | 诊断事件：`map` / `focus-in` / `focus-out` / `show(...)` / `hide` / `activate` | **平时不含输入本身，但有一条例外**：`activate kind=… title=…` 这行记的是**被激活条目的标题**，搜索结果标题长这样：「必应搜索：<你的关键词>」——关键词就跟着进去了（`qbar/search_config.py:entry_title`，实测 2026-10-16） | 只留**最后 200 行**，每次写整体重写 |
| `qbar-history.json` | Qbar | 使用历史，用来把常用项往前排。**键 = `kind<TAB>payload`**，`payload` 是最终 URL / 路径 / 命令 | **含**。键的格式是 `url<TAB>最终 URL`，例如 `url<TAB>https://www.bing.com/search?q=keyd+%E9%85%8D%E7%BD%AE`（= 搜过「keyd 配置」）—— URL 编码过的关键词明明白白写在里面，另外还会记你打开过的本地路径。**`ask` 类型（2026-10-16 加）记的是 `ask<TAB>["最终 URL", "问题原文"]`**：问题**不编码**、**原样**落盘（一句五千字的中文长句就整句写进去）。格式是 JSON 数组，不是分隔符拼接（见 `qbar/logic.py:ask_payload_text`） | 最多 **200 条**，按最近时间裁（`qbar/logic.py:History(max_items=200)`） |
| `keyd-conf-backup/` | `capsplus apply` | 每次替换 `/etc/keyd/capslock-plus.conf` 之前的原件副本 | 间接含：里面是你的键位（个人习惯，不含文字内容） | 最近 **10 份**；更旧的 `mv` 进焚化炉 `/tmp/attic-<日期>/`（不用 `rm`，所以**重启即清空**） |
| `config-backup/<时间戳>/` | 设置窗口写回 `qbar.toml` 前 | 快速链接配置的原件快照 | **含**：`qbar.toml` 里是你自己加的网址、本地路径、命令 | 每次保留 **20 份**（`capsplus/settings_logic.py:snapshot_config(keep=20)`） |
| `qbar.toml`（`~/.config/capsplus/`） | 你 / 首次打开 Qbar 自动生成 / 设置界面 | 快速链接与搜索引擎、默认引擎、`browser`、`hide_on_focus_out` | **含**：你的个人网址、`app` 类型的命令行、`file` 类型的本地路径 | 无自动清理（是你的配置，删了就没） |
| `mappings.toml`（`~/.config/capsplus/`） | 设置界面「按键映射」 | 你改过的键位覆盖（只存改动那几条） | 不含文字，但含键位习惯 | 无自动清理 |
| `qbar.sock` / `settings.sock` / `tray.lock` | 单实例与 flock | Unix socket / 锁文件，**不存内容** | 不含 | 进程退出即失效（socket 会被清） |
| `/etc/keyd/capslock-plus.conf` | `capsplus apply`（sudo） | 渲染出的键位配置（含你机器上的 bin 目录绝对路径） | 含**你的家目录路径**（`command(<你的 ~/.local/bin>/keyd-qbar-cmd)`） | 由你管 |

另外两个"环境变量决定行为"的点：

- `CAPSPLUS_QBAR_DEBUG=1` 起 Qbar → 日志里**多记一行行按键**（`key <键名> widget=…`）。
  默认关闭；开着的时候那份日志就别外传了。
- `CAPSPLUS_BINDIR` / `CAPSPLUS_KEYD_BIN` / `CAPSPLUS_USER` 只影响路径与降权目标，不落盘。

## 二、怎么"关"

**目前没有"关掉日志/历史"的开关**（这是实话，不是待办委婉说法）。可选做法按代价排序：

1. **定期删**（最省事）：
   ```bash
   mkdir -p /tmp/attic-$(date +%Y%m%d)
   mv ~/.local/state/capsplus/qbar.log ~/.local/state/capsplus/qbar-history.json \
      /tmp/attic-$(date +%Y%m%d)/
   ```
   下次用 Qbar 会重新长出来（历史排序会退化成"全按内置顺序"，功能不受影响）。
2. **让日志只记动作、不记标题**：`qbar/qbar_app.py` 里 `activate` 那行把
   `title=…` 去掉即可（一行的事）；历史的关键词要去掉得改 `history_key()` 的构造，代价大得多
   —— 历史排序就靠这个键。
3. **想要真开关**：得在 `qbar.toml` 的 `[general]` 加两项（比如 `log` / `history`），
   写回模板与设置界面也要跟着加。**这轮没做**，列在未解决里。

## 三、要把截图 / 配置 / 日志贴到网上之前

按这个清单来（都是"贴出去就收不回"的东西）：

```bash
# 1) 诊断日志与搜索历史（含关键词）
mv ~/.local/state/capsplus/qbar.log ~/.local/state/capsplus/qbar-history.json /tmp/attic-$(date +%Y%m%d)/
# 2) 配置原件快照（里面是你历次的个人网址）
mv ~/.local/state/capsplus/config-backup /tmp/attic-$(date +%Y%m%d)/
# 3) 键位备份（含你的家目录路径）
mv ~/.local/state/capsplus/keyd-conf-backup /tmp/attic-$(date +%Y%m%d)/
# 4) 要贴 qbar.toml / mappings.toml 之前先看一眼：
less ~/.config/capsplus/qbar.toml      # 自己加的 trigger / URL / 本地路径
grep -c . ~/.config/capsplus/mappings.toml 2>/dev/null
```

还有两点容易忘：

- **Qbar 截屏**会把搜索框里的字与结果标题（含关键词）一起拍进去；`capsplus status` 的输出里
  有**你的家目录绝对路径**与 `/etc/keyd` 路径 —— 贴终端截图前先想想。
- **提 issue 附带的日志**：`qbar.log` 里的 `activate` 行已经够定位问题，贴之前把带关键词的
  那几行删掉（或者只贴焦点相关那几行：`map` / `focus-in` / `focus-out` / `hide` / `show`）。
- **剪贴板本身**：最后一次 `qw 你的问题` 回车之后，问题原文就留在系统剪贴板里（截图、
  共享屏幕、以及任何"粘贴一下看看"都会把它带出去）。要清就随便拷点无关文本盖掉。

## 四、剪贴板：只写、不读（2026-10-16 说清楚）

Qbar 会**往系统剪贴板里放东西**（原先这句写在"本项目不做的事"里，那是不准的 —— 早就有三处在写），
但**从不读**剪贴板内容，也不保存历史剪贴板。写入的三处：

| 触发 | 写进剪贴板的内容 | 代码位置 |
| --- | --- | --- |
| `=1+2*3` 回车 | 算出来的结果（`4`） | `qbar/qbar_app.py::_activate` 的 `calc` 支 |
| `-> date` / `-> time` | 日期 / 时间字符串 | `qbar/qbar_app.py::_run_builtin` |
| **`qw 我的问题` 回车**（`ask` 类型） | **你输入的问题原文**，一字不改（不是 URL、不是 URL 编码串） | `qbar/qbar_app.py::_activate` 的 `ask` 支 |

`ask` 这一条是本次新增，副作用要说透：

- 回车之后剪贴板里原来那块内容**被覆盖**（GTK/X11/Wayland 的剪贴板就是一份，没有"独立第二块"，
  上游 Capslock+ 的第二剪贴板在 Linux 上得靠 copyq）。所以别在拷了密码之类敏感内容之后
  去敲 `qw 问题`。
- 同一个问题原文还会**落两处盘**：`qbar-history.json`（见第一节那张表，最多 200 条）与
  `qbar.log` 的 `activate kind=ask title='千问（复制+打开）：<你的问题>'`。
  日志只留最后 200 行，但**问题原文就在里面**。
- 光敲 `qw` 没跟问题：**不动剪贴板**（只打开网页）—— 免得把人家原有的内容洗成空串。
- 想彻底不留痕迹：按第二节的办法把 `qbar.log` 与 `qbar-history.json` 挪进焚化炉。
  关掉 `ask` 的复制行为则要在源码层改，目前没有开关（这是实话）。

## 五、本项目**不**做的事

- 不联网、不上报、不埋点、不自更新；没有任何远端请求。
  （`ask` 只把网址交给**你自己的默认浏览器**，本仓代码自己不发一个字节到任何站点；
  问题原文是你在网页输入框里 `Ctrl+V` 之后由你按回车发出去的，发不发、发给谁都在你手里。）
- 不记录按键流（除显式 `CAPSPLUS_QBAR_DEBUG=1`）。
- **不读剪贴板内容**、不维护剪贴板历史（写的那三处见第五节）。
- 不动 `~/.bash_history`、其他应用的数据。
- 不改系统配置：`/etc/keyd/` 只在你自己跑 `capsplus apply` 时写，且写之前一定先备份。
