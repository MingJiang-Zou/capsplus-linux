# 更新记录

口径先说清楚：本仓已有两个公开 tag —— `v0.1.0`（2026-10-16，第一份公开版）与 `v0.2.0`
（2026-10-16，`ask` 类型 + 可选 GNOME 扩展 raise-browser + CI 扩展体检门），都已发布在
GitHub 仓库的 Releases 页。版本号唯一落点是 `capsplus/__init__.py` 的 `__version__`
（托盘「关于」框显示它），本文件是唯一的版本史，别在别处再写一份。

---

## 未发布 —— 语音输入（Caps+M）

主题：**在本机全离线地把"说话"变成"光标处的文字"**。键位走用户覆盖层（`m` 在上游表里本来就空着），
**没有动仓内 `mapping_data.py`**；识别是 loopback 上的本机 SenseVoice，一个字节都不出本机。

### 链路

`Caps+M` → keyd `command()`（root）→ `bin/capsplus-voice` 降权（runuser + 只报一个显示后端）
→ `voice_worker.py`（Qbar 式 socket 单实例，`~/.local/state/capsplus/voice.sock`，0600）
→ `arecord -f S16_LE -r 16000 -c 1` → 能量门 VAD → `POST http://127.0.0.1:18090/asr-upload?lang=zh`
→ **可编辑确认弹窗**（Enter 上屏 / Shift+Enter 换行 / Esc 丢弃）→ **窗口销毁之后**才注入
（**备份剪贴板** `wl-paste` → `wl-copy` → `ydotool key 29:1 47:1 47:0 29:0` → 300ms 后**条件还原**）。

### 实测数字（2026-10-17，本机，不是估算）

| 项 | 数 |
| --- | --- |
| 出字延迟 | 5.59 秒真人中文语音 → **273ms**（含 HTTP 往返） |
| 识别服务常驻 | **RSS 408MB**、**不占 GPU**（本机 4070S 的 12G 已被别的常驻服务吃到 11.5G） |
| 底噪 / 语音能量 | 房间底噪 **RMS≈19、峰值 108**；语音 **RMS≈4330、峰值 27611**（差约 200 倍） |

### 三条护栏（这次真正的工程量在这儿）

- **能量门是主闸，不是保险**：3 秒**纯底噪**喂给真服务实测返回 `{"ok":true,"text":"我"}`
  （早先另一段返回「嗯」）⇒ 没有 VAD 就会让环境音往用户光标里塞字。门限
  `max(150, min(1000, 4×前300ms底噪RMS))`：下限 150 挡"底噪太轻门形同虚设"，**上限 1000 是被真实
  失真逼出来的** —— 按键后立刻张口时校准窗装的全是人声，门被抬到语音自己的 4 倍，把整句截成
  270ms 碎片（单测钉的同一形状：4200 RMS 平稳音频在 960ms 处被自己判成"说完"）。
  夹具是真采的 PCM（`tests/test_voice_vad.py`），不是合成的理想波形。
- **录音期一个窗口都不建**：xdg-shell 没有"别给我焦点"的请求，`set_accept_focus(False)` 拦不住
  合成器 ⇒ "绝不抢焦点"的唯一可靠实现就是那时候根本没窗口。要指示用 `CAPSPLUS_VOICE_BADGE=1`（自担风险）。
- **注入先探测再动手**：键码从内核头文件现读（不写死数字），探测没过就**拒绝注入**并如实报
  "未验证"；此时降级成"只写剪贴板 + 提示你自己 Ctrl+V"，且结果 `ok=False` —— **不许当成功报**。
  为什么只能这么绕：Wayland 下逐字打中文的路本机实测全断，逐条证据与复现方法写进
  `docs/KNOWN-LIMITS.md`「语音输入的上屏」一节。

### 现状与未做（写清楚，别让下一个人以为已完成）

