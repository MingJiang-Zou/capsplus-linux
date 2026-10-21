# 更新记录

口径先说清楚：本仓已有两个公开 tag —— `v0.1.0`（2026-10-16，第一份公开版）与 `v0.2.0`
（2026-10-16，`ask` 类型 + 可选 GNOME 扩展 raise-browser + CI 扩展体检门），都已发布在
GitHub 仓库的 Releases 页。版本号唯一落点是 `capsplus/__init__.py` 的 `__version__`
（托盘「关于」框显示它），本文件是唯一的版本史，别在别处再写一份。

---

## 未发布 —— 修 bug 的一轮（2026-10-21）

### 2026-10-21 事故：结果面板的「复制」按钮点了没反应（翻译 / OCR 的文本复制不出来）

**现象**（明江原话）：「翻译以及 OCR 出来的文本点击那个复制按钮复制不出来」。

**根因**：`Annotator.copy_panel_text()` 里那行是
`display.get_clipboard(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)` —— `get_clipboard` 是
**GTK4** 的 API，本仓是 GTK3，真机上是
`AttributeError: 'GdkWaylandDisplay' object has no attribute 'get_clipboard'`。
异常抛在 GTK 的信号处理器里，而这条链是 `Caps+F1` 由 keyd 起的进程、stderr 落在 journal 里没人看
⇒ 现象就是"按钮点了完全没反应"。与 2026-10-20 那个 `cairo.Context.flush()` 同一类：调了一个
**这个对象上不存在**的方法。另外一半原因：即便换成 GTK3 的 `Gtk.Clipboard`，GTK 自己的剪贴板由
**本进程**持有选区，标注窗一关、进程一退文字就没了 —— 那也照样"复制了粘不出来"。

**修复**：
· 改走**与 Ctrl+C 同一条** `wl-copy` 文本链路（`copy_text_to_clipboard`，可注入）—— 选区由外部进程
  持有，窗口关掉文字还在；它已带"留守者握着 stderr"那套守护步处理，不会卡满 5 秒把界面冻住；
· 新增 `Annotator.say()`：成功 / 失败都立刻刷到状态栏（"复制成功"和"点了没反应"不许长得一样）；
· 抽屉按钮的 tooltip 从「复制（Ctrl+C）」改成「复制这段文字」（Ctrl+C 在标注窗里复制的是**图片**，
  那行提示当时也在把排查带偏）。

**判据（都带反例具型）**：
· `tests/test_shot_annotate.py::TestPanelCopyGoesThroughWlCopy`（5 条）—— 送出去的就是面板该复制的那段
  （有译文优先译文）/ 默认实现必须是 `copy_text_to_clipboard` / 失败原因原样落到状态栏 /
  面板没内容时**不许**碰剪贴板。**这条路径过去一条判据都没有**，所以它能一直坏着。
· `tests/test_shot_draw_smoke.py::TestNoGtk4ApisInGtk3Code`（2 条）—— 把"调了不存在的方法"体检从
  **模块级**（`Gtk.xxx` / `Gdk.xxx`）扩到**实例方法**：`display = Gdk.Display.get_default()` 之后所有
  `display.xxx` 逐个拿真 `Gdk.Display` 类 `hasattr` 核。反例真红过：把那行塞回去 →
  `line 1435: display.get_clipboard() 在 Display 上不存在`，还原后 md5 逐字节一致。

**真机复验**（明江："可以复制出来了"）；本机三口径 1539 OK（skip 81 / 273 / 1），`compileall` rc=0。

## 未发布 —— 文档与判据卫生：生成器不再能吃正文 + 一条"只在有桌面时才对"的判据

两件收尾上的小事，都属"会反复伤人"那一类（各配反例具型，红过才留）。

### 2026-10-20 事故：`tools/gen-readme.py` 会把 README 的整节删掉

**现象**：`README.md` 与 `README.template.md` 早已不同步（历史提交只改 README、没改模板），
谁跑一次 `tools/gen-readme.py`，就把 README 里手工加过的整节删掉 —— 实测删掉「安装：一条命令，
一份实现」共 **105 行**（靠 git 才捞回来）；而模板仍缺那几节 ⇒ 下一个人再跑还会再删一次。

**原因**：旧生成器是「模板 → **整份覆盖** README」，且没有任何护栏；`README.template.md` 是 README
的第二份真相，两份各写各的必然漂。

**修复**：README 手工维护，生成器只碰生成区。

| 项 | 落点 |
| --- | --- |
| 生成区标记 | README 的两张键位表夹在 `<!-- BEGIN/END GENERATED: key-table:* -->` 之间 |
| 只写生成区 | `tools/gen-readme.py` 只重写夹住的那几行，**正文一个字节都不碰**（结构上不可能被覆盖）|
| 三道 fail-closed 闸 | ① 标记必须唯一且有序；② 正文不许有未提交的改动（**只在干净树上跑**）；③ 拼装后自证正文逐字节没变。任一不过 = **非零退出 + 一个字节都不写** |
| 退役 | `README.template.md` 退场（`git show 96af83d:README.template.md` 可取回）|

