# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）。
# 这是文档：本项目的数据落地位置、保留策略，以及公开分享前该清掉什么。

# 隐私：本项目在你机器上留下什么

先给结论：**没有任何数据离开本机**（2026-10-17 修正口径）。

> 原话是"全仓没有 `urllib.request` / `http.client` / `socket.AF_INET` / `requests` 之类调用
> （2026-10-16 实测）"—— **那句已经不成立了**，别照它行事：加了语音输入之后，
> `capsplus/voice_asr.py` 确实 `import urllib.request`。

现在的事实是：全仓**唯一的网络动作**是语音输入向 `127.0.0.1:18090` 发一次识别请求
（`POST /asr-upload?lang=zh`，只打 loopback，超时/寻址都在 `voice_asr.py` 里， host 默认 `127.0.0.1`）。
自证两条：`ss -ltnp | grep 18090` 看监听者 —— 是本机的 `voice-svc`（SenseVoice 本地模型，
`~/voice-svc/daemon.py`，不联网、模型常驻本机），**没有任何远端**。
除此之外唯一的"网络"仍是把拼好的搜索 URL 交给浏览器打开，那之后就是浏览器的事了。
不上传、不埋点、不自更新。

但它会在你家目录里**写文件**，其中两处真的会记下你搜过什么；语音输入上屏前后还会**读一次你的
剪贴板**（只在内存里备份、粘贴完条件还原，**不落盘**，见第四节）。下面逐条列出位置、内容、
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
| `voice.sock` | 语音常驻 worker（`capsplus/voice_worker.py`） | 单实例与命令通道（换行分隔 JSON）。**权限 0600** —— 与 Qbar 那个只收一字节的 socket 不同，这条线上跑的是**你说的话**，同机别人不能连（`voice_proto.SOCK_MODE`，判据在 `tests/test_voice_proto.py`） | 传输瞬间含，**落盘不含**（socket 本身不存东西） | 进程退出即失效 |
| `voice.log` | 语音 worker（`voice_proto.log_line`） | **只记状态与数量**，形如 `recording 开始采集` / `decoding 送识别 96000 字节` / `review 待确认 12 字` / `idle 识别失败 <code>`。**不记录识别出的文本、不记录音频**（写日志的调用点只传 `state`+`note`，`note` 里最多是字节数/字数） | 不含文字内容 | 只留**最后 200 行**，每次写整体重写 |
| `/etc/keyd/capslock-plus.conf` | `capsplus apply`（sudo） | 渲染出的键位配置（含你机器上的 bin 目录绝对路径） | 含**你的家目录路径**（`command(<你的 ~/.local/bin>/keyd-qbar-cmd)`，语音输入还会多一条同形状的 `capsplus-voice` 行） | 由你管 |

另外几个"环境变量决定行为"的点：

- `CAPSPLUS_QBAR_DEBUG=1` 起 Qbar → 日志里**多记一行行按键**（`key <键名> widget=…`）。
  默认关闭；开着的时候那份日志就别外传了。
- `CAPSPLUS_BINDIR` / `CAPSPLUS_KEYD_BIN` / `CAPSPLUS_USER` 只影响路径与降权目标，不落盘。
- **`CAPSPLUS_VOICE_BACKEND=manual`** —— 语音输入唯一"完全不碰剪贴板"的开关：只把识别出的文本留在
  弹窗里由你自己复制，注入那一步整个不发（`voice_inject.BACKENDS`，默认是 `clipboard`），
  备份/还原那两段也跟着整个跳过（**连读都不读**）。要隐私就设它，代价是多按一次 Ctrl+V。
- **`CAPSPLUS_VOICE_RESTORE_CLIPBOARD=0`** —— 关掉"粘完把剪贴板还回去"这一步（默认开）。
  关掉的代价是实打实的：**上屏会覆盖你原有的剪贴板内容且不还原**（老行为）。
  留这个开关是为了"宁可覆盖也别多读一次剪贴板"这类偏好，见第四节。
- `CAPSPLUS_VOICE_BADGE=1` 会在**录音期**也建一个窗口（可能抢焦点，默认关；纯显示，不落盘）。
- `CAPSPLUS_ASR_HOST` / `CAPSPLUS_ASR_PORT` / `CAPSPLUS_ASR_LANG` 改识别服务的寻址。
  ⚠ **别把它指向远端主机** —— 一旦指出去，你说过的话就出本机了（默认 `127.0.0.1`，
  单测靠它指到本机 stub，不需要真服务）。