- **剪贴板备份 + 条件还原（本轮补上）**：上屏那一步过去**只覆盖不恢复**（`voice_inject.py` 里
  `grep wl-paste` 得空），而我早先对外说过"约 800ms 后自动恢复" —— 那句话当时没有实现撑着。
  现在按 `snapshot → wl-copy → Ctrl+V → 等 `CLIPBOARD_SETTLE_MS`(300ms) → **条件还原** → 释放` 做成了
  事实（`voice_inject.snapshot_clipboard` / `check_still_ours` / `perform_restore`，等待由 worker 的
  主循环 `after_ms` 计时、读写在后台线程）。三条边界：
  **只在剪贴板还是我们写的那段时才还原**（被别人改过就明说"剪贴板已被别处更新，未还原"，
  不硬盖）、**快照只在内存**（不落盘、不进 `voice.log`、不进事件帧，对外只有类型与字节数）、
  **读不了的类型明确告知不还原**（备份只认 `image/png`/`text/uri-list`/`text/html`/`text/plain`；
  挑最不可再生的那一种，一次只还原一种）。原本剪贴板为空 ⇒ 还原是 `wl-copy --clear`（本机
  wl-clipboard 2.2.1 的 `--help` 实测有 `--clear`；**探不到就退化成"不还原 + 说明"**，不假设）；
  降级（`needs_manual_paste`）与粘贴失败两种情况**故意不还原**（那时文本还要留给用户自己粘）。
  开关 `CAPSPLUS_VOICE_RESTORE_CLIPBOARD=0` 关掉还原（回到老行为，且连备份读都不做）。
  `capsplus-voice status` 新增一行「剪贴板」，能看出上次是否真还原了。
- **未实测清单**（只有真桌面 + 真人能清）：① 弹窗销毁后焦点是否真还给原文档；② `Ctrl+V` 在
  Edge / Chrome / GNOME 终端 / LibreOffice / VS Code 的实际落点与适用面；②b **剪贴板还原的真机
  时序** —— 300ms 够不够、常驻 worker 没有键盘焦点时 `wl-copy` 能不能真把选区所有权接回去
  （实现只做"回读不一致就报 `verify_mismatch`"，不谎报已还原；真机成不成未测）；②c **中途改
  剪贴板**的实测手感（300ms 内自己拷了东西会不会频繁判否）；③ **ibus / libpinyin 在场时
  Enter 会不会被输入法吃掉**（头号风险，`voice_osd.key_action` 已备但没在真输入法下跑过）；
  ④ 打字中途按 `Caps+M`，键声会不会被误判成语音；⑤ 跨线程碰 GTK 会**段错误 rc=139**
  （试过、已在代码里禁止，但真 Wayland 下长期稳定性未测）。
- 判据：新增 `tests/test_voice_{proto,vad,asr,inject,portability}.py` 五个模块（含"假探针不许碰真系统"、
  "底噪必须判无语音"、"探测没过绝不发命令"这类反例具型）。剪贴板这一轮再加
  `tests/test_voice_clipboard.py`（顺序 / 条件还原两个方向 / 空剪贴板走 `--clear` / 隐私四条，
  五条反例具型逐条真红过：把快照挪到 `wl-copy` 之后、删掉还原步、去掉现值检查、空剪贴板改写空串、
  摘要里漏出快照字节）+ `tests/test_voice_mainloop.py` 两条（新增的 `after_ms` 回调进主线程名单，
  反例具型是"回调里 `wait()` 必须红"）。2026-10-17 实跑
  `python3 -m unittest discover -s tests` → **649 条 OK（skipped=46）**（同轮早前是 579 条）。
  ⚠ 本轮踩到一条 GLib 事实：`idle_add` 的源会被更高优先级的挂起事件**饿死** —— 判据里用
  `timeout_add(1)` 派发，并留 8s 硬兜底（同进程同时加载 `test_voice_mainloop` 时曾把整套卡死 45s+）。
- 运维副作用（都不在本仓安装范围内，属本机配置）：新增依赖 `ydotool` / `wl-clipboard` / `arecord` /
  GTK3；注入守护走 **system 级 root 单元** `ydotoold.service`（原因：组变更对已跑会话无效，
  不能等用户注销重登），本用户加入 `input` 组，包自带的同名 user 单元已 `disable` 防撞车；
  识别服务 `voice-svc` 需 `systemctl --user enable --now voice-svc`（unit 模板与模型都在仓外）。