**判据**（都在真仓上跑过，带 md5 前后对比）：干净树跑生成 = **no-op**（README 逐字节不变）；
往 README 加一节再跑 = **拒绝 + md5 不变**；生成区标记被改坏 = 拒绝。
「键表不手写」这条规矩没变：要改键表就改 `capsplus/mapping_data.py`，再跑那条命令。

### 同批：一条真跑 `wl-copy` 的判据在无显示的 shell 里挂死

**现象**：`test_copy_to_clipboard_is_fast_with_a_daemon_shaped_command` 真跑 `wl-copy --type image/png`，
**没有合成器时那条命令根本不肯退出**（手跑 `timeout 15 wl-copy --type image/png` ⇒ rc=124）⇒
卡满 5 秒 timeout 报红，还把 `test_no_gi_smoke` 里「整套 discover 退出码为 0」那条**级联带红**
（三种口径各 2 条红）。

**修复**：形状与行为分开验 ——

* **形状**：`clipboard_command()` 必须还是 `["wl-copy", "--type", "image/png"]`（单独一条判据，改了就红）；
* **行为**：命令换成等价形状的 stub（`sh -c 'sleep 3 & exit 0'`：父进程立刻退、留守者抱着 stderr），
  端到端跑一遍 `copy_to_clipboard` —— 不碰真剪贴板、不需要显示服务器、不需要真装 wl-copy。
  runner 要是又回到 `capture_output=True`，留守者抱住那条永不 EOF 的管子 ⇒ 慢过 1 秒就红。

顺带修掉一个副作用：老版在**真桌面**上跑全量单测，会把明江的剪贴板塞进一张 PNG；现在不会了。

---

## 未发布 —— 扩展开发回路：部署前预检 + 登录后自证 + 无痛自检

主题：**把"改一行 extension.js 就要注销重登一次"从日常变成最后一步**。这一轮的主诉是明江原话
「当前这个工具是当成正式项目去做的，老是要重登 感觉也不好吧」——那天他先后 3 次因为改扩展而重启/重登。

### 2026-10-20 事故：坏文件进了扩展目录 ⇒ ERROR 态 ⇒ 只能注销重登

**现象**：登录后按 Caps+F1 完全没反应。`gnome-extensions info` 报 `状态: ERROR`；
总线名 `NameHasOwner` 回 `(false,)`。journal 里的原始证据：

    10月 20 17:35:34 gnome-shell[7532]: Extension raise-browser@capsplus.example:
      TypeError: malformed UTF-8 character sequence at offset 8190

**原因**：`offset 8190 ≈ 8192-2` = 一个三字节汉字被切在两半 —— 登录同步脚本与 gnome-shell
抢同一个文件，shell 读到了 `cp` 到一半的 `extension.js`。而 Wayland 下**没有就地重载**：
`ReloadExtension` 上游已废弃（实测回 `deprecated and does not work`），`disable/enable` 不会重新
import ⇒ 只能注销重登。

**修复**（三道闸，各自有判据）：

| 闸 | 落点 | 判据 |
| --- | --- | --- |
| **部署前预检**（新） | `tools/preflight-extension.sh` + `capsplus/extpreflight.py`：合法 UTF-8（报坏在第几字节）/ metadata / **行首** `export default class` / `node --check` / 带 mock Shell 的 import 冒烟。是 `--sync` 的**强制闸门**，红了**一个字节都不写**（连目标目录都不建）| `tests/test_ext_preflight.py`（含三种反例 + "逐字节没被写"的端到端）|
| 原子落盘（上一轮） | `install-extension.sh --sync`：同目录临时名 + `rename`、内容相同就不写 | 同上 + `test_raise_browser.py` 的脚本形状判据 |
| **登录后自证**（新） | `tools/verify-after-login.sh` + `capsplus/extverify.py`：七项 PASS/FAIL 表（shell 单元 / 本次开机 ready 行 / 总线名有主 / Probe 往返 / 真抓一帧 200×150 且非纯色 / 贴图+清贴图 / 遮罩起停）| `tests/test_ext_verify.py`（每项正反例、注入依赖、不碰真屏）|

### 2026-10-20 实测：无痛自检（`ext-bench.sh`）能验什么、不能验什么

`dbus-run-session` + `gnome-shell --headless` 里**能**验：扩展加载（`ready capture=on pin=on …`）、
总线名有主（`(true,)`）、Probe 往返（发信号 → 日志多一行）、贴图 + 清贴图（ack 文件回来）。

**不能**验抓帧 —— 而且它会把 shell 打挂：headless 的 mutter 不做 mode setting ⇒ stage 0×0 ⇒
`Failed to take screenshot: Failed to create 0x0 texture`，同一时刻内核日志出现
`pool-N[TID]: segfault at 0 … in libcairo.so.2`（进程退出码 139，两次尝试两次都中）。
⇒ 抓帧只能在真会话里验；`ext-bench.sh` 也补了"绝不许抓帧"的判据
（`test_bench_never_grabs_a_frame`）。