- `CAPSPLUS_STATE_DIR` 只改 `voice.sock` / `voice.log` 的落点（测试注入点，不写别处）。

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
4. **语音输入这一路**（2026-10-17 新加，三条按代价从小到大）：
   - 只想不碰剪贴板：`CAPSPLUS_VOICE_BACKEND=manual`（文本留在弹窗里，你自己复制粘贴；
     备份与还原两段一起跳过，**连读都不读一次**）。
   - 只想"别读别写但也别还原"：没有这种组合 —— 还原的前提就是先读，`CAPSPLUS_VOICE_RESTORE_CLIPBOARD=0`
     只关掉还原那一步，**读还是会发生一次**（要连读都不许就设 `manual`）。
   - 只想不留常驻：`capsplus-voice stop` 收掉 worker（下次按键会自己再起来），
     `systemctl --user disable --now voice-svc` 收掉识别服务。
   - 整个不要这颗键：删掉 `mappings.toml` 里那两行再 `capsplus apply`（仓内键位本来就没有 `m`）。
   `voice.log` 本来就**只记状态不记文本**（剪贴板那一步只记 `剪贴板还原 <code>` 一个代码，
   比如 `restored` / `not_ours`，**不含任何内容**），一般不必清；要清跟 `qbar.log` 一起 `mv` 走即可。

## 三、要把截图 / 配置 / 日志贴到网上之前

按这个清单来（都是"贴出去就收不回"的东西）：

