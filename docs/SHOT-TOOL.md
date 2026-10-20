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
- **`.pinned.png` 是干什么的**（明江 2026-10-19 问过，写清楚免得成谜）：点"钉到屏幕上"时，
  标注窗先把**渲染结果**写一份 `<同名>.pinned.png` 到同一目录，再把这个文件交给扩展去钉
  （扩展要的是 PNG 字节，贴图本体在 shell 那边是裸 RGBA）。所以它是**有意的**：它是"你钉的那张"
  的留存，和普通 `shot-*.png` 的区别只是"按过钉屏"。想不想要都行 —— 不想要就把 `on_pin` 里那
  两行去掉（判据会跟着改），留下它的理由是"钉了之后还能找回原图"。
  另注：贴图本体的 RGBA 落在 `$XDG_RUNTIME_DIR/capsplus-pin/`（tmpfs），**不进相册目录**。

## 三·六·四、翻译/OCR 的后端与凭据（2026-10-19 定）

- **默认档 = `local`**（明江："翻译最好用免费的"）。本机 `qwen3.8-27b` @ `127.0.0.1:12087`，
  离线、不限次；**绝不因为云 key 失效而失败**。`auto`/`cloud` 保留，但必须**主动切**才走
  （`cloud` 那条是按 token 计费的 LLM API，不是免费翻译服务）。本机没起时的文案只谈"怎么起本机模型"，
  不再有"静默切云"这回事。
- **云凭据三级**（高→低）：`CAPSPLUS_CLOUD_KEY` 环境变量 → `~/.bailian/config.json` 的 `token-plan`
  段 → **`~/.dsh/.credentials.yaml` 的 `refs.AL_TOKEN_PLAN_API_KEY`**（DSH 自己那把，实测 200）。
  纪律：只读、**绝不打印**、绝不改写任何一个凭据文件；`source` 字段只记"哪来的"不带值；
  401 的文案是"**凭据失效 → 按面板『切到本地』，或换一把 key**"，切换后下一次调用立刻生效
  （不给云配置做进程级缓存）。
- **401 要真的换下一把（2026-10-20，实测）**：`~/.bailian/config.json` 里那把 key **实测 401**
  （`Invalid API-key provided`），而同一时刻 `~/.dsh/.credentials.yaml` 那把 **HTTP 200 正常出字**
  ⇒ "有凭据"不等于"能用"。所以发请求时按 `cloud_candidates()` 的清单**逐把试**：某一把 401 就换下一把，
  成功的 `note` 里写明"跳过了哪几把"；全坏才报错并列出试过哪些来源（文案里只有标签、没有值）。
  显式传 `cloud=`（判据/调用方指定）时只试那一把。
- **推理模型必须关思考（2026-10-20，明江真机"翻译无效"的病根）**：本机 12087 换成会思考的
  `qwen3.8-27b` 之后，`max_tokens=32` 全花在 `message.reasoning_content` 里、`content` 是**空串**、
  `finish_reason=length` ⇒ `parse_completion` 读 `.content`，翻译结果**恒为空**。
  实测唯一有效的开关是请求体里的 **`chat_template_kwargs: {"enable_thinking": false}`**：
  本机 `reasoning_content` 134 字 → 空、22 token 出完整译文；云端 `deepseek-v4.1-flash`
  `reasoning_tokens` 149→0、`completion_tokens` 159→9、2.73 s→**0.83 s**（**两个后端都认**）。
  ⚠ 提示词里写 `/no_think` **不管用**（实测照样 32 token 全思考、`content` 仍为空）——别照抄那条。
  对面若是个不认这个字段的实现（400）⇒ **去掉它重试一次**并在 `note` 里写出来，不因一个可选字段废掉翻译。
  另外两件事一起定了：`max_tokens` 显式给（文本 1024 / 直读图 4096），空 `content` **必须带原因**
  （认出"思考把 token 吃光了"这种形状 + 给可复制的修法），**超时 60s → 120s**（服务端自己算一次翻译
  0.39 s，可被同机另一个会话排到 **63.7 s** 墙钟 ⇒ 60s 会把"在排队"误报成"端点死了"；
  `CAPSPLUS_TRANSLATE_TIMEOUT` 可覆盖，超时与"连不上"在文案里分开说）。