### 工程规矩（写进 `docs/SHOT-TOOL.md` 第三节）

新功能优先做在 **Python/GTK 侧**（改 Python 是秒级热更新），扩展里只加"**shell 特权原语**"。
依据（`extension.js` 988 行，按花括号配平量）：贴图 + 工具条 UI **439 行 = 44%**（正是反复改的那块）、
D-Bus 接线 135 行 = 14%、抓帧 117 行 = 12%、窗口激活 63 行 = 6% ⇒ **真正非 shell 不可的只有 ~18%**。

## 未发布 —— 一处管理：一条命令装 / 起 / 查 / 修

主题：**把这套工具的"装、起、停、查、修"从五处收成一处**。这一轮的主诉不是功能缺失，是
"这里开一个东西那边开一个东西，还没有统一的管理与启动器"。

### 2026-10-20 事故：装机与启动散在五处，漏一步就半残，坏了也不在一个地方看见

**现象**（用户视角）：改完代码/换台机器之后，得自己记着做五件事 ——
`install.sh`（bin 软链 + 菜单入口 + autostart）、`tools/install-extension.sh`（GNOME 扩展副本）、
`capsplus apply`（keyd 配置）、`sed` 渲染两个 systemd unit 再 `enable`、以及"托盘到底走 autostart
还是 unit"。任何一件忘了，症状都是**某个键按了没反应**，而去看的地方彼此不知道对方存在。

**原因**：五条路径各自都"对"，但它们没有共同的入口，也没有共同的状态视图。于是：
· `capsplus status` 只报 keyd 一项（那是最早写的一个），扩展/语音/托盘坏了它不说；
· 托盘走 `~/.config/autostart`、扩展校验走独立 unit —— 两条并行自启路，`systemctl --user stop`
  只能停一半；
· 应用菜单里有两条入口（Qbar + 设置），用户 2026-10-15 的原话是"应用里面有两个"。

**修复**（一份实现，多处叫法）：

| 原来 | 现在 |
| --- | --- |
| `install.sh` / `tools/install-extension.sh` / `capsplus apply` / 手渲 unit / autostart | **`capsplus install`**（`./install.sh` 只是它的壳子）：依赖检查 → bin 软链 → **一条**菜单入口 → 扩展副本（原子落盘）→ keyd 配置（备份/校验/回滚）→ systemd 单元组 → 退役旧件 |
| `capsplus status` 只看 keyd | **`capsplus status` 一页看全**：keyd / 扩展 / 语音 / 截图 / 用户侧栈，**每个红项都带下一步动作** |
| 两条自启路 | **`capsplus.target`**（systemd --user）一个开关：`capsplus up/down/restart`；autostart 那份退役 |
| 应用菜单两条 | **一条「CapsPlus」** = 管理器窗口；Qbar 从管理器或 Caps+Q 开 |
| 没有卸载 | **`capsplus uninstall [--purge-keyd] [--purge-extension]`**：用户侧全退，默认**不动** `/etc/keyd` 与 `~/.config/capsplus/`、`~/.local/state/capsplus/` |
| 坏了靠猜 | **`capsplus doctor --fix`**：只自动修能安全修的装机件；`capsplus logs` / `capsplus test` 一个入口 |

**一条命令的落地细节**（都在 `capsplus/install.py`，判据 `tests/test_cli_install.py` 21 条）：
· **幂等**：跑两遍结果一样；单元/菜单内容没变就一个字节都不写（`write_atomic` 同目录 rename）。
· **可预演**：`--dry-run` 把每一步先打一遍计划，一个字节都不改。
· **退役不是删除**：旧的 Qbar 菜单入口、旧的 autostart 托盘一律 `mv` 进 `/tmp/attic-<日期>/`，
  撞名加序号（不许覆盖上一次退役下来的东西）。
· **不假装成功**：`up` 在单元没装时退回直接起进程并说明；`install` 有步骤失败仍把其余步骤跑完，
  最后把状态页打给你看，并按退出码区分。
· **迁移期的一个真坑**：托盘靠 `tray.lock` 抢单实例、抢不到**退 1**，而单元是 `Restart=on-failure`
  ⇒ 从 autostart 迁到单元组时，如果不先收掉旧实例就是"每 5 秒重启一次"。`capsplus up` 现在会
  先停下"不是单元起的那个托盘"再 start target（反例具型在 `TestUpDown` 里钉着顺序）。

### 管理器窗口：应用菜单里唯一的入口

`capsplus-settings` 从"三页设置窗"变成**四页管理器**，第一页是「状态」：
内容是 `capsplus status` 的同一份（直接调 `capsplus.cli.collect_status()`，**不另算一份**），
底下五个按钮 `重新检查` `一键修复` `同步扩展` `应用键位` `重启托盘` + `打开日志目录`。
探测放在后台线程（否则窗口会假死 1~2 秒），按钮只调 `capsplus` 子命令（不在界面里重写一遍装机逻辑）。

