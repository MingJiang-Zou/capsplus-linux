# 更新记录

口径先说清楚：**本仓尚无任何 release tag**，也没有发过任何版本包。下面的 `0.1.0` 是"第一份
公开版"的叙事占位号，与 `capsplus/__init__.py` 的 `__version__` 一致（托盘「关于」框里就显示它）。
真打出第一个 tag 之前，这个文件就是唯一的版本史，别在别处再写一份。

---

## 0.1.0 —— 2026-10-15 起（未打标）

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
- 默认引擎模板加淘宝 / 闲鱼（两个搜索端点实测 HTTP 200，中文关键词走 `{q}` URL 编码）。

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