- `--translate-status` **做一次真往返**（1~8 token）并按结果分类：**活着且能出字 / 活着但 content 空
  （推理模型）/ 超时 / 端点不在**；云端逐把凭据各试一次。原来只说"端点活着"，等于把上面那个故障说成正常。
- 判据：三级优先级、只读（静态扫"凭据函数里没有写操作"）、**假凭据 fixture 断言任何文案里都不含它的值**、
  401 文案可操作、默认档是 `local`（反例改回 `auto` → 红）、**请求体里真有那个关思考字段**（本机/云/直读图
  三处各一条，反例去掉 → 红）、空 content 的文案里必须出现 `reasoning_content` 与 `enable_thinking`、
  400 退化路径（先带后不带，序列 `[True, False]`）、超时文案说"排队"而不是"没应答"、
  四种探活状态各一条、**第一把 401 时第二把真被用上**（序列 `[KEY-FROM-JSON, KEY-FROM-YAML]`）。

## 三·六·六、"提取颜色"（2026-10-20 新功能）

- **遮罩阶段**：指针旁画一小块读数（色样 + `#RRGGBB`），按 **`C`** 把该色值复制走
  （`wl-copy --type text/plain`，与复制图片共用同一条守护步 runner）。
  颜色从**冻结帧**取、按"逻辑坐标 × 帧/窗口 缩放"换算 —— 本机帧可能是 1.3333×，不换算就取错像素。
  跨屏/贴边时按**当前指针那块屏**取（读数越界自动翻面）。
  ⚠ `C` 是遮罩窗**唯一**放行的字母键（原来一个字母都不占）—— 多一个都算偷他的输入。
- **标注窗阶段**：工具条多了**吸管**（图标自绘，与其它 19 个同一支笔触；橡皮旁边那一组）。
  点画布任意像素 ⇒ 设为当前绘制颜色，状态栏显示 `取色 #RRGGBB`，接着画的图元就用它。
  取色**不产生图形、不进撤销栈**；点到截图外则什么都不做（不静默改色）。
- 判据：喂一张"**每一列颜色都不同**"的假冻结图，逐像素断言 `#RRGGBB` **精确相等**；
  反例（取样坐标偏 1px / 吸管只显示 hex 不设色）必红。

## 三·六·七、性能（2026-10-20 实测，明江："框选特别卡"）

| 层 | 修前 | 修后 |
| --- | --- | --- |
| 整幅重画（2560×1440 源每帧缩到 1920×1080） | **p50 32.1 / p95 33.0 ms** | **p50 0.76 / p95 0.82 ms**（预缩放同尺寸 blit，~42×） |
| 指针移动（键盘注入连移 30 次） | 画面**逐像素 0.0000% 变化**（见下） | **每次移动都真重画**，整幅 p50 **0.87 ms** |
| 文字读数测量 | p50 ≈ 0 ms | 无需缓存 |

修法：① `_scaled_frame` **预缩放缓存**（每块屏一份与窗口同尺寸的 surface，换帧/换尺寸才重做；
**2026-10-20 改成每块屏各留一份** —— 只留最新一份时，拖选区要重画三块屏，每帧都要把另外两块重新缩
一遍，3×36 ms/帧）；② **指针一动就 `GtkWidget.queue_draw()` 整幅重画**（预缩放之后整幅只要 0.87 ms，
60Hz 一帧 16.7 ms 里占 5%，不需要省）；③ `freeze()` 切完就 `surface.finish()` 放掉联合大图
（35 MB/张，切片是独立拷贝，安全）。

