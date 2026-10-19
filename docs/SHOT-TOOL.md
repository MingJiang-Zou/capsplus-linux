# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）。
# 这是文档，不是代码；写在这里的每条都带"哪天验的 + 怎么验的"。

# capsplus 截图工具（替代 Snipaste）—— 架构与实测事实

面向对象：接手这条链路的人。明江要的四件事：① 区域截图（**绝不闪**）② 贴图（真置顶）
③ 图像标注 ④ 截图翻译。第一批（本文件覆盖）：①（含选区遮罩）+ ③ + ② —— 已交付；
④ 与热键绑定/设置页集成属第二批（不在本文件范围内）。

四条命令（`bin/capsplus-shot`）：

| 命令 | 干什么 |
| --- | --- |
| `--capture X Y W H [--out FILE]` | 直接抓一块矩形（逻辑坐标） |
| `--region [--out FILE] [--no-annotate]` | 拖选区（遮罩不闪）→ 抓帧 → 开标注窗 |
| `--annotate PNG` | 对已有的图开标注窗（复制/存盘/钉屏都在窗里） |
| `--pin PNG [--at X Y]` | 把一张图钉在屏幕上（真置顶） |
| `--journal` | 打印 shell 那侧的抓帧/贴图痕迹（成功与失败都有一行） |

## 一、为什么抓帧必须走 GNOME 扩展（2026-10-19 实测）

| 做法 | 实测结果 |
| --- | --- |
| 外部 `gdbus call … org.gnome.Shell.Screenshot.ScreenshotArea 0 0 320 200 false /tmp/x.png` | `AccessDenied: ScreenshotArea is not allowed` —— 不给外部调 |
| shell 进程内 `Shell.Screenshot`（`/usr/lib/gnome-shell/Shell-18.typelib`） | 可用，且**不闪** |
| shell 自己的截图 UI（`ui/screenshot.js` 的 `Flashspot`） | 闪屏动画在这里：`opacity 255 → 0`、`duration: 500ms`。它是"UI 层"的东西，**不在 C 抓帧里** |

结论：capsplus 不调任何 D-Bus 截图接口，只发信号；抓帧在扩展里做，**完全不碰** `Flashspot` 那条动画
⇒ 明江要的"绝不闪"是这么来的（他换掉 Snipaste 就是因为闪）。

### 抓帧 API 的准确签名（从本机 typelib dump 出来，别照网上老写法抄）

```
Shell.Screenshot.screenshot_area(x:int32, y:int32, w:int32, h:int32,
                                 stream:Gio.OutputStream, callback, user_data) -> void
Shell.Screenshot.screenshot_area_finish(result) -> (ok:boolean, area:Rectangle)
```

dump 方法（可复现）：用 gjs 的 `GIRepository` 把参数名与类型全列出来 —— 关键是 `GI_TYPELIB_PATH`
要同时给 `mutter-18`（`Shell` 依赖 `Meta`），`LD_LIBRARY_PATH` 要给 `/usr/lib/gnome-shell`
（`libshell-18.so`）：

    LD_LIBRARY_PATH=/usr/lib/gnome-shell \
    GI_TYPELIB_PATH=/usr/lib/gnome-shell:/usr/lib/x86_64-linux-gnu/mutter-18:/usr/lib/x86_64-linux-gnu/girepository-1.0 \
    gjs -m -e "…repo.find_by_name('Shell','Screenshot') 逐个 get_method(i) 打 get_name/get_n_args…"

（一次性脚本没留在仓里；同样的信息也能从 `gjs` 里打
`Object.getOwnPropertyNames(Shell.Screenshot.prototype)` 与 `.length` 拿到实参个数。）

两点必须注意：

1. **坐标是逻辑坐标，交 C 之前要乘 stage 的 scale factor** —— shell 自己的 D-Bus 截图接口就是
   这么换算的（`ui/screenshot.js` 的 `_scaleArea`）。本机三块屏里有一块 `scale=2`（`VILTROX_4K30`，
   逻辑 `(3840,540) 960×540`），少这一步就抓偏。
2. **扩展里 `GLib.timeout_add` 是原始 C 绑定**：`gjs 1.88`，2 实参直接抛
   `At least 3 arguments required`；要写 `(priority, interval, cb)` 三实参。
   （同批实测：`Gio.DBus.session.export_object_path` / `register_object` / `return_value` 不可用，
   所以**不能导出 D-Bus 对象、不能回值** —— 见扩展头注。）