```bash
# 1) 诊断日志与搜索历史（含关键词）
mv ~/.local/state/capsplus/qbar.log ~/.local/state/capsplus/qbar-history.json /tmp/attic-$(date +%Y%m%d)/
# 1b) 语音的日志（只记状态与字节数，不含你说过的话；要贴 status 截图时顺手一起清）
mv ~/.local/state/capsplus/voice.log /tmp/attic-$(date +%Y%m%d)/
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
  语音输入上屏写进剪贴板的也是**你说的话**，但它过一会儿会被**条件还原**成你原来的内容 ——
  还原没发生的那些情况（见第四节那三条）里，你说的话就一直躺在剪贴板上。

## 四、剪贴板：写四处、读一处（2026-10-16 说清楚，2026-10-17 补齐备份/还原）

Qbar 与语音输入会**往系统剪贴板里放东西**（原先这句写在"本项目不做的事"里，那是不准的 ——
早就有三处在写），写入的四处：

| 触发 | 写进剪贴板的内容 | 代码位置 |
| --- | --- | --- |
| `=1+2*3` 回车 | 算出来的结果（`4`） | `qbar/qbar_app.py::_activate` 的 `calc` 支 |
| `-> date` / `-> time` | 日期 / 时间字符串 | `qbar/qbar_app.py::_run_builtin` |
| **`qw 我的问题` 回车**（`ask` 类型） | **你输入的问题原文**，一字不改（不是 URL、不是 URL 编码串） | `qbar/qbar_app.py::_activate` 的 `ask` 支 |
| **语音输入上屏**（`Caps+M` → 弹窗回车，默认 `clipboard` 后端） | **识别出来的那段文本**，一字不改（就是你要打进光标的内容） | `capsplus/voice_inject.py::plan` 的 `wl-copy`，随后 `ydotool` 发 Ctrl+V |

**读只有一处，而且是新加的**：语音输入上屏**之前**会用 `wl-paste` 读一次剪贴板做备份
（`capsplus/voice_inject.py::snapshot_clipboard`）。三条边界写死在这里，别按"绝对安全"来用：

1. **只在剪贴板还是我们写的那段时才还原**（`check_still_ours`）。到点先读回现值逐字节比对：
   还是我们那段 ⇒ 还原；**已经被你或别的应用换掉 ⇒ 一律不还原**（把人家刚复制的东西冲掉，
   比不还原更糟），`capsplus-voice status` 里会写「剪贴板已被别处更新，未还原」。
   读不回现值也按"改过了"处理 —— 宁可少还原一次，不可错盖一次。
2. **快照只在内存，绝不落盘**。`Snapshot.data` 里可能是密码、银行卡号、刚截的图：
   它不写进任何文件、不进 `voice.log`、不进 socket 事件帧、不进任何摘要 ——
   对外只有**类型与字节数**（形如 `已备份 image/png 481234 字节（已回读核对）`）。
   判据：`tests/test_voice_clipboard.py::TestSnapshotNeverHitsDisk`（照"念一句银行卡号"那条的写法）。
   也不维护剪贴板历史：还原完成、进程退出，这份字节就没有了。
3. **读不了 / 备份不了的类型会明确告知"不还原"**。备份只认这几种类型（按优先级，
   挑最不可再生的那一种，一次只还原一种）：`image/png` → `text/uri-list` → `text/html` →
   `text/plain;charset=utf-8` → `text/plain`。清单里只有别的类型（例如 `wayland/roles`、
   `x-special/gnome-copied-files` 这类文件管理器专有目标）、或读回来失败、或超过
   `SNAPSHOT_MAX_BYTES`（64 MiB）⇒ **不发任何还原命令**，状态里写「剪贴板里有 <类型> 无法备份，
   本次会覆盖且不还原」，并提示用 `CAPSPLUS_VOICE_BACKEND=manual` 避开。

其余仍要照实的几点：

- **原本剪贴板是空的** ⇒ 还原动作是**清空**（`wl-copy --clear`），不是写一个空串（空串会让别的
  应用读到一条空文本）。这台机的 wl-clipboard 2.2.1 实测**有** `--clear`（`wl-copy --help` 里
  `-c, --clear  Instead of copying, clear the clipboard.`）；探测按 `--help` 的文本走，
  **探不到就退化成"不还原 + 说明"**，不假设别的机器也有。
- **等一段才还原**（`CLIPBOARD_SETTLE_MS = 300ms`）：`wl-copy` 只是挂上选区所有权，目标应用收到
  Ctrl+V 之后还要异步来取数据，太早回写等于把刚递出去的文本抽走。
  早先对外的"约 800ms 后自动恢复"那个数字没有依据，现按实测取的 300ms 口径写。
- **Wayland 的硬限制**：把选区所有权接回去要求发数据源的客户端**有键盘焦点**。还原那一步由常驻
  worker 发，它多半没有焦点 ⇒ 真机上可能出现"命令发了、rc=0、但实际没还原"。
  实现对此不装死：还原后会**回读核对**，不一致就报 `verify_mismatch`（`已还原` 只在核对通过时才说）。
  这条只有真机能定论，列在未实测清单里。
- **连续两句话**（第二句在 300ms 内接上）：单槽只留**最早**那份快照（那才是你原来的东西），
  第二句只把自己的文本追加进"我们写过的"集合，不拿中间态当原物还原。
- `CAPSPLUS_VOICE_RESTORE_CLIPBOARD=0` 关掉还原 ⇒ 回到"覆盖且不还原"；这时**连备份读都不做**。
- 探测没过（`ydotoold` 不在跑等）时会**降级**成"只写剪贴板、提示你自己按 Ctrl+V"
  （`voice_inject.degraded_clipboard_write`，结果 `ok=False`）—— 这种情况**故意不还原**：
  你还没粘出去，还原始会把你要的文本抽走。
- 粘贴那一步本身失败（`step_failed`）时同样**不还原**：剪贴板里那段文本是你唯一还能救回来的东西。

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

- 不上传、不上报、不埋点、不自更新；**没有任何远端请求**（唯一的网络动作是本机 loopback 上那一次
  识别调用，见开头那条修正口径；要指向远端只能自己改 `CAPSPLUS_ASR_HOST`，改了这条就不成立了）。
  （`ask` 只把网址交给**你自己的默认浏览器**，本仓代码自己不发一个字节到任何站点；
  问题原文是你在网页输入框里 `Ctrl+V` 之后由你按回车发出去的，发不发、发给谁都在你手里。）
- 不记录按键流（除显式 `CAPSPLUS_QBAR_DEBUG=1`）。
- **不维护剪贴板历史**，读的也只有那**一处**（语音上屏前的备份，见第四节）：读完只在内存里留一份，
  还原完成或进程退出就没了，不落盘、不进日志、不进事件帧。
  Qbar 的三处写入（`=计算` / `-> date` / `qw 问题`）**照旧不备份也不还原** ——
  那三条覆盖掉的内容仍然取不回来，这是现状不是设计。
- **音频不落盘**：语音输入采集到的 PCM 只在内存里过（喂 VAD → 拼 WAV → POST → 丢引用），
  `voice.log` 里最多是「送识别 N 字节」这种数量（2026-10-17 读代码核对）。
- 不动 `~/.bash_history`、其他应用的数据。
- 不改系统配置：`/etc/keyd/` 只在你自己跑 `capsplus apply` 时写，且写之前一定先备份。