- 回滚：删 `mappings.toml` 里那两行再 `capsplus apply`（本次 apply 的备份在
  `~/.local/state/capsplus/keyd-conf-backup/capslock-plus.conf.20261017-150609-494922`）；
  `capsplus-voice stop` 收 worker；`systemctl --user disable --now voice-svc` 收服务。
- **`capsplus/__init__.py` 的 `__version__` 这次不动**（本仓规矩：打 tag 时与 CHANGELOG 一起同步）。

---

## 0.2.0 —— 2026-10-16

### 19:00 追加：真正修好"还是新窗口"的那一刀在发送方

- **根因**：Qbar 用 `GLib.usleep(80ms)` 等激活落地 —— 但 `emit_signal` 只是入队，
  主线程睡过去 ⇒ 主循环不转 ⇒ **信号根本没发出去**。扩展早已 ACTIVE，用户实测仍是新窗口。
  改成跑真主循环（`GLib.MainLoop` + `timeout_add`）后，A/B 实测：**A 轮 1→2（+1，复现老毛病），
  B 轮 2→2（+0，原窗口开标签页）** ✓
- 判活改走 **journal**（扩展在 shell 进程里 `emit_signal` 实测上不了总线：`dbus-monitor` 抓 0 条回包
  且无 JS ERROR）：Qbar 后台线程发 Probe → 跑主循环 400ms → `journalctl --user --since …` 取
  `[raise-browser] probe … windows=N`，写进 `raise-browser.json`，`capsplus status` 并排显示。
- 扩展去掉 `ProbeReport` 回包代码（走不通的路不留半成品）；`get_windows(false)` 改无参调用
  （GNOME 50 该方法是 0 参，带参会在 journal 刷 `Too many arguments`）。
- 判据加两道门：**泵门**（`_raise_browser_window` 必须 `_pump(`、不许 `usleep`）与
  **未导入大写常量门**（常量改名时 qbar_app 没跟上，工作树破到一走就 `NameError`，
  而当时判据全绿——因为没人跑过那条分支）。另加 journal 助手、扩展不回包等 5 条。


主题：**能问 AI、能复用浏览器窗口、静默失效能自检**。

- `ask` 链接类型（复制问题 + 打开页面）、DeepSeek 预填引擎 `ds`/`dsp`（读它自己的 `main.js` 取证）、
  千问 `qw`（实测无预填参数，走 copy+open）；Qbar 宽度按**最窄**显示器收敛 + 单元格省略号。
- 可选 GNOME 扩展 `raise-browser`：打开网址前激活浏览器现有窗口（GTK3 递不出 Wayland 激活令牌）；
  走 D-Bus **信号**（本机 GJS 无法导出对象），扩展回 `ProbeReport` 让 Qbar 知道链路活着没。
- `capsplus status` 并排两个读数（shell 的观点 / 链路的观点）+ 登录同步 unit，把"静默不加载"变可见。
- CI 三个 job → 四个：新增 `assets-check`（扩展 JS 的 ESM 语法与 `export default class`、
  metadata uuid、脚本按自身 shebang 过 `-n`、systemd 单元形状）。

---

## 0.1.0 —— 2026-10-15 起（已打标）

一天之内从零做到"能用 + 有判据 + 敢公开"，按主题归并如下（不逐提交流水账）。

### 键位映射：单一真相源 + 渲染 + 落地（`capsplus/`）

- v1 骨架：`mapping_data.py`（Python 常量存键位，注释贴在被注释那一行旁边，避免"数据 + 注释表"
  两份真相）→ `render.py` 渲染 keyd 配置 / 生成 README 键表 / `binding_rows` 给界面用 →
  `apply.py` 备份 → 装 `/etc/keyd` → reload → 抓 `ERROR:` → 出错自动回滚，`--dry-run` 只渲染自检。
- 按 keyd 的**实测边界**修正映射：单条 `macro(...)` 最多 15 个按键事件，超了整条静默丢掉 ——
  原版"选中 10 行"压到 7 行、"上/下 30 行"改 `pageup`/`pagedown`；并补 `Caps+Q` 唤起 Qbar。
  同时立了真机端到端验收脚本 `tools/e2e/check_mappings.py`（`/dev/uinput` 虚拟键盘逐条比对）。