### 扩展：部署 / 校验 / 自检三件事分开管（不再靠"注销重登"来试探）

**现象**（用户视角）：改完 `extension.js` 之后唯一能做的验证是注销重登；重登回来还是不对，就再来一次。
**原因**（分三层，之前混在一起）：

1. **写入时机错了**：登录那个 unit 跑的是 `install-extension.sh --quiet`，也就是**每次登录都 `cp`**
   一遍扩展目录 —— 而它 `After=graphical-session.target`，正好与 gnome-shell 扫描扩展目录撞在同一秒。
   2026-10-20 实测的现场是 shell 读到**拷贝中的半截文件**：
   `TypeError: malformed UTF-8 character sequence at offset 8190`（8190 = 8192−2，一个汉字被切成两半）。
   扩展 import 失败 ⇒ 抓帧通道没有 ⇒ `Caps+F1` **完全没有反应**，而用户只会说"截图又坏了"。
2. **"改得对不对"与"shell 里跑的是不是新代码"是两件事**：GNOME 不会就地重新 import 扩展
   （`ReloadExtension` 上游已废弃，实测回 `deprecated and does not work`；`disable/enable` 也不重新 import），
   所以"重登"是**平台**的硬边界。但"我的 JS 有没有语法错/加载期抛异常"这件事**不该**靠重登去试。
3. **失败是静默的**：`Caps+F1` 那条路是 keyd 的 `command(...)` 起的进程，stdout/stderr 落在 journal 里没人看。

**修复**：

| 现在 | 干什么 | 怎么写 / 怎么判 |
| --- | --- | --- |
| `capsplus ext sync` | **唯一写扩展副本的地方** | 原子落盘（同目录临时名 + rename）+ 内容相同不写 + 落盘后 md5 校对 |
| `capsplus ext verify` | 登录时只**读**：md5 比对 + uuid 在不在 `enabled-extensions` | 不一致 → stderr + 桌面通知 + **exit 1**（红在面板上），**一个字节都不写** |
| `capsplus ext bench`（新 `tools/ext-bench.sh`） | 在**隔离的 `dbus-run-session` + headless gnome-shell** 里真加载一次 | 认 journal 里那行 `[raise-browser] ready capture=on pin=on …`；正例 `EXT_OK（1s）`，坏扩展真红（实测 `SyntaxError: unexpected token: identifier …:990:6`） |

**三条实测结论**（都写进 `docs/KNOWN-LIMITS.md`「扩展的部署 / 校验 / 自检」）：
· 判活**只能认日志**：本机 gjs 的 `register_object` 是原始 C 绑定（`At least 5 arguments required`），
  扩展只 `own_name` + `signal_subscribe`，所以隔离会话里 `gdbus --method Probe` 会回
  `UnknownMethod 对象在路径处不存在` —— 别拿它当"扩展没加载"的证据。
· `bench` 绿 = **文件层面没问题**，不等于他那个会话里的 shell 已经换上新代码（那仍需一次重登）。
  所以 `status` 的 ERROR 文案改成"先跑 bench：绿了说明只差重登，红了才是代码坏了"。
· 失败必须出声：抓帧/贴图超时经 `capsplus/shot_frame.py:notify_failure` 发桌面通知（GIO 优先，
  没有 gi 退 `notify-send`），三条闸防骚扰（`CAPSPLUS_NOTIFY=0` / 单测进程里 / stderr 是终端）。

### 判据

新增 `tests/test_cli_install.py`（**21 条**，含四条"判据抓不抓得住旧写法"的反例具型：
未知子命令必须退 2 / `--dry-run` 不许落盘 / 退役撞名不许覆盖 / **报红却不给动作必须被
`groups_without_actions` 抓住**）；`tests/test_settings_window.py` 的页数与落到哪一页跟着改成四页，
并新增"状态页打开就有字"那条。三口径：
`python3 -m unittest discover -s tests` → 全套 OK（skip 77）；Xvfb + `CAPSPLUS_GUI=1` → 界面用例真跑；
`PYTHONPATH=tools/ci/nogi` → 无 gi 也能收集。

## 未发布 —— 语音输入（Caps+M）

主题：**在本机全离线地把"说话"变成"光标处的文字"**。键位走用户覆盖层（`m` 在上游表里本来就空着），
**没有动仓内 `mapping_data.py`**；识别是 loopback 上的本机 SenseVoice，一个字节都不出本机。

### 链路

`Caps+M` → keyd `command()`（root）→ `bin/capsplus-voice` 降权（runuser + 只报一个显示后端）
→ `voice_worker.py`（Qbar 式 socket 单实例，`~/.local/state/capsplus/voice.sock`，0600）
→ `arecord -f S16_LE -r 16000 -c 1` → 能量门 VAD → `POST http://127.0.0.1:18090/asr-upload?lang=zh`
→ **可编辑确认弹窗**（Enter 上屏 / Shift+Enter 换行 / Esc 丢弃）→ **等到"窗口已 unmap 或收到
focus-out"这个事实**才注入（下限 120ms、上限兜底 400ms，见下文"上屏的触发条件"）
（**备份剪贴板** `wl-paste` → `wl-copy` → `ydotool key 29:1 47:1 47:0 29:0` → 300ms 后**条件还原**）。