⚠ **脏矩形那条路已作废（2026-10-20 真机事故，别再改回去）**：上一版对 GdkWindow 调**子区域**
`invalidate_rect(dirty, False)`，而 GTK3 对"直接打在 GdkWindow 上的子区域失效"**不发 `draw`**
（隔离实测：40 次子区域失效 → draw **1** 次；`queue_draw_area()`／`queue_draw()`／整幅
`invalidate_rect(None,…)` → **40/40**）。真机探针：连移 30 次指针 ⇒ MOTION **690** / REDRAW **689** /
draw **30**（都在起止两头），两次抓屏**逐像素 0.0000% 变化** —— 现象就是明江说的
"十字线不跟鼠标走、要按着拖才跟"（拖动走的是整幅那条路，所以反而跟手）。
判据 `TestMotionMovesTheCrosshairForReal`（真窗口 + 真像素：动**两次**，断言 draw 次数增加、
新位置准线画出来、旧位置擦干净）；反例：换回子区域 `invalidate_rect` → 红在第一条断言。

## 三·六·五、hover 工具条与"遮罩压在贴图之上"（2026-10-19 明江定）

**① hover 工具条点不到（真 bug）**：工具条原本画在贴图**外面**（`pin.height + 6`），而贴图的
`leave-event` 立刻 `toolbar.hide()` ⇒ 鼠标一离开贴图就把工具条收了，"闪现但抓不住"。
病根是**离开就 hide**，解药是 **250ms 宽限**（`HOVER_GRACE_MS` + `hoverPlan` 状态机，
进工具条/按钮就取消）；位置只是配合它的。
判据把这两个函数从 `extension.js` 里抠出来在 **node 里真跑**（不是"函数被调用了"这种假绿）：
断言"进入贴图 → 离开贴图 → 进工具条 → 点复制"整条序列里工具条**一直可见**、点击**命中到按钮**、
`×` 一击即中。反例：宽限期改 0 → 红。

**工具条的位置（2026-10-20 明江改判，与上面那条并列看）**：中间一版把它搬进贴图**内部**右上角，
明江 2026-10-20 说"摆里面会挡住贴图部分内容" ⇒ **搬回贴图外面**：默认在下方，下面放不下翻上方，
贴屏幕边再左右拉回 stage 内；跨那 `TOOLBAR_GAP` 空隙由上面的 250ms 宽限接住。
判据是坐标级的 **`overlaps === false`（与贴图矩形零重叠）** + 默认在下方 + 到底/到边会翻面收边；
反例：摆回贴图内部 → 红。
（与 2026-10-19 那版相比只有**位置**这一项变了；"离开就 hide 必须靠宽限救"从未变过。）

**② 遮罩压在贴图之上（明江的口径，不做开关）**：他的原话是「这个只需要截图框选的图层比贴图还要高
就可以了。我感觉，你的推荐反而复杂了」。物理事实：贴图在 **shell 图层**（`addTopChrome`，当初为
"真置顶"特意选的）、遮罩是 **GTK 窗口**，shell 图层恒在窗口之上 ⇒ "把 GTK 遮罩抬上去"做不到。
唯一等价做法：**抓帧前把所有贴图真的隐藏 → 抓完恢复**（`hidePinsForCapture` /
`restorePinsAfterCapture` / `pinVisibilityPlan`）。这样 ①压暗覆盖到原贴图位置；②**新截图不会把
旧贴图拍进去**。隐藏必须是真 `hide()`（**不是** `set_opacity(0)`：冻结是合成后抓的，透明的照样拍得到），
恢复只动"当时可见的那些"（不动贴图自身状态），四条出口（成功/抓帧失败/关流失败/改名失败/开流失败）
共用一个恢复漏斗，另有 30 秒**超时兜底**（回调不来也不会永久隐身），扩展停用走 `restoreAllPins()`。
**副作用（明确写下来）**：想拍贴图本身时目前**拍不到**——先关掉贴图再截。

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
| `tests/test_shot_draw_smoke.py` | **绘制面**：真 cairo 上下文逐个调全部绘制函数（不存在的方法/参数不兼容一次拦住）/ 取色读数**像素**（色样==取样色、`#RRGGBB` 有墨）/ 真 draw 处理器走到读数 / 全仓 `cr.*` 与 gi 引用逐个 `hasattr` 真对象 |
| `tests/test_shot_annotate.py` | 撤销重做 / 坐标换算 / 四种图元的像素结果（含马赛克"块内一致"）/ 存盘 / 剪贴板命令 |
| `tests/test_shot_pin.py` | PNG→RGBA **通道顺序** / ack 落点 / 等 ack 的编排与话术 |
| `tests/test_shot_cli.py` | 四条命令的参数与**编排**（取消不抓、失败不开窗、退出码、钉的是渲染结果） |