- 用户键位覆盖层落地（`mappings.toml`，只存改过的那几条；没有覆盖层时渲染结果逐字节不变）。
  踩到的 TOML 硬坑：带 `+` 的段名必须加引号（`["capl+alt"]`），写/读/报错三处对齐。
- `command(...)` 唤起 GUI 的降权包装 `bin/keyd-qbar-cmd`（keyd 以 root 执行 `command()`）。
- 修 `runuser` 探测误报、`keyd-qbar-cmd` 兜底绝对路径、Qbar 收到信号后清 socket。

### Qbar（`qbar/`）

- 从"能起窗"做厚：可配置快速链接 + 网页搜索（`qbar.toml`），纯逻辑层 `logic.py` 与配置层
  `search_config.py` **刻意不 import gi**，所以 fuzzy / 算术 / 历史 / `.desktop` 解析在 headless 也能跑单测。
- 快速链接四类型（`search` / `url` / `app` / `file`）+ TOML 写回保真（顺序、`type`、开头注释），
  不写 `type` 的老配置按推断规则照常工作。
- 失焦即隐藏（`hide_on_focus_out`，默认开）；`Caps+Q` 智能切换（失焦时拉回前台而不是藏起来）。
- 修冷启动不抢焦点（首次唤起后打不进字）；加诊断日志 `~/.local/state/capsplus/qbar.log`。
- 修「打开网页会另起一个浏览器窗口」（2026-10-16 实测）：四处 `launch_default_for_uri` / `AppInfo.launch`
  原本都传 `None`（不带激活上下文），桌面无法把它当成「用户主动点开」，Edge 于是新起窗口并把后续
  页面全堆进去。改为带 `Gdk.AppLaunchContext` + 事件时间戳；判据 `tests/test_launch_context.py`
  （AST 扫描，headless 也跑，改回 `None` 立刻红），详见 `docs/KNOWN-LIMITS.md`。
- 修「Qbar 太宽、超出屏幕」（2026-10-16 用户实测）：结果列表的 `CellRendererText` 原先没有 ellipsize、
  标题列也没 expand，所以一条长历史 URL 就能把窗口自然宽度顶爆。现在：标题列 `expand` + 尾部省略，
  窗口宽度按 `logic.window_width()` 钳到**最窄那块屏**的 90%（默认 680 的上限不变）；
  判据 `tests/test_qbar_width.py`（钳位算法 + 源码级防回退 + Pango 必须 require_version）
  与 `test_qbar_window_is_not_wider_than_the_screen`（真构造窗口断宽度，500px 虚拟屏实测 450）。
- 改 `bin/keyd-qbar-cmd`：**只报一个显示后端**——调用者已导出就用它的；否则探 `$XDG_RUNTIME_DIR/wayland-0`
  socket 在不在（在 → Wayland），再否则才退回 `DISPLAY`。原先两个变量一起凭空注入，是浏览器另起窗口的温床。
- **定位**「焦点不在浏览器时另起新窗口」的根因：GTK3 没有 Wayland 激活令牌通道
  （`libgdk-3` 里 `XDG_ACTIVATION_TOKEN` 0 次），不是双报后端、也不是另起进程（主进程数实测恒为 1）。
  三条可行修法与成本写进 `docs/KNOWN-LIMITS.md`；扩展方案需在 GNOME 上重启 shell 才能装新扩展。
  ⚠ 过程中我自己把 `bash` 数组写进了 `#!/bin/sh`（dash）脚本 → **Caps+Q 直接失灵**（keyd 不报错，只是没反应）；
  已改回 POSIX 变量，并把判据从 `bash -n` 升级为**按 shebang 选解释器**（`tests/test_portability.py`）。
- **扩展可见性（A 方案）**：扩展收到 `Probe` 会回一发 `ProbeReport` 信号（带 `windows=` /
  `on-current=`），Qbar 据此判断扩展在不在——确认不在时**不再白等那一帧**；实测状态写进
  `~/.local/state/capsplus/raise-browser.json`，`capsplus status` 现在并排两个读数
  （shell 的观点 + 链路的观点）。修掉一个真 bug：状态文件原本手拼 JSON 把 Python `None` 写进去
  ⇒ 非法 JSON、status 读不了；改用 `json.dumps` 并补了**写→读闭环**判据（原来只测了读那一半）。
  CI 新增 `assets-check` job：扩展 JS 走 `node --check` + 强制 `export default class`、
  metadata 的 uuid 必须等于目录名、`tools/*.sh` 按**自己的 shebang** 过 `-n`、systemd 单元形状检查。