### 实测数字（2026-10-17，本机，不是估算）

| 项 | 数 |
| --- | --- |
| 出字延迟 | 5.59 秒真人中文语音 → **273ms**（含 HTTP 往返） |
| 识别服务常驻 | **RSS 408MB**、**不占 GPU**（本机 4070S 的 12G 已被别的常驻服务吃到 11.5G） |
| 底噪 / 语音能量 | 房间底噪 **RMS≈19、峰值 108**；语音 **RMS≈4330、峰值 27611**（差约 200 倍） |

### 2026-10-20 —— 「语音转文字几乎不可用，只可以是短语音，识别率有点低」

明江的原话。三件事查下来都不是服务端与设备的问题，**全是客户端自己身上**，而且**录音过程中屏幕上
什么都没有** —— 所以他只能靠感觉描述，我们只能靠猜。这一轮把它们都量出来、并摆到台面上。

| 症状 | 根因 | 改前 → 改后（同一段真人语音 `test_wavs/zh.wav`） |
| --- | --- | --- |
| 只可以是短语音 | `MAX_MS=20000` 是客户端自造的上限，到点**静默截断** | 同一句重复 10 次（57.1s）：旧参数在 **11.5s** 停、识别 26 字；新参数录满 **57.1s**、识别 **130 字**（13 字×10，一字不差）。服务端两次都 HTTP 200 |
| 一句话被切成两半 | 尾静音**固定 700ms**，正好卡在"换气"与"说完了"之间 | 含 900ms 句中停顿那段（12.1s）：旧参数 **5.8s** 判停、只送出 5.3s；新参数录完 **12.1s**，识别 26 字＝两遍全须全尾 |
| 首字常丢 | 句首与句尾共用一个 150ms pad | 前置静音 150ms：pad=150 → 「**放**时间…」；pad=300 → 「开饭时间…」（首字在了，"饭"是模型自己的错） |

- **参数**（`e3a2325`）：上限 60s 且到点**四处出声**（decoding 事件 / review 弹窗顶栏 / `voice.log` /
  `status`）；尾静音自适应 `min(2400, 1200 + 每说满 1s +100ms)`；句首 300ms、句尾 250ms 分开留。
- **录音期看得见**：牌子（`CAPSPLUS_VOICE_BADGE=1`，默认关是因为 Wayland 抢焦点）每秒刷
  `● 录音中 3.2s · 电平 RMS 412 / 峰 3000 · 有声（门限 150）`；没开牌子也能从 `status` 的
  `录音中读数` 行与 `voice.log` 的 `voice_progress`（每 5s 限流一行，只有状态量）看到。
  读的是同一帧的 RMS/峰值/门限（`Recorder.live()`），时间轴与 VAD 共用 `vad.frames`，两处不会漂。
- **`capsplus-voice meter --live`**：实时把 RMS/峰值刷在同一行，条子半程＝当前门限；与 `meter`
  同一条 arecord 命令、同一个测量函数。判读只有一处（`voice_osd.level_verdict`），牌子与它共用。
- **术语替换表**（`capsplus/data/voice_glossary.tsv` + `~/.local/share/capsplus/`）：SenseVoice
  **没有热词入口**（`OfflineRecognizer` 上没有 hotwords），所以用确定性后处理兜专名错字。
  三条护栏：ASCII 键要词边界、中文键 ≥2 字、最长优先且改完即止不级联。
- **五种失败各自带下一步**：服务不在→`systemctl --user start voice-svc`、超时→重启并看日志、
  无 arecord→装 alsa-utils、设备被占→`pactl set-default-source`（`HINTS["device-busy"]` 过去
  **是从没被用到的死文案**，设备被占时走的是 `no-data`，指的下一步方向是错的）、太短/没说话→
  "对着麦克风说完整一句"（**不许**报成"麦克风坏了"）。