全部"删掉实现就红"（每份都有反例具型或已被改前/改后对照验过）。跑 GUI 那两组要虚拟显示：

    xvfb-run -a --server-args="-screen 0 1280x800x24" \
      env -u WAYLAND_DISPLAY GDK_BACKEND=x11 CAPSPLUS_GUI=1 \
      python3 -m unittest tests.test_shot_overlay tests.test_shot_annotate
信号链路的真机自证夹具在 `~/.dsh/scripts/capsplus-shot/fixtures/ipc-selftest/`（`run.sh` 一条命令跑完）。

## 七、第二批（2026-10-19）：冻结帧遮罩 / 贴图删除路径 / F2-F4 / OCR / 翻译

### 7.1 P0：遮罩改成「**先冻结、再选**」
明江真机反馈「框选的时候全黑屏是盲选」——旧版靠"全屏半透明窗 + 压暗 + 指望合成器透出桌面"，
**Wayland 下拿不到 ARGB 表面时那层"透明"就是全黑**，而 xvfb（X11 有 ARGB 视觉）永远复现不了，
当时那批像素判据全绿也拦不住。现在的链路（**顺序不能改**）：

    freeze（抓一张联合矩形，不可见）→ 遮罩（每屏一窗，背景 = 该屏冻结图，**不透明**）
      → 从冻结图裁剪（跨屏就每屏各裁一块再拼）→ 标注窗

* 新模块 `capsplus/shot_frames.py`：`freeze()` / `slice_frames()` / `crop_from_frames()` / `snap_rect()`；
  缩放按"抓回来的图多大"反推（本机实测 `scale=1`），**不硬编码**。
* 选区走**全局逻辑坐标** ⇒ 拖出单屏、**跨屏**都成立；`Ctrl+A` = 整个联合矩形，双击 = 指针那块屏，
  松手贴边吸附 8px。
* **不再二次抓帧** ⇒ 旧版那套"藏窗 → settle → 再抓一帧"整段删掉（它是为"躲自己的遮罩"存在的）。
* shell 只允许**一个**截图操作在跑（`Only one screenshot operation at a time is permitted`，实测），
  `freeze()` 遇到这个标记会**重试**（用户连按属正常）。
* 冻结失败 ⇒ **不开遮罩**、退出码非零：宁可说"这次没成"，也不给全黑盲选的窗。

### 7.2 P0：贴图删得掉（样式表 + 五条路径 + 模态 Esc）
`extensions/raise-browser@capsplus.example/stylesheet.css` **以前根本不存在**，而 extension.js 一直在用
`capsplus-pin` / `capsplus-pin-close` / `capsplus-pin-box` ⇒ 那些 class 一个都不生效，`×` 是裸按钮
（没尺寸、没背景、没 hover），这就是「删都删不掉」的直接原因。现在：

* 样式表定义：贴图边框/圆角/投影、工具按钮（尺寸 + hover + active）、焦点态 `.capsplus-pin-active`；
* **五条关闭路径**（全部收敛到 `destroyPin`）：hover 工具条（复制 / 存盘 / ×）、右键菜单（关闭 /
  复制 / 存盘 / 透明度± / 清空全部）、双击、**模态 Esc**、`Caps+Esc` 全局兜底
  （新信号 `ClearPins` + 新子命令 `capsplus-shot --clear-pins`，**没有贴图时也安全**）；