- **新增 GNOME 扩展 `raise-browser`（可选）**：Qbar 打开网址前先用 D-Bus 请扩展把浏览器现有窗口激活，
  再打开 —— 针对「焦点不在浏览器时另起一扇窗」。配套 `tools/install-extension.sh`（装；需**注销重登一次**
  才生效，GNOME 只在启动时扫用户扩展目录）与 `tools/verify-raise-browser.py`（A/B 实测：用扩展 `Probe`
  的窗口数做差判定，+1 = 新窗口、0 = 原窗口开标签页）。`[general] raise_browser` 默认开，
  设置界面第②页有开关；扩展不在时 7ms 静默跳过，功能不退化。判据 `tests/test_raise_browser.py`。
  ⚠ 实现改过两次形态，原因都写进 `docs/KNOWN-LIMITS.md`：第一版用模块级 `export function enable`（GNOME 50 要 `export default class`，报 `not a constructor` **静默不加载**）；第二版想导出 D-Bus 对象，实测本机 GJS 的 `export_object_path` 是 `undefined`、`register_object` 只剩 C 绑定 ⇒ 改走**信号**（`emit_signal` 5 参 / `signal_subscribe` 7 参且回调在最后），并把这些死路钉进判据。

- 默认引擎模板加淘宝 / 闲鱼（两个搜索端点实测 HTTP 200，中文关键词走 `{q}` URL 编码）。
- AI 问答进默认引擎（2026-10-16）：`ds` / `dsp` 走 `search`（DeepSeek 网页端**支持** URL 预填与
  自动发送，参数名与 `≤800 字` 那条限制是从它 `main.js` 里读出来的）；`qw`（千问）走新加的
  第五种类型 **`ask`（复制 + 打开）** —— 扒它 `@ali/qianwen-web/4.10.0` 的 async chunk 得到三条证据
  证明站方没给口子（只读夸克集成参数 / 唯一的 `?q=` 是夸克搜索构造器 / 明确抛
  `"Workbench URL auto-send is not supported"`），于是改成"问题原文进剪贴板 + 打开网页"这条
  **不依赖站点配合**的路。证据与结论见 `docs/KNOWN-LIMITS.md`。
- `ask` 的历史建模：`Entry` 新增 `clip` 字段带问题原文，历史键的载荷用 `JSON([网址, 问题])`
  —— **不用字符串分隔符**拼两个字段（问题里有换行 / 逗号 / `|` / 中文长句都会串位），
  所以点历史能"重新复制同一个问题 + 重新打开"。判据 `tests/test_qbar_ask_type.py`：
  脏输入逐字符往返、`_activate` 的 ask 支源码级体检（复制漏了 / 复制成 URL / 忘了打开 /
  绕过 `_open_url` 四种违例都红）、以及 mock 掉 GTK 的动作层断言（不建窗口、不叫浏览器）。
- `docs/PRIVACY.md` 补一节「剪贴板：只写、不读」：原先写在"本项目不做的事"里那句"不动剪贴板"
  是**不准的**（算数结果、`-> date/time` 早就在写），现在把三处写入与 `ask` 落盘的副作用写清。

### 托盘（`tray/`）

- GTK3 + AyatanaAppIndicator3：打开 Qbar / 设置… / 编辑快速链接 / 启停映射 / 重载 /
  打开映射文件 / 看最近日志 / 关于；外部命令一律后台线程 + `GLib.idle_add` 回收，托盘不许被命令卡住。
- 用 `keyd listen` 读层状态进 tooltip（取不到就静默降级）。
- 应用菜单只留必要条目（托盘走 autostart，不在应用菜单里放重复入口）。

### 设置窗口 `capsplus-settings`

- 三页（快速链接 / 通用 / 按键映射）+ Unix socket 单实例；入口三个：托盘「设置…」、
  Qbar 里 `-> settings`、命令行 `capsplus-settings`。