- **判据**：新增 `tests/test_voice_glossary.py`(18) / `test_voice_progress.py`(30) /
  `test_voice_failure_hints.py`(11)，`test_voice_asr.py` +3、`test_voice_portability.py` +2。
  13 条反例具型逐条真红过、脚本在 `~/.dsh/scripts/capsplus-voice/fixtures/progress-and-glossary/red.sh`。

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
- **上屏的触发条件（2026-10-17 晚，真机现象"贴上又闪掉"之后改的）**：过去只有一个定时器 ——
  窗口销毁后固定等 `FOCUS_RETURN_MS = 120ms` 就发 Ctrl+V。**"过了 120 毫秒"不等于"焦点回到了
  原文档"**，而 Ctrl+V 落进我们那个正在消失的弹窗，用户看到的就正是"字进去了、又随窗口没了"。
  现在改成三段，且**事实与时长分开**（`capsplus/voice_worker.py` 文件头逐段写了理由）：
  必要条件 = `voice_osd` 记下的生命周期时间线里出现 `unmap` 或 `focus_out`（纯数据，条目只有
  `{kind, us}`）；下限 `INJECT_FLOOR_MS=120`（X11 实测 `unmap` 与 `destroy` **同步**、Enter 后 0.03ms
  就到，"只等事件"在那边等于不等，不许把注入提得比现状更早）；上限兜底 `INJECT_WAIT_CAP_MS=400`
  （Wayland 可能一个这类通知都不给客户端 ⇒ 等满也注入，但这一次**写成"未确认"**，
  状态行 / `voice.log` / `status` 三处同步，不写成"上屏结束"）。等待全程由主循环计时，
  主线程一处 `sleep` 都没有（AST 名册新增这两个函数，且加了"名册里的名字必须在 worker 里
  真存在"那条门）。
  **诊断能自证**（这是本轮主要交付物）：`voice.log` 里每次上屏多三行固定前缀 + key=value 的
  可解析行 —— `inject_diag gate=… enter_ms=… release_ms=… visible=… realized=… mapped=…
  unmap=… focus_out=… destroy=… entries=… tl=map@-8,finish@0,unmap@250`、
  `inject_done backend=… ok=… code=… steps=… gate=…`、
  `restore_diag branch=armed/skip/merged/restored/not_ours/verify_mismatch/… still_ours=1/0/na`；
  `capsplus-voice status` 多一行「上屏结论」= 结论 + 依据 + 那几个状态量（一行，可直接贴）。
  全部**只有状态量**：识别文本与剪贴板内容一个字符都不进（隐私判据按"念一句银行卡号"那条扩到
  新字段）。判据：新增 `tests/test_voice_focus_gate.py`（时间线纯数据 / 事件路径 / 兜底未确认 /
  隐私四条 + 假件与真窗口不许漂，四条反例具型在本轮逐条真红过：把触发条件改回"固定 120ms 立即发"、
  删掉上限兜底、兜底那次也报"上屏结束"、在到点回调里 `wait()`）；`test_voice_mainloop.py` 的真窗口
  那条从"隔了 ≥100ms"改成钉**顺序**（焦点交出的事实必须早于注入）。2026-10-17 晚实跑
  `python3 -m unittest discover -s tests` → **682 条 OK（skipped=48）**；Xvfb + `CAPSPLUS_GUI=1`
  全量 → **682 条 OK（skipped=1）**。真机那三种可能（贴进自己 / 目标应用自己清掉 / `wl-copy`
  没拿到选区所有权粘出旧内容）**还没定** —— **2026-10-19 定了：三种可能都不是**，是"写剪贴板"那一步
  压根没成功（`steps=0` + 耗时差 = `STEP_TIMEOUT_S`，见本轮 2026-10-19 条目）。
  详见 `docs/KNOWN-LIMITS.md`「语音输入的上屏」第 10 条。
- **未实测清单**（只有真桌面 + 真人能清）：① 弹窗销毁后焦点是否真还给原文档（**本轮已经把"能不能
  观察到"变成了数据**：X11/Xvfb 实测 `unmap` 与 `destroy` 同步、`focus_out` 在没有窗口管理器时压根
  不来；mutter 下收不收得到、以及"收到之后焦点到底给了谁"仍未取证 —— 那要靠真机那一行
  `inject_diag` 才能定）；② `Ctrl+V` 在
  Edge / Chrome / GNOME 终端 / LibreOffice / VS Code 的实际落点与适用面；②b **剪贴板还原的真机
  时序** —— 300ms 够不够、常驻 worker 没有键盘焦点时 `wl-copy` 能不能真把选区所有权接回去
  （实现只做"回读不一致就报 `verify_mismatch`"，不谎报已还原。**2026-10-19 部分实测**：无键盘
  焦点、无 activation token 的进程 `wl-copy` **能**把选区设成（见本轮条目），剩下的只是 300ms 这个
  时长够不够、以及 worker 自己那条回读路在真机上的表现）；②c **中途改
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

### 2026-10-19 事故：上屏那一步从落地起**一次都没真贴上过字**（写剪贴板被判 5 秒超时）

- **现象（明江真机 09:15）**：识别出 6 个字，焦点交出的事实也拿到了
  （`gate=floor … visible=0 mapped=0 unmap=1 destroy=1`，**不是**"贴进自己弹窗"那个方向），随后
  `inject_done backend=clipboard ok=0 code=step_failed steps=0`；两条耗时读数相减 = **5.0 秒**，
  恰好是 `STEP_TIMEOUT_S` 本身 ⇒ 第一步"写剪贴板"被判失败，**Ctrl+V 从未执行**。