* 模态那段是从 shell 自己的 JS 里读出来的（`gresource extract /usr/lib/gnome-shell/libshell-18.so
  /org/gnome/shell/ui/main.js`）：`Main.pushModal(actor)` 返回 `Clutter.Grab`、`Main.popModal(grab)`
  收那个 grab、actor 销毁时自动 pop、`set_key_focus` 由 pushModal 自己做（与 `can_focus` 无关）；
  它抓**键盘+鼠标** ⇒ 点到窗外要**主动交还焦点**（代价：那一下点击被吞，注释里写明了）；
* `tools/install-extension.sh` 补上复制 `stylesheet.css`（扩展目录是**复制**不是软链，
  安装脚本漏了它 ⇒ 仓里改得再对，shell 那边还是裸控件）。
* **契约变化**：`PinImage` 从 `(siiiis)` → `(ssiiiis)`（多一个 PNG 路径：工具条的"复制/存盘"要 PNG 字节，
  用法抄 shell 自己的 `screenshot.js` 的 `St.Clipboard.set_content(…,'image/png',bytes)`）。

### 7.3 F2/F3/F4 + 结果面板 + 标注增强
* 窗内键位**只定义在一处**（`key_command()`）：F2 翻译 / F3 贴图 / F4 OCR；与 Esc / Ctrl+C / Ctrl+S /
  Ctrl+Z(Y) 不冲突；**回车留给第一批的"确认选区"语义**（不抢）。失焦时底部提示条换成
  「窗口没有焦点：F2/F3/F4 现在**不会**生效……点一下窗口再按」。
* 面板（`capsplus/shot_panel.py`）：后端**一眼看清**（本地/云）、原文/译文两栏可切、复制、
  「送去翻译」**只认 OCR 原文**（防越翻越走样）、出错给可操作下一步。
* 标注增强：荧光笔（强制半透明 α=0.35）、序号圆标（自动 1,2,3…）、颜色 5 档 + 线宽 3 档。
* F2/F4 **不冻界面**：后台线程 + **信箱**（主循环 50ms 轮询）。⚠ 别改回从 worker 里调
  `GLib.idle_add` —— PyGObject 下那样会**把堆搞坏**（判据连跑三轮崩两次，实测见 7.5）。

### 7.4 OCR / 翻译的两条后端（实测数字见 7.5）
* OCR：`local`（RapidOCR + onnxruntime 跑在**独立 venv**，主进程用子进程调 `shot_ocr_local.py`）/
  `cloud`（百度 `accurate_basic`，凭据读 `~/.dsh/skills/ocr/.env`）。`CAPSPLUS_OCR=auto|local|cloud`，
  `auto` = **本地优先**（离线不花钱），本地没装才上云；两边都没有 ⇒ 明确说清"装什么/缺哪个凭据"。
* 翻译：`local` = 本机 `http://127.0.0.1:12087/v1`（`qwen3.8-27b`，**只吃文本**，实测
  `modalities.vision=false`）/ `cloud` = 阿里 token-plan 的 `deepseek-v4.1-flash`（**多模态，可直读图**）。
  云端的 base_url 取自 `~/.bailian/config.json`，**必须补 `/compatible-mode/v1`**（站点根直接拼
  `/chat/completions` 会 404，实测）。本机偏好时"没有 OCR 原文"会提示先按 F4；切到云之后 F2
  可以**直接读图**（跳过 OCR）。
* 自查命令：`capsplus-shot --ocr-status` / `--translate-status`。

### 7.5 第二批的实测数字（真机，2026-10-19）
| 项 | 数字 | 怎么来的 |
| --- | --- | --- |
| 云端 OCR（百度 accurate_basic） | **391 ms**（640×360、3 行、含取 token） | 自己画的测试图，`shot_ocr.ocr_cloud()` |
| 本机翻译（qwen3.8-27b） | **1038 ms**（两行文本） | `shot_translate.translate_local()` |
| 抓帧（冻结那一下） | 4–19 ms（shell 侧） | journal `shot result=ok … ms=` |
| PyGObject 跨线程调 GLib | 判据连跑 3 轮崩 2 次（`corrupted size vs. prev_size`） | 改信箱后 5/5 稳定 |