## 二、链路形状

```
capsplus-shot --region | --capture X Y W H --out FILE | --pin FILE
  └ emit_signal(5 实参)  接口/路径/成员：io.github.capsplus.RaiseBrowser  CaptureArea  "(iiiis)"
      └ raise-browser 扩展 signal_subscribe → deepUnpack() → 乘 scale → Shell.Screenshot.screenshot_area
          └ 先写 FILE.part、成功后 rename 成 FILE（**FILE 一出现就是完整 PNG**）
              └ capsplus 等文件出现（超时 3s）→ 校验 PNG 魔数 + IEND
```

没有回复通道 ⇒ 结果靠两处：**产物文件**（成没成）与 **journal 一行**
（`[raise-browser] shot result=…`，成功/失败/被拒都会写，`capsplus-shot --journal` 直接看）。

### 为什么"成功"只认产物

`signal_subscribe` 只订阅执行、没有回值（本机 GJS 造不出 vtable）；扩展在 shell 里 `emit_signal`
也不上总线。所以"请求-响应"这条路在本机是不通的，别去试了 —— 形态由实测钉死：
**单向下发 + 结果落 journal + 产物落文件**。

## 三、改了扩展之后怎么让它生效（**这条最容易白折腾**）

| 动作 | 实测结果（2026-10-19） |
| --- | --- |
| `gnome-extensions disable` + `enable`（磁盘上已是新代码） | **不会重新 import**：journal 里依旧是旧代码的 `ready default=…`，新代码的 `ready capture=…` 一次都没出现 |
| 注销重登 | shell 重新扫描扩展目录并重新加载 ⇒ 新代码生效 |

复现（别用 `journalctl | grep raise-browser` 裸查：你**自己命令行里的字符串**也会被算进去，
只认 gnome-shell 进程写的行）：

    DST=~/.local/share/gnome-shell/extensions/raise-browser@capsplus.example
    cp extensions/raise-browser@capsplus.example/{metadata.json,extension.js} "$DST/"
    TS=$(date -Is); gnome-extensions disable raise-browser@capsplus.example; sleep 1
    gnome-extensions enable raise-browser@capsplus.example; sleep 2
    journalctl --user --since "$TS" --no-pager | grep "gnome-shell\[" | grep raise-browser

判据：出现 `[raise-browser] ready capture=on pin=on promisified=true …` ⇒ 新代码在跑；
只有 `ready default=…` ⇒ 还是旧代码，**要注销重登**。

**装扩展的脚本目前有个坑（2026-10-19 实测，未修 —— 不在本次改动范围内）**：
`tools/install-extension.sh` 第 17 行 `[ "$1" = "--quiet" ] && QUIET=1` 在 `set -eu` 下无参调用会
`parameter not set` 直接退出（要传 `--quiet` 反而只报错不复制）。手动部署就照上面那两行 `cp`。

## 三·五、第 2 步：选区遮罩（capsplus 侧，不需要重登）

`capsplus/shot_overlay.py`。一屏一窗（`fullscreen_on_monitor(screen, **序号**)` —— GTK3 要的是
监控器**序号**，传 Gdk.Monitor 对象会报 `Must be number, not X11Monitor`；GTK4 才是传对象）。

- 压暗用"**四条带**"而不是"整屏压暗 + 挖洞"：像素上等价，判据一眼能算出来。
- `paint()` 只吃一个 cairo 上下文，判据把它渲染到 `ImageSurface` 上**逐像素**断言
  （选区内全透明、选区外 α=0.35、边框亮青、读数是宽×高）—— 不需要显示服务器。
- **绝不闪**：全文件没有 `ease` / `set_opacity` / `transition` / `Clutter` 动画，也没有逐帧定时器；
  唯一的定时器是"藏窗后的 settle"。判据 `tests/test_shot_overlay.py::TestNoAnimation` 扫这条。
- 键盘只在窗口拿到焦点时到，所以**右键确认那条路永远有效**（提示行两种都写了）。不 grab 输入。
- 确认 → **藏所有遮罩窗 → 等 settle（默认 150ms）→ 把全局逻辑坐标交给调用方**。
  2026-10-19 xvfb 真事件判据抓出过一个真 bug：第一版确认时立刻退主循环，settle 的 timeout
  根本没机会跑，"等一帧"从来没发生过 ⇒ 抓帧可能把遮罩自己拍进去。现在 `_tick` 到点才交坐标并退循环。