- 界面可读性：覆盖行加粗；段名提示只在 keyd 写法与 TOML 写法真不一样时才说。
- **治本方案 A**：Wayland/GTK3 下 TreeView 的 cell editor 是不跟随滚动的独立 surface，
  表格一滚，编辑框视觉上落到别的行 —— 用户以为改 `t`，写进盘的是 `k`。
  于是把编辑控件整个搬出滚动区域：两页表格**只读**，表格下方固定编辑条/表单条，
  外加两道保险（编辑条与状态栏两边都写明"要改的是 `capl / t`"；提交时核对编辑条认的格子
  == 表格选中的格子，不一致直接拦下）。
- 修「滚动时下拉悬浮在原处」（先用"滚动即结束该格编辑会话"治标，随后被方案 A 取代）。
- 修「点别处不消失」的根因：PyGI 里 `window.is_active` 是**方法**，漏括号＝恒真的 bound method，
  失焦判定永远走不到隐藏；补源码级 AST 护栏（GUI 模块里这几个名字必须带括号调用）。
- 修一处自引入的严重回归：`settings_app` 加了 `from gi.repository import Gdk` 却没
  `require_version("Gdk","3.0")`，入口一跑就 ImportError，图标点了没反应 —— 而单测全绿。
  于是加了**子进程跑真入口**的判据（`test_real_entry_points_import_cleanly`）。

### 测试与文档

- 单测只用标准库 `unittest`（无 pytest）。窗口测试提速：`pump` 支持 `until` 条件，
  不再固定等满 0.8s，全量 17.1s → 0.85s。
- 判据成对：源码级 AST 护栏（`tests/test_table_readonly.py`，headless 也拦得住）+
  真窗口行为级断言（`tests/test_settings_window.py`）。
- 止住单测泄漏（`mkdtemp()` 换 `TemporaryDirectory` + `addCleanup`）。
- README 由 `README.template.md` + 映射数据生成（`tools/gen-readme.py`）：键表不手写，
  改映射即改文档；「两页表格为什么只读」「明确不做」两段同步方案 A 的事实与理由。

### 公开前的整改（2026-10-16）

- **GPL-2.0 合规**：`LICENSE` 全文（取自 Debian `/usr/share/common-licenses/GPL-2`）；30 个源文件
  顶部加 `SPDX-License-Identifier` 声明块；`capsplus render` 生成的 keyd 配置头带上游出处；
  README 新增「许可与上游」（上游 `wo52616111/capslock-plus` 是 GPL-2.0，本仓的映射表是它的翻译品）。
- **跨发行版可移植**：新增 `capsplus/env.py` 作为唯一落点 —— keyd 二进制名依次试
  `keyd.rvaiya`（Debian/Ubuntu）→ `keyd`（其余发行版）→ 兜底；systemd 单元名恒为 `keyd`；
  用户 bin 目录按 `Path.home()` 现算（`CAPSPLUS_BINDIR` 可覆盖）；`packaging/*.desktop`
  改占位符由 `install.sh` 渲染；`install.sh` 预检查加 `python3 >= 3.11` 与 keyd 是否已装
  （按发行版给命令，不替人装）。
- **去个人化**：工作树里所有个人姓名与个人路径换成中性表述（日期与技术事实保留）。
- **CI**：`.github/workflows/tests.yml`（ubuntu-latest × py3.11/3.12，不装 gi、不碰 `/etc`、
  不用 sudo）+ `tests/test_no_gi_smoke.py` 守着"没有 python3-gi 也得能过收集阶段"。
- **文档**：`docs/KNOWN-LIMITS.md`（13 条实测平台限制，每条带日期与复现方法）、
  `docs/PRIVACY.md`（会在本机留哪些文件、含不含搜索内容、分享前怎么清）。
- 界面测试改为默认不跑（`CAPSPLUS_GUI=1` 才跑，并在跑之前检查 GDK 实际后端）——
  真弹窗会抢走正在用人手里的键盘焦点，这是骚扰。

---

## 还没做的事

放 issue / README「与原版 Capslock+ 的差异」一节，不在这里记账；**明确不做**的三项
（独立剪贴板 B 组、WinBind 切窗口、热字符串）理由写在 README 里。