### 7.6 第二批的判据
| 判据文件 | 钉什么 |
| --- | --- |
| `tests/test_shot_frames.py` | 联合矩形只抓一次 / 按屏切 / 忙等重试 / 跨屏裁剪的内容来源 / 吸附确定性 |
| `tests/test_shot_overlay.py`（重写） | **背景必须是冻结图**（图案在不在，不看 alpha）/ 整块画布不透明 / 跨屏各画自己那块 / 整屏捷径 / 无动画 |
| `tests/test_raise_pin_ui.py` | 样式表存在且"用到几个 class 就定义几个" / ≥3 条关闭路径（含 `ClearPins` 与 `--clear-pins`）/ **pushModal-popModal 配对** / 重复订阅去重 / `node --check` 语法 / 部署同步 |
| `tests/test_shot_panel.py` | 后端标签 / 两栏切换 / 复制口径 / "送去翻译只认 OCR 原文" / 出错话术 |
| `tests/test_shot_ocr.py` / `test_shot_translate.py` | 后端选择（本地优先、明确指定却不可用要拒绝）/ 助手 JSON 协议 / token 缓存 / 失败话术可操作 / **云直读图真的把图发出去了** / base_url 补兼容路径 |
| `tests/test_shot_text.py` | 接线层：画面落 PNG / 偏好翻得动 / 只有云能直读图 / 与窗的接口名 |

### 7.7 GTK3 上的两个真机坑（都已进判据，别改回去）

**① 全屏窗口必须先给尺寸**：`win.realize()` → `fullscreen_on_monitor()` → `show_all()` 会让窗口停在
GTK toplevel 的默认 **200×200**（mutter 把这张 stale 缓冲拉伸铺满整屏 ⇒ 看着像"整屏纯黑 / 一块矩形"）。
正确顺序（xvfb 顺序探针实测：旧顺序 device=200×200，新顺序 device=1600×900 = 期望）：

    win.set_default_size(mon_w, mon_h)      # 先给尺寸
    win.fullscreen_on_monitor(screen, idx)  # 再全屏到该屏
    win.show_all()                          # 最后显示；不要提前 realize()

map 之后还要**自检**：窗口尺寸 == 该屏逻辑尺寸（× scale），不对就 `resize` 重试，仍不对就**关掉遮罩 +
说清期望/实得**（`select_region` 返回 `(rect, failure)`；`command_region` 不写文件、退出码 1）。
自检放行"设备像素 / 逻辑像素"两种解释（Wayland 上 GDK 报哪种没有定论，认死一种会变成"每次自检失败自动关窗"）。

**② `draw` 里自定义绘制必须 `return True`**：`GtkWidget::draw` 的**默认处理器跑在自定义处理器之后**，
会把控件自己的主题背景（Adwaita `#fafafa ≈ 250`）刷在我们的画上面 —— 表现是"窗口尺寸全对、屏幕整块纯白"
（2026-10-19 真机：抓屏实测均匀 **250.0**、标准差 **0.0**；`return True` 之后同一块量到 **163.3 = 250×0.65**
= 真实桌面 × 压暗）。

### 7.8 第三批（2026-10-19）：界面重做（明江："太丑、易用性有待提高"，照 Snipaste 的手感）

**布局**：一条**底部居中**的主工具条（放在截图**下方**预留的带里；下方不够就翻到上方；
上下都没有才贴着图内下沿并标 `overlaps=True`）。窗口 = 画布（截图居中 + 四周留白 + 中性底色）
+ 结果抽屉 + 状态栏，**三块分层**（抽屉与工具条绝不挤一行）。