- **根因（精确到 fd，别再靠"应该继承了管道"这种模糊说法）**：执行器用
  `subprocess.run(..., capture_output=True, timeout=STEP_TIMEOUT_S=5.0)` 跑 `wl-copy`，而 `wl-copy`
  会 **fork 一个常驻进程替它守剪贴板数据**，那个子进程**继承了 stderr 管道** ⇒ `capture_output`
  等不到管道 EOF ⇒ `TimeoutExpired`。读 `wl-clipboard` 2.2.1 的 `src/wl-copy.c`
  （`did_set_selection_callback`）：fork **之前**只换两个 fd ——
  `dup2(devnull, STDOUT_FILENO)`（源码注释原话 *so the stdout file descriptor isn't kept alive*）
  与 `dup2(devnull, STDIN_FILENO)`，**stderr 原样留着**；所以握着管道的是 stderr，而
  `capture_output=True` 恰好把 stderr 也接了过来。⇒ **会 fork 常驻进程的命令，永远不许用"等管道
  EOF"的方式判成败。**（同源两条：`--clear` 在同一个回调里提前 `exit(0)`、**根本不 fork**；父进程
  退 0 只代表"数据交给了留守者"，不代表合成器把选区判给了我们 —— 见下一条。）
- **修复（在工作树里，未提交）**：`run_step()` 分两类命令两种等法 ——
  * **守护步**（`Step.daemonizes=True`，**一切 `wl-copy` 调用**，含 `--clear` / `--primary` / `--type`）：
    stdout/stderr 重定向到状态目录里 **0600** 的临时文件，只等**父进程退出**（毫秒级），错误详情
    只读**字节数**，读完 `os.unlink` **瞬毁** —— 文件里可能贴着用户剪贴板，所以它绝不进 message /
    `voice.log` / 事件帧 / `status`（`os.unlink` 而非进焚化炉：那条红线保护的是"还要查证的现场"，
    这里是"按设计必须瞬毁的隐私件"）；
  * **普通步**（`ydotool` / `wl-paste`）：**维持** `capture_output=True` —— 它们的输出采集一个都
    不许丢，一刀切等于再造一个"读不到错误"的瞎子。
  另加**回读自证**：写完立刻 `wl-paste` 回读，rc=0 **且逐字节相同**才算写成功，否则报
  **`owner_rejected`**（不是笼统的 `step_failed`）且**不发粘贴**；回 idle 的状态行也跟着加了
  「上屏失败：`wl-copy` 没拿到剪贴板所有权」，**不许写成"上屏结束"**；`inject_done` 新增 `copy_ms=`
  打点（"修好之后到底几毫秒成功"要看得见，不许再拿"没报错"当证据，没跑到写入步记 -1）。
- **顺带把一条旧担心的假设实测排除了**：本机**没有** data-control 协议（45 个全局里 `data_control`
  命中 0，只有核心 `wl_data_device_manager` v3 + `zwp_primary_selection_device_manager_v1`），
  所以"无键盘焦点的进程设不了选区"此前只是**担心**；2026-10-19 11:07 实测排除 —— 从一个无键盘
  焦点、无 activation token 的 shell 里 `wl-copy --type image/png < 5.62MiB.png`，**父进程毫秒级
  返回、选区当场设成**（deskflow 日志 `sent clipboard size=5880052`）。`owner_rejected` 照样要报
  （那是运行期事实），但它不再是"必然发生"的预期。
- **判据**：新增 `tests/test_voice_daemonized_copy.py`（**16 条**：真子进程"父退、守护进程还握着
  管道活着"的三种管道组合、写自证全链隐私条、"成功 / 失败 / 超时"三条路径的临时文件都必须销毁）
  —— **改动前 14/16 红**。工作树稳定后重跑的实测数（**两种口径、各自跑法与结果**，2026-10-19 11:52
  本机，**含并发分身在写的截图线用例**）：
  * 默认口径：`python3 -m unittest discover -s tests` → **Ran 863 tests / OK (skipped=53)**，`rc=0`（38.6s）；
  * 无 gi 口径：`PYTHONPATH=tools/ci/nogi CAPSPLUS_NOGI_SUB=1 python3 -m unittest discover -s tests`
    → **Ran 863 tests / OK (skipped=89)**，`rc=0`（19.2s）。
  ⚠ 早先这里写过的「769 条 OK（skipped=48）」是 **11:15 那一刻**的快照，已被上面这组取代；
  条数会跟着并发分身的工作继续变 —— 任何一次都是有跑法的**实测**，但都不是"现状"，别拿它当第二份真相。