## 三·六、第 3 步：标注窗（capsplus 侧）

`capsplus/shot_annotate.py`：矩形 / 箭头 / 自由文字 / 马赛克 + 撤销重做 + 复制 / 存盘 / 钉屏。

- 底图由 **cairo 自己加载 PNG**（`create_from_png`）。不经 numpy/PIL 胶水：cairo 的 ARGB32 是预乘
  BGRA，自己拼字节极易把通道顺序搞错。
- **马赛克** = 按块降采样到小 surface，再用 `FILTER_NEAREST` 放大回去。两个坑：
  ① `SurfacePattern` 默认矩阵是恒等，光 `set_source()` 不会缩放 —— 小图会被 1:1 画在原点，
  等于没画（像素判据第一次就抓到了）；必须 `translate(x,y)` + `scale(w/bw,h/bh)`。
- **自由文字** = 点一下弹一个 `Gtk.Popover` + `Gtk.Entry`，回车落地。**不是**画布内联编辑光标
  ——那是另一个量级的活，第一批不做，这里如实标出来。
- 撤销/重做是纯数据（`ShapeStack`），新画一笔清掉重做分支（与所有画图软件一致）。
- 窗口显示的是**按屏幕缩放**后的图，图形坐标一律存图像坐标（`to_image_coords`），否则标注会整体偏。
- 复制走 `wl-copy --type image/png`（PNG 字节走 stdin）；没装 wl-copy 就**如实报错**，不假装成功。
- 存盘默认 `~/Pictures/Screenshots/shot-<时间戳>.png`（`CAPSPLUS_SHOT_DIR` 可覆盖），
  同名不覆盖（加 `-1`）。

## 三·七、第 4 步：贴图 / 真置顶（扩展 + capsplus）

链路：`capsplus/shot_pin.py` 用 PIL 把 PNG 解成 **RGBA 原图**（`$XDG_RUNTIME_DIR/capsplus-pin/`，
tmpfs，与 pin 的寿命对齐；不在他的相册目录里留 `.ack`）→ 发 `PinImage(rawPath, w, h, x, y, ackPath)`
→ 扩展建 actor。

扩展侧（`extension.js` 的 `pinImage`）：

- **真置顶 = `Main.layoutManager.addTopChrome(actor)`**。这是 shell 自己的 `layout.js` 写死的语义：
  `addChrome` 会把 actor 放在 `top_window_group` **下面**（那不算置顶），
  "above all windows, including popups" 只有 `addTopChrome`（或 `uiGroup.add_child`）。
  **注意**：uiGroup 里本来就包含 `global.window_group`，所以挂在 uiGroup 上的东西天然在所有窗口之上。
- 位图用 `St.ImageContent.new_with_preferred_size(w,h)` + `set_bytes(coglContext, GLib.Bytes,
  Cogl.PixelFormat.RGBA_8888, w, h, w*4)`，`coglContext = global.stage.context.get_backend().get_cogl_context()`
  （与 shell 自己的 `ui/qrCode.js`、`ui/screenshot.js` 同形）。**PNG 不在 JS 侧解码**。
- 交互：左键拖动、滚轮缩放（0.2×–4×）、双击或点右上角 **×** 关闭、可多张并存。
- **不抢焦点**：× 按钮 `can_focus: false`，全文件不调 `grab_key_focus`。
- 清理：`removeChrome` + `destroy`，扩展 `disable()` 时 `destroyAllPins()` 全清（不留泄漏的 actor）。
- 位置坐标与抓帧同一口径：**逻辑坐标 × stage scale factor**（`stageScale()`），日志里带 `scale=`。

**"置顶是否真生效、怎么知道的"**：机制上的依据是上面这条 API 语义（shell 自己就用它把
screenshotUIGroup / keyboardBox 放到所有窗口之上，`layout.js` 的注释原文写着），
外加判据 `test_pin_uses_add_top_chrome_for_real_always_on_top` 钉住"不许退化回 addChrome"。
**但它仍然需要明江真机看一次**：我们的 actor 是不是真的压在他的应用窗口（含全屏）之上、
拖动时有没有抢走键盘焦点 —— 这两件事只有在真会话里才看得见，判据替不了。

## 四、已知限制（本工具自己的，不写进 KNOWN-LIMITS.md）