**工具条**（从拍到右）：撤销/重做 → 绘制类（矩形/椭圆/箭头/直线/荧光笔/自由笔）→ 标记类（序号/文字/马赛克）
→ 橡皮 → 颜色 7 色圆点 → 线宽 3 档圆点 → 动作区（**复制是主按钮**、存盘、贴图、关闭）。组间细分隔线；
每个按钮**图标 + 悬浮提示**（图标是 cairo 自己画的，见下）；当前工具/颜色/线宽用
`cp-btn-active` / `cp-swatch-active` / `cp-dot-active`（底色 + 描边）高亮，不是"稍深的灰"。

**可编辑的标注**：点中标注（空心图元按边线判、橡皮按盒内判）即选中 —— 选中画蓝框 + **8 个手柄**，
hover 画**双色虚线**（深底 + 白线，浅色截图上白线看不见）；拖主体移动、拖手柄改大小、
方向键微调 1px（Shift=10px）、Del/橡皮删除；这些编辑都进**编辑历史**，Ctrl+Z/Ctrl+Y 撤得回来
（删除是"从栈里拿掉 ⇒ 撤销按原下标插回"，这条分支单独写，别再混进"改几何"那条）。

**放大镜**：光标旁 80×80，**从冻结帧取样**（零成本、零延迟），`zoom=4` 且用 **FILTER_NEAREST**
（放大镜要能对准像素，双线性会把单像素糊掉）；中心画**方格**而不是十字线（十字线正好盖住你要对准的
那个像素）；边上带坐标读数（右下放不下就翻到左上）。`CAPSPLUS_MAGNIFIER=0` 可关。

**键位**（唯一真相源 `key_command()`）：`Enter` 复制并关（Snipaste 手感）· `Esc` 关 · `Ctrl+C/S` ·
`Ctrl+Z/Y` · 方向键微调 · `Del` · 右键菜单（复制/存盘/贴图/关闭）；遮罩那条链路回车仍是"确认选区"。

**工具/颜色/线宽的键**（2026-10-20 加，明江"工具栏有几个按钮不知道什么意思"）：工具 `R/E/A/L/H/P/B/T/M/X/I`、
颜色 `1..7`、线宽 `8/9/0`（按工具条上从左到右的顺序排）。**唯一真相源是
`TOOL_KEYS`/`COLOR_KEYS`/`WIDTH_KEYS` 三张表**：按钮提示、状态栏那两行（`tool_key_hint()` 现算）
与键处理（`tool_for_key()` 等）都读它 —— 判据断言"提示里写的键真能选中那个工具"、键位互不重复、
且提示行必须等于表里现算的那串（不许另抄一份）。字母键不引 Ctrl，所以与 `Ctrl+C/S/Z/Y/P` 不冲突。

**按钮提示**（2026-10-20）：**每个**工具/颜色/线宽/动作按钮都有"是什么 + 快捷键"的中文提示
（工具那句由 `TOOL_LABELS` + `TOOL_KEYS` + `shot_icons.TIPS` 拼成，三样各只有一处定义）；
鼠标悬停在哪个按钮上，状态栏主行立刻写 `悬停：吸管（I）：点一下取那个像素的颜色…`（提示气泡要等一秒，
状态栏是即时的）；移开回到 `工具：矩形（R）`。吸管本来就在工具表里（③ 不是缺功能，是发现性），
这条把它变得认得出。

**窗口宽度下限 = 工具条固有宽度**（2026-10-20，明江真机：260×420 的窄选区，窗口 288 px 而工具条
**1026 px** ⇒ 右端那排颜色圆点被窗口黑边切掉）。根因：窗口尺寸原来**只由截图算**，而工具条宽度与截图无关。
修法（他给的三选一里的 ①，治本）：`window_plan(..., min_width=bar_min_window_width(bar_w))`，
窗口宽度 = max(画布宽, 工具条宽 + 两边)，图像在变宽的画布里**居中**。
工具条宽度取 `get_preferred_width()[1]`，实测 **1026 = 33 个子控件 950 + 间距 64 + `.cp-bar` 的
padding 5×2 + border 1×2**（少算 CSS 那 12 px 时右端仍会切掉 6 px —— 真窗口判据抓到的就是它）。
判据 `TestNarrowWindowInARealWindow`（真窗口：实际分配的条右端必须 ≤ 窗口右端、且不压图）；
反例：`_build` 不传 `min_width` → 红（实测 `288 not greater than or equal to 1026`）。