- **「先占后贴」还没接线（写清楚，别当成已做）**：`copy` 与 `paste` 在时间上本就可以分开 ——
  弹窗还活着、我们还有焦点时先把数据交给 `wl-copy`，销毁窗口、焦点归还之后再发 Ctrl+V。本轮只把
  **形状**备到位（每步可独立执行 / 自证是独立函数 / `InjectResult` 带 `copy_ms`·`stages`·`commands`），
  **故意不接线**（真机还没证明需要它，提前动时序等于把 `test_voice_focus_gate` 钉住的东西再拆一遍）。
  真要做的成本（下面每个数都是**量出来的改动面**，不是估的）：
  `voice_inject.inject()` 现在是 118 行一次跑完四段，要拆成"占 / 贴"两次可独立调用；
  `voice_worker.py` 三个函数（`_arm_inject` 49 行、`_inject_with_diagnosis` 16 行、
  `_inject_directly` 27 行）要改成"武装**之前**先在后台线程跑占、焦点交出的事实到了再跑贴"；
  直接钉在这条链上的既有用例 **23 条**（`test_voice_clipboard` 2 + `test_voice_focus_gate` 15 +
  `test_voice_daemonized_copy` 6）要跟着改口径，主线程名册 `MAIN_PATH_FUNCS`（13 个名字）与
  `tools/verify-voice-mainloop.sh` 要同步，最后真机再验一次。
  **行为代价**：剪贴板被我们占住的时间窗口从"粘贴后 300ms 起算"变成"弹窗还活着就开始占"，
  多占的正是 `finish→unmap` 那一段（真机 `inject_diag` 里能直接读出来，本轮**未测**）。
- **回滚**：本次改动只在 `capsplus/voice_inject.py` 与 `capsplus/voice_worker.py`（后者只加了
  `copy_ms=` 打点与 `owner_rejected` 那行"上屏失败"的措辞），`git checkout` 这两个文件即回；
  回滚后症状是"识别成功、报 `step_failed`、一个字都不上屏"（也就是这轮之前的状态）。

---

### 2026-10-20 事故：**只可以是短语音、识别率低** —— 三个限制值全是客户端自造的

**现象**：`Caps+M` 说长句，弹窗里只有几个字；说快一点/停顿一下就被截断。用户的判断是"几乎不可用"。
**根因**（量出来的，不是猜的）：

| 旧值 | 真凶 | 实测 |
| --- | --- | --- |
| `MAX_MS = 20000` | 单次录音 20 s 硬上限 | 同一段 59.5 s 音频：20 s 上限只拿到 **44 字**，放开到 60 s 拿到 **130 字**；服务端两次都 HTTP 200（0.97 s / 3.74 s）⇒ **服务端从来不是瓶颈** |
| `SILENCE_STOP_MS = 700` 固定 | 尾静音 700 ms 就判"说完了" | 句中 900 ms 的思考停顿：旧参数 **1230 ms 就断**，新参数成一整段 2400 ms |
| `TRIM_PAD_MS = 150`（前后同值） | 送识别前把句首掐掉 | 8 组前置静音打真服务：旧 150 ms **丢首字 6/8**，300 ms 只丢 1/8，400/500/800 不再改善 ⇒ **300 是拐点** |

**修复**：上限 60 s（到上限**明确提示**：弹窗顶栏 / `voice.log` / `status` 三处，不再静默截断）；
尾静音自适应 `min(2400, 1200 + 每说满 1 s +100 ms)`；句首/句尾 pad 拆成 300 / 250。
另修一个被这条连带暴露的 bug：会话看门狗 `MAX_SESSION_S` 90 s 装不下"60 s 录音 + 3.7 s 识别 + 人反应"，
提到 180 s（否则一次慢识别就把还在等人确认的会话判死）。

**新增诊断口**（为的是"别再靠用户当测试员"）：
· `capsplus voice meter [秒数]` —— 只读麦克风量电平（RMS/峰值/分位/过门帧/建议增益），
  不起录音线程、不写 socket、不弹窗、不送识别；
· 每次录音把 `voice_level rms=… peak=… voiced_rms=… gain=…` 写进 `voice.log` 与 `capsplus voice status`；
· `CAPSPLUS_VOICE_KEEP_LAST=1` → 状态目录留最近一次送识别的 WAV（0600、上限 5 份、只清自己的文件）。

**没有加自动增益**（量完才定的）：本机真人语音峰值已用掉量程的 84%，任何 ≥1.2× 都会削顶（削顶比声音小更毁识别）；
而底噪 19 × 8 = 152 会跨过 `GATE_FLOOR=150`，那时没说话也出字 ⇒ 只留手工 `CAPSPLUS_VOICE_GAIN`（夹 1~4×）。

**判据**：`tests/test_voice_vad.py` 扩到 45 条（新增"句中停顿不切断"/"60 s 上限"/"pad 保句首"三组）、
新增 `tests/test_voice_levels.py` 26 条、`test_voice_asr.py` +5、`test_voice_proto.py` +7。
五组反例具型**逐条真红过**（把旧参数塞回去 → 对应判据红，日志在 `~/.dsh/work/20261020-voice-fix/red/`，
扰动后 `sha256` 自证还原）。

**诚实的未做**：60 s 的 A/B 用的是"同一句参考音频重复 10 次"的替身，不是真人 60 秒长句；
真机"说一整句 → 弹窗 → 上屏"这一条仍要用户自己按一次（会抢焦点，不越这道线）。

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