1. **多屏**：遮罩按"每块屏一扇全屏窗"实现，跨屏的一次性拖拽选择不支持（各屏各自可以选）。
   本机三块屏里第三块是 `scale=2` 的 VILTROX（逻辑 `(3840,540) 960×540`）：坐标换算已按 shell
   自己的口径乘 stage scale，但**跨屏拼一张图**没做。
2. **遮罩藏起来到抓帧之间有一个 `settle`（默认 150ms）**：合成器要把我们那扇窗的移除提交一帧，
   这期间若屏幕内容恰好变了，抓到的是新内容（不是脏图，只是"晚一帧"）。
3. **贴图置顶只在 GNOME 会话里成立**（本质是 shell 图层 actor），换桌面环境要另做。
4. **抓帧与贴图都依赖扩展处于 ACTIVE**：扩展被禁用/加载失败时，命令会 3s 超时退出 1，
   超时话术里给了三条自查命令。
5. **标注工具没有"颜色/线宽"选项**，默认红色 3px；文字不能画布内联编辑（弹输入框）。
6. **贴图不吃"点击穿透"**：落在贴图上的点击会被它吃掉（Snipaste 有穿透模式）。
   要做穿透得再处理 input region，属第二批候选。
7. **贴图位置默认主屏中间偏上**；从标注窗钉的时候用默认位置，钉完自己拖。
8. **`screenshot_area` 的 scale 语义靠"照抄 shell"**：本机没能独立验到 stage scale ≠ 1 的场景
   （三块屏里只有 VILTROX 是 2×，而 stage scale 取决于 mutter 的合成方式）。日志里带 `scale=`，
   一旦真机上发现抓偏/钉偏，先看这个数。

## 五、怎么再验一遍

    # 1) 纯逻辑判据（不需要真屏、不需要扩展、无头 CI 也能跑）
    python3 -m unittest tests.test_shot_frame tests.test_shot_extension -v

    # 2) 信号链路的真机自证（不需要注销重登：用草稿接口名，不与真扩展撞车）
    sh ~/.dsh/scripts/capsplus-shot/fixtures/ipc-selftest/run.sh ~/project/private/capsplus-linux
    #    一个 gjs 进程订阅（照抄扩展的参数解包与 .part→rename），一个 python 进程发信号，
    #    断言产物出现、IEND 完整、journal 行能被 shot_frame.parse_shot_log 解析。

    # 3) 真屏抓一张（需要扩展已加载；跑完把产物挪进焚化炉，别留在仓里）
    bin/capsplus-shot --capture 0 0 640 360 --out /tmp/x.png && file /tmp/x.png

## 六、判据在哪

| 判据文件 | 钉什么 |
| --- | --- |
| `tests/test_shot_frame.py` | 信号实参 / 等产物 / 超时话术 / journal 解析 / 选区几何 / 落盘命名 |
| `tests/test_shot_extension.py` | 扩展形状：`screenshot_area` 实参个数、7 实参订阅、`.part`→rename、禁止 flash 动画、`addTopChrome`、`can_focus: false`、ack 受 `if (ackPath)` 管、`timeout_add` 三实参、成员名与 Python 常量逐字一致 |
| `tests/test_shot_overlay.py` | 事件口径 / 几何 / 遮罩**像素**（选区内全透明、外压暗）/ 无动画 / 真事件走一遍（xvfb） |
| `tests/test_shot_annotate.py` | 撤销重做 / 坐标换算 / 四种图元的像素结果（含马赛克"块内一致"）/ 存盘 / 剪贴板命令 |
| `tests/test_shot_pin.py` | PNG→RGBA **通道顺序** / ack 落点 / 等 ack 的编排与话术 |
| `tests/test_shot_cli.py` | 四条命令的参数与**编排**（取消不抓、失败不开窗、退出码、钉的是渲染结果） |

全部"删掉实现就红"（每份都有反例具型或已被改前/改后对照验过）。跑 GUI 那两组要虚拟显示：

    xvfb-run -a --server-args="-screen 0 1280x800x24" \
      env -u WAYLAND_DISPLAY GDK_BACKEND=x11 CAPSPLUS_GUI=1 \
      python3 -m unittest tests.test_shot_overlay tests.test_shot_annotate
信号链路的真机自证夹具在 `~/.dsh/scripts/capsplus-shot/fixtures/ipc-selftest/`（`run.sh` 一条命令跑完）。