**设计语言**（`shot_style.py`，唯一一份）：颜色一律 `@theme_*` 变量（跟随 GNOME 深浅色），
按钮有 hover/active/disabled 三态，间距/尺寸常量与布局代码同源。

**收尾五刀**（lead 看图点的，都已落地 + 判据）：
① 选中态**只有一套语言**：工具/颜色/线宽共用同一个 `cp-active`（底色 + 描边），颜色与线宽是
   "外圈 + 中心实心点"的同族语言；主按钮单独用 `@theme_selected_bg_color`。旧的三套类
   （`cp-btn-active`/`cp-swatch-active`/`cp-dot-active`）已删，判据断言三者取到的激活类**必须一致**。
② 抽屉高度**跟内容走**（`min_content_height=48` + `propagate_natural_height` + `max_content_height=200`）：
   两行文字不再留一大片空白，内容多了滚动。
③ 荧光笔定死为**半透明黄**（`HIGHLIGHT_COLOR = (1.0, 0.85, 0.15, 0.70)`）：跟随颜色会调出"深底 + 深笔"
   的橄榄色，压在上面的字读不出来。判据：高亮条亮度 ≥0.55 且与底色色差 ≥0.25（浅底/深底各一遍）。
④ 状态栏**分两档**：`cp-status-main`（常用键 Enter/Esc/Ctrl+C/S/Z，13px）与 `cp-status-more`
   （其余键，11px、暗一档），行距与分区间隔都放大。
⑤ **落笔一律裁到截图矩形内**：`clamp_to_image`/`clamp_rect_to_image`/`clip_shape_to_image`
   （指针坐标、拖动、微调、提交、自由笔点集全夹），`draw_canvas` 再对图像矩形 `clip()` 一道；
   导出/复制仍然只导出截图矩形（尺寸 == 底图）。判据+反例：把裁剪去掉 → 红。

**一条血泪（图标颜色别赌主题变量）**：主按钮底色是 `@theme_selected_bg_color`，第一版拿
"选中态前景色"画图标 ⇒ 某些主题下**前景色与底色同亮**，渲染出来抽屉右上角的复制按钮是个**空方块**。
现在改成 `readable_colour()`：量**按钮自己背景的亮度**再反着挑颜色（浅底深图标/深底浅图标）。

**判据两条纪律（都是崩过才知道的）**：① 真窗口判据里**所有碰控件的检查必须在 `destroy()` 之前做完** ——
控件销毁后再 `get_image()` / `get_style_context()` 是**段错误**（不是抛异常），整轮判据会崩掉；
② 判据里用到的 `GLib` 兜底定时器要在 `loop.run()` 之后 `source_remove`（只在没触发过时移除），
否则它留在默认主上下文里，会在**别的用例**开主循环时乱入（实测出过一次 6 秒的诡异失败）。

**判据纪律（那次漏测的根因）**：`Gtk.DrawingArea` **没有自己的背景** ⇒ 拿它测不出"窗口背景有没有盖上来"，
会**假绿**。这类判据必须用**真 `Gtk.Window`**（`Gtk.OffscreenWindow` 就是 `Gtk.Window` 子类，走真实主题背景
路径），连接窗口自己的 `draw`、渲染后 `Gdk.pixbuf_get_from_window` 读回像素，断言"是帧内容 + 压暗、
不是主题底色/纯色"。现有对照（同一反例具型"删掉 `return True`"下）：
`TestWindowsBackgroundNotOverpaint`（真 Window）**红 4 条**且复现出 `mean=0.980 std=0.000` 那一片纯白；
`TestRealWidgetDrawPath`（DrawingArea）**仍然全绿** —— 它只证明"draw 处理器把内容画对了"，
证明不了"没人盖在上面"。