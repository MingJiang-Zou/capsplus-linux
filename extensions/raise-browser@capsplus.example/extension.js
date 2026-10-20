// SPDX-License-Identifier: GPL-2.0-only
// Copyright (c) 2026 capsplus-linux contributors
// 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
//   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
/*
 * raise-browser —— 让默认浏览器**复用现有窗口**，而不是另起一扇新窗。
 *
 * 为什么需要它（2026-10-16 在本机 GNOME 50.1 实测）：
 *   GTK3 递不出 Wayland 激活令牌（libgdk-3 里字符串 `XDG_ACTIVATION_TOKEN` 出现 0 次）。
 *   没有令牌，mutter 不允许浏览器把已有窗口拉到前台 ⇒ 浏览器为了让你看得见只能新画一扇窗。
 *   扩展跑在 mutter 进程里，`Meta.Window.activate()` 是它自己的特权动作，不需要令牌。
 *
 * 为什么走 **D-Bus 信号**而不是"导出对象"—— 这台机的 GJS 实测（别再照网上的老写法抄）：
 *   - `Gio.DBus.session.export_object_path` / `unexport_object_path` / `return_value`
 *     / `return_error_literal` 全是 **undefined**（GJS 的便捷函数已被摘掉）；
 *   - `register_object` 存在，但暴露的是**原始 C 绑定**：报 `At least 5 arguments required`，
 *     而 vtable 是 C 结构、JS 构造不出来 ⇒ 导出对象在本机走不通；
 *   - `emit_signal`（5 个实参：destination, object_path, interface, member, parameters）与
 *     `signal_subscribe`（7 个实参，**回调在最后一位**）实测端到端可用。
 *   所以：Qbar 发 `RaiseBrowser` / `Probe` 信号，本扩展订阅并执行，结果写 journal。
 *
 * 只激活**当前工作区**的窗口：不把人从别的工作区硬拽过来；不满足就报 `off-current-workspace`。
 * 信号没有回复通道 ⇒ Qbar 侧不知道成没成，这没问题：失败最多退回旧行为，绝不挡住"打开网址"。
 *
 * 追加（2026-10-19，替代 Snipaste 的截图工具第一批）：同一套信号通道再挂两个成员——
 *   `CaptureArea(x, y, w, h, outPath)`：抓一块矩形写 PNG；
 *   `PinImage(rawPath, w, h, x, y, ackPath)`：把一张 RGBA 原图钉成**真置顶**的图层 actor。
 * 为什么抓帧只能在这里做（外部调被拒的实测见 capsplus-linux 的 docs/SHOT-TOOL.md）：
 *   外部 `org.gnome.Shell.Screenshot.ScreenshotArea` 实测 `AccessDenied`；而 shell 进程内
 *   `Shell.Screenshot`（Shell-18.typelib）可用。闪屏是 shell 自己 `ui/screenshot.js` 里
 *   `Flashspot` 那段 JS 动画（500ms opacity 255→0）干的，**不在 C 抓帧里** ⇒ 本文件直接调
 *   `screenshot_area(x, y, w, h, stream)`、不碰那段动画，天然不闪（这正是明江换掉 Snipaste 的原因）。
 */
import Clutter from 'gi://Clutter';
import Cogl from 'gi://Cogl';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const BUS_NAME = 'io.github.capsplus.RaiseBrowser';
const OBJ_PATH = '/io/github/capsplus/RaiseBrowser';
const IFACE = 'io.github.capsplus.RaiseBrowser';
const M_RAISE = 'RaiseBrowser';
const M_PROBE = 'Probe';
const M_REPORT = 'ProbeReport';
const M_CAPTURE = 'CaptureArea';
const M_PIN = 'PinImage';
const M_CLEAR = 'ClearPins';

let _ownId = 0;
let _subRaise = 0;
let _subProbe = 0;
let _subCapture = 0;
let _subPin = 0;
let _subClear = 0;

/**
 * 同一条信号被**两个订阅**收到的情况：扩展被 `gnome-extensions disable/enable` 热重载过。
 * 旧实例的订阅摘不掉（这版 GJS 没有 `signal_remove`，见 `disable()`），于是每次请求都跑两遍：
 *   * 抓帧：两条 handler 抢同一个 `.part` ⇒ 后一条触发 "Only one screenshot operation at a time"，
 *     它的失败清理又删掉 `.part`，前一条 rename 时"没有那个文件" ⇒ **两条都失败**。
 *     2026-10-19 实测（明江那边就是"按了没反应"）：每次抓帧都是两行 started + 两行 fail。
 *   * 贴图：一张图会变成两张；清空：ack 写两遍。
 *
 * 所以用 shell 的 `global` 当**跨实例**去重表 —— 模块级变量各实例一份，挡不住这种重复。
 * 窗口取 1 秒：人手重复触发不会这么快，而重复订阅一定是同一瞬间。
 */
function duplicateRequest(kind, key, windowMs = 1000) {
    const now = GLib.get_monotonic_time() / 1000;
    global.__capsplusSeen = global.__capsplusSeen || new Map();
    const seen = global.__capsplusSeen;
    const last = seen.get(`${kind}:${key}`);
    seen.set(`${kind}:${key}`, now);
    if (seen.size > 64) {
        for (const [k, v] of seen) {
            if (now - v > 10000)
                seen.delete(k);
        }
    }
    if (last !== undefined && (now - last) < windowMs) {
        console.log(`[raise-browser] dup request=${kind} key=${key} —— 跳过（重复订阅）`);
        return true;
    }
    return false;
}

function defaultBrowserDesktopId() {
    const info = Gio.AppInfo.get_default_for_type('x-scheme-handler/https', false);
    return info ? (info.get_id() || null) : null;
}

/** 浏览器现有窗口 + 匹配来源（先按 .desktop 查 App，查不到再按 wm_class 兜底）。 */
function browserWindows() {
    const id = defaultBrowserDesktopId();
    const out = [];
    if (id) {
        const app = Shell.AppSystem.get_default().lookup_app(id);
        if (app) {
            // GNOME 50 的 Shell.App.get_windows() 是 0 参：传 false 会在 journal 刷
            // `Too many arguments … expected 0, got 1`（本机 18:47 实测）。要「当前工作区」
            // 就自己按 workspace 过滤，别指望这个参数。
            for (const w of app.get_windows()) {
                out.push({ w, why: `app=${id}` });
            }
        }
    }
    if (out.length === 0 && id) {
        const hint = id.replace(/\.desktop$/, '').toLowerCase();
        for (const w of global.display.get_tab_list(Meta.TabList.NORMAL_ALL, null)) {
            const cls = w.get_wm_class ? String(w.get_wm_class() || '') : '';
            const gid = w.get_gtk_application_id ? String(w.get_gtk_application_id() || '') : '';
            if (`${cls} ${gid}`.toLowerCase().includes(hint)) {
                out.push({ w, why: `wmclass~=${hint}` });
            }
        }
    }
    return out;
}

function activeWorkspace() {
    return global.workspace_manager ? global.workspace_manager.get_active_workspace() : null;
}

/** 一行可 grep 的报告。A/B 判定就靠这里的 windows= 数（+1 = 另起新窗，0 = 原窗口开标签页）。 */
function report(extra) {
    const id = defaultBrowserDesktopId();
    const all = browserWindows();
    const ws = activeWorkspace();
    const here = ws ? all.filter(c => c.w.get_workspace() === ws).length : all.length;
    return `default=${id} windows=${all.length} on-current=${here}${extra ? ' ' + extra : ''}`;
}

function raiseBrowser() {
    const all = browserWindows();
    if (!all.length) {
        console.log(`[raise-browser] raise ${report('result=no-window')}`);
        return;
    }
    const ws = activeWorkspace();
    const here = ws ? all.filter(c => c.w.get_workspace() === ws) : all;
    if (!here.length) {
        console.log(`[raise-browser] raise ${report('result=off-current-workspace')}`);
        return;
    }
    const w = here[0].w;
    try {
        if (w.minimized) {
            w.unminimize();
        }
        w.activate(global.get_current_time());
        console.log(`[raise-browser] raise ${report(`result=raised via=${here[0].why}`)}`);
    } catch (e) {
        console.error(`[raise-browser] activate 失败：${e.message} ${report('')}`);
    }
}

/* ---------------------------------------------------------------- 截图抓帧 --- */

// shell 自己（ui/screenshot.js 顶部）就是这么把 async 方法变成 Promise 的：
//   Gio._promisify(Shell.Screenshot.prototype, 'screenshot_area');
// 本机 gjs 实测 `Gio._promisify` 存在；万一哪个版本没有，就退回 (callback, user_data) 老形态——
// C 侧签名（GIRepository dump，2026-10-19）是
//   screenshot_area(x:int32, y:int32, w:int32, h:int32, stream:Gio.OutputStream, callback, user_data)
// 两种形态都对得上，谁也别删。
let _areaPromisified = false;
try {
    Gio._promisify(Shell.Screenshot.prototype, 'screenshot_area');
    _areaPromisified = true;
} catch (e) {
    console.error(`[raise-browser] screenshot_area 没法 promisify（退回回调形态）：${e.message}`);
}

let _shooter = null;

/** 复用一个 Shell.Screenshot 实例（shell 自己也是 new 一个用反复；省掉每次抓帧的构造）。 */
function shooter() {
    if (!_shooter)
        _shooter = new Shell.Screenshot();
    return _shooter;
}

/**
 * 外部给的矩形是**逻辑坐标**（GTK/面板看到的坐标），交给 C 抓帧前要乘 stage 的 scale factor——
 * shell 自己的 D-Bus 截图接口就是这么换算的（`screenshot.js` 的 `_scaleArea`）：
 * 少这一步，在缩放屏（本机有一块 scale=2 的 VILTROX）上抓出来的框会整体偏小/偏左。
 */
function stageScale() {
    try {
        const sc = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        return sc > 0 ? sc : 1;
    } catch (e) {
        return 1;
    }
}

function callArea(shot, x, y, w, h, stream, done) {
    if (_areaPromisified) {
        shot.screenshot_area(x, y, w, h, stream)
            .then(res => done(null, res))
            .catch(e => done(e, null));
        return;
    }
    shot.screenshot_area(x, y, w, h, stream, (src, res) => {
        try {
            done(null, src.screenshot_area_finish(res));
        } catch (e) {
            done(e, null);
        }
    });
}

/**
 * 一行可 grep 的抓帧痕迹（判活/定位都读它，capsplus 侧 `capsplus/shot_frame.py` 用同一个格式解析）：
 *   [raise-browser] shot result=started path=/… rect=x,y,w,h scale=1
 *   [raise-browser] shot result=ok      path=/… rect=… scale=1 ms=42
 *   [raise-browser] shot result=fail    path=… why=…
 * 先写 `<out>.part` 再 rename 成 outPath：**outPath 一出现就是完整 PNG**，
 * 调用方（capsplus 等文件出现）不会读到半张图 —— 这是本链路上唯一能防"静默半图"的办法。
 */
/** 抓帧期间把手头所有贴图**真的藏起来**，抓完/失败/超时一律恢复（明江 2026-10-19 定的口径）。 */
function hidePinsForCapture() {
    const was = _pins.map(p => p.actor.visible);
    const plan = pinVisibilityPlan(was);
    for (const i of plan.hide)
        _pins[i].actor.hide();                 // 真 hide，**不是** set_opacity(0)：冻结是合成后抓的
    return {was};
}

function restorePinsAfterCapture(saved) {
    if (!saved || !saved.was)
        return;
    const plan = pinVisibilityPlan(saved.was);
    for (const i of plan.restore) {
        if (_pins[i])
            _pins[i].actor.show();
    }
}

function captureArea(x, y, width, height, outPath) {
    const t0 = GLib.get_monotonic_time();
    const ms = () => Math.round((GLib.get_monotonic_time() - t0) / 1000);
    const scale = stageScale();
    const sx = Math.round(x * scale), sy = Math.round(y * scale);
    const sw = Math.round(width * scale), sh = Math.round(height * scale);
    const rect = `rect=${sx},${sy},${sw},${sh} scale=${scale}`;
    // `.part` 名字带**每次调用唯一**的后缀：万一还是有两个 handler 同时进来（老实例的订阅摘不掉），
    // 它们各写各的，不会互相删文件 —— 只有 rename 到同一个 outPath 那一步会撞，谁先到谁赢（内容一样）。
    const part = Gio.File.new_for_path(
        `${outPath}.${GLib.get_monotonic_time().toString(36)}.part`);
    const out = Gio.File.new_for_path(outPath);
    console.log(`[raise-browser] shot result=started path=${outPath} ${rect}`);

    // **遮罩压在贴图之上**（明江 2026-10-19 定的口径，见 docs/SHOT-TOOL.md §7.9）：
    // 贴图在 shell 图层、GTK 遮罩在窗口层，物理上抬不过去；等价做法是抓帧前把贴图
    // 真的藏起来、抓完恢复 —— 这样 ①压暗能盖住原贴图位置 ②新截图不会把旧贴图拍进去。
    const hidden = hidePinsForCapture();
    let restored = false;
    const restore = () => {                       // 所有出口共用这一个漏斗
        if (restored)
            return;
        restored = true;
        restorePinsAfterCapture(hidden);
    };
    // 兜底：万一回调永远不来（进程被杀/超时），30 秒后也要把贴图放回来
    const safety = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => {
        restore();
        return GLib.SOURCE_REMOVE;
    });

    let stream = null;
    try {
        stream = part.replace(null, false, Gio.FileCreateFlags.NONE, null);
    } catch (e) {
        console.log(`[raise-browser] shot result=fail path=${outPath} why=open:${e.message} ms=${ms()}`);
        restore();
        if (safety)
            GLib.source_remove(safety);
        return;
    }

    callArea(shooter(), sx, sy, sw, sh, stream, (err, res) => {
        // 失败一律**不留文件**：不然调用方会把半张图当成功。
        const cleanup = () => {
            try {
                if (part.query_exists(null))
                    part.delete(null);
            } catch (e) {
                console.error(`[raise-browser] shot 清理 ${part.get_path()} 失败：${e.message}`);
            }
        };
        const ok = !err && !!res && res[0] !== false;
        if (!ok) {
            const why = err ? err.message : 'screenshot_area_finish 返回 false';
            try {
                if (stream)
                    stream.close(null);
            } catch (e) {
                // 关不掉就算了：下面照样删临时文件
            }
            cleanup();
            console.log(`[raise-browser] shot result=fail path=${outPath} why=${why} ms=${ms()}`);
            restore();
            if (safety)
                GLib.source_remove(safety);
            return;
        }
        try {
            stream.close(null);
        } catch (e) {
            cleanup();
            console.log(`[raise-browser] shot result=fail path=${outPath} why=close:${e.message} ms=${ms()}`);
            restore();
            if (safety)
                GLib.source_remove(safety);
            return;
        }
        try {
            if (out.query_exists(null))
                out.delete(null);
            part.move(out, Gio.FileCopyFlags.OVERWRITE, null, null);
        } catch (e) {
            cleanup();
            console.log(`[raise-browser] shot result=fail path=${outPath} why=rename:${e.message} ms=${ms()}`);
            restore();
            if (safety)
                GLib.source_remove(safety);
            return;
        }
        console.log(`[raise-browser] shot result=ok path=${outPath} ${rect} ms=${ms()}`);
        restore();
        if (safety)
            GLib.source_remove(safety);
    });
}

/* ---------------------------------------------------------------- 贴图（真置顶） --- */

// 为什么贴图只能在这里做（2026-10-19 实测）：Wayland 下 GTK3 窗口**没有置顶 API**
// （docs/KNOWN-LIMITS.md 记着这条，那份文档不在本批改动范围内）。GNOME 里唯一"真的在所有窗口之上"
// 的地方是 shell 的图层 actor：`Main.layoutManager.addTopChrome(actor)`
// —— shell 自己的 layout.js 注释写死了：`addChrome` 会把 actor 放在 top_window_group **下面**，
// 要 "above all windows, including popups" 就得用 `addTopChrome`（或直接 uiGroup.add_child）。
const PIN_MIN_SCALE = 0.2;
const PIN_MAX_SCALE = 4.0;
const PIN_DOUBLE_CLICK_MS = 350;      // 双击判定间隔（自己算，不依赖 event 的 click_count）
const PIN_OPACITY_STEP = 0.1;
const PIN_OPACITY_MIN = 0.2;          // 再低就找不着了（Snipaste 也有下限）

let _pins = [];                        // 多张并存：每项 = {actor, scale, opacity, dragging, grab, ...}

function pinLog(result, extra) {
    console.log(`[raise-browser] pin result=${result}${extra ? ' ' + extra : ''}`);
}

// ── 模态焦点：点/拖某张贴图 → 键盘归它，Esc 关掉它（明江 2026-10-19 要的就是 Snipaste 这套）──
//
// **本机实测**（不是照网上写法来的，2026-10-19 从 shell 自己的 JS 资源里读出来的：
//   `gresource list /usr/lib/gnome-shell/libshell-18.so` → 抽 `/org/gnome/shell/ui/main.js`）：
//   * `Main.pushModal(actor, params = {})` 内部是 `global.stage.grab(actor)`，**返回 `Clutter.Grab`**，
//     默认 `actionMode: Shell.ActionMode.NONE`；它自己 `set_key_focus(actor)`
//     —— 所以 `can_focus` 不参与（保持 false 挺好：普通焦点不会 Tab 到贴图上）；
//   * `Main.popModal(grab)` 收的是**那个 grab**（不是 actor），找不到就 `throw new Error('incorrect pop')`
//     ⇒ 必须自己存 grab、pop 之前先清引用，绝不二次 pop；
//   * pushModal 还会 `actor.connect('destroy', …popModal)`：actor 一销毁就自动摘焦点。**仍然显式配对**，
//     因为我们要保证"能 destroy 的每条路都先 pop"（宁可多一次显式调用，也不要赌自动那一下）。
//   * 它是 `global.stage.grab`：**键盘和鼠标一起抓**。所以"点窗外"要主动交还焦点
//     （见 `_isOutside`）—— 否则明江点到别的窗口会像"点不动"。
//
// ⚠ 灾难级失误的防线：pushModal 没配对 popModal = shell 卡在模态态、明江的键盘进不了任何应用。
//   本文件里 pushModal **只有一处**（`grabPinFocus`），popModal **只有一处**（`releasePinFocus`），
//   而所有销毁路径都必经 `destroyPin` → `releasePinFocus`。判据
//   `tests/test_raise_pin_ui.py::TestModalFocusIsPaired` 就是钉这件事的（删掉 pop 必红）。
function releasePinFocus(pin) {
    if (!pin.grab)
        return false;
    const grab = pin.grab;
    pin.grab = null;               // 先清引用：万一 popModal 抛了也不会有人再 pop 同一个 grab
    try {
        Main.popModal(grab);
    } catch (e) {
        console.error(`[raise-browser] popModal 失败：${e.message}`);
    }
    pin.actor.remove_style_class_name('capsplus-pin-active');
    pinLog('focus-released', `left=${_pins.length}`);
    return true;
}

function grabPinFocus(pin) {
    if (pin.grab)
        return true;               // 已经拿着了（重复点不该 push 第二次）
    for (const other of _pins) {    // 焦点**只在该张上**：换一张就把上一张的交还
        if (other !== pin)
            releasePinFocus(other);
    }
    try {
        pin.grab = Main.pushModal(pin.actor);
    } catch (e) {
        pin.grab = null;
        pinLog('focus-fail', `why=${e.message}`);
        return false;
    }
    pin.actor.add_style_class_name('capsplus-pin-active');
    pinLog('focus', `left=${_pins.length}`);
    return true;
}

function destroyPin(pin) {
    const idx = _pins.indexOf(pin);
    if (idx !== -1)
        _pins.splice(idx, 1);
    releasePinFocus(pin);           // **唯一出口**：任何销毁路径都先摘焦点（配对纪律，见上面的注释）
    if (pin.menu) {
        try {
            pin.menu.destroy();
        } catch (e) {
            console.error(`[raise-browser] pin 菜单销毁失败：${e.message}`);
        }
        pin.menu = null;
    }
    try {
        Main.layoutManager.removeChrome(pin.actor);
    } catch (e) {
        console.error(`[raise-browser] pin removeChrome 失败：${e.message}`);
    }
    try {
        pin.actor.destroy();
    } catch (e) {
        console.error(`[raise-browser] pin destroy 失败：${e.message}`);
    }
    pinLog('closed', `left=${_pins.length}`);
}

function destroyAllPins() {
    for (const pin of _pins.slice())
        destroyPin(pin);
}

/** 扩展停用/热重载：没有贴图可恢复时要保证**没有任何东西**还等着被恢复。 */
function restoreAllPins() {
    for (const pin of _pins) {
        if (pin.hoverTimer) {
            GLib.source_remove(pin.hoverTimer);
            pin.hoverTimer = 0;
        }
    }
}

/** 全局兜底（keyd 的 `Caps+Esc` 走这里）：清空所有贴图，**没有贴图时也安全**。 */
function clearPins(ackPath) {
    const before = _pins.length;
    destroyAllPins();
    if (ackPath)
        GLib.file_set_contents(ackPath, `cleared=${before}\nleft=${_pins.length}\n`);
    pinLog('clear-all', `cleared=${before} left=${_pins.length}`);
    return before;
}

// ── 工具条（hover 出现）的三件事：复制 / 存盘 / 关闭 ────────────────────────────
// 为什么复制和存盘要 PNG 路径：`St.Clipboard.set_content(type, 'image/png', bytes)` 要**PNG 字节**，
// 而贴图本体在 shell 这边是裸 RGBA（不解码是刻意的）。所以 PinImage 信号里多带一个 pngPath
// —— capsplus 侧本来就生成过那个 PNG，白拿。（用法与 shell 自己的 screenshot.js 一致，实测同上。）

function copyPin(pin) {
    if (!pin.pngPath)
        return pinLog('copy', 'why=没有 PNG 路径');
    try {
        const [ok, bytes] = GLib.file_get_contents(pin.pngPath);
        if (!ok)
            throw new Error(`读不到 ${pin.pngPath}`);
        St.Clipboard.get_default().set_content(St.ClipboardType.CLIPBOARD, 'image/png',
            GLib.Bytes.new(bytes));
        pinLog('copy', `bytes=${bytes.length}`);
    } catch (e) {
        pinLog('copy-fail', `why=${e.message}`);
    }
}

function savePin(pin) {
    if (!pin.pngPath)
        return pinLog('save', 'why=没有 PNG 路径');
    try {
        const dir = GLib.build_filenamev([GLib.get_home_dir(), 'Pictures', 'Screenshots']);
        GLib.mkdir_with_parents(dir, 0o755);
        const stamp = GLib.DateTime.new_now_local().format('%Y-%m-%d-%H%M%S');
        const dest = GLib.build_filenamev([dir, `pin-${stamp}.png`]);
        const [ok, bytes] = GLib.file_get_contents(pin.pngPath);
        if (!ok)
            throw new Error(`读不到 ${pin.pngPath}`);
        GLib.file_set_contents(dest, bytes);
        pinLog('save', `path=${dest} bytes=${bytes.length}`);
    } catch (e) {
        pinLog('save-fail', `why=${e.message}`);
    }
}

function setPinOpacity(pin, op) {
    pin.opacity = Math.min(1.0, Math.max(PIN_OPACITY_MIN, op));
    pin.actor.set_opacity(Math.round(pin.opacity * 255));   // St 的 opacity 是 0–255
    pinLog('opacity', `value=${pin.opacity.toFixed(2)}`);
}

// ── hover 工具条的**纯逻辑**（判据在 node 里真跑这两个函数）─────────────────────
// 2026-10-19 真机事故：鼠标移上贴图工具条出现，再往工具条上移就消失、点不到。
// 根因：工具条画在贴图**外面**（`pin.height + 6`），而贴图的 leave-event 立刻 hide ⇒
// 鼠标离开贴图那一步就把工具条收了，永远到不了按钮。修法是双保险：
//   ① 工具条放进贴图**内部**（右上角），鼠标不必离开 actor 就能到按钮；
//   ② leave 不立刻 hide，给 HOVER_GRACE_MS 宽限，进工具条/按钮就取消。
const HOVER_GRACE_MS = 250;             // 200–300ms 宽限：跨空隙也能走到工具条上
const TOOLBAR_GAP = 6;                  // 工具条与贴图之间的空隙（宽限要大于走过去的时间）

/**
 * 工具条的位置：**在贴图外面**（明江 2026-10-20 明确：摆里面"会挡住贴图部分内容"）。
 *
 * 默认**下方**、下面放不下翻**上方**；再越界就左右挪，保证整条都在 stage 里。
 * 返回的 `overlaps` 是判据要的那个硬条件：**与贴图矩形零重叠**。
 * 与贴图之间留 `TOOLBAR_GAP` 的空隙 —— 跨这段空隙时靠 hover 宽限（`HOVER_GRACE_MS`）接住，
 * 所以"点不到"的病根（离开就 hide）不能复发。
 */
function toolbarGeometry(pinWidth, pinHeight, barWidth, barHeight,
                         pinX = 0, pinY = 0, stageW = 0, stageH = 0) {
    const below = pinHeight + TOOLBAR_GAP;                  // 贴图右下角的相对坐标
    let y = below;
    if (stageH && pinY + pinHeight + TOOLBAR_GAP + barHeight > stageH)
        y = -TOOLBAR_GAP - barHeight;                       // 下面放不下 ⇒ 翻到上方
    let x = Math.max(0, Math.round(pinWidth - barWidth));   // 默认与贴图右缘对齐
    if (stageW) {
        const absX = pinX + x;
        if (absX + barWidth > stageW)
            x = Math.max(0, stageW - pinX - barWidth);
        if (pinX + x < 0)
            x = -pinX;
    }
    const overlaps = x < pinWidth && x + barWidth > 0 && y < pinHeight && y + barHeight > 0;
    return {x, y, overlaps};
}

/** 三个按钮的命中矩形（点击区要够大：工具条高 h、每个按钮宽 w）。 */
function toolbarButtons(geo, barHeight, count) {
    const width = 34;                   // 每个按钮 34×barHeight，比图标大一圈，好点
    const out = [];
    for (let i = 0; i < count; i++)
        out.push({index: i, x: geo.x + i * width, y: geo.y,
                  w: width, h: barHeight});
    return out;
}

/** 命中哪个按钮（点不到就 -1）—— `×` 必须一击即中，所以判定要简单直接。 */
function hitButton(buttons, px, py) {
    for (const b of buttons) {
        if (px >= b.x && px < b.x + b.w && py >= b.y && py < b.y + b.h)
            return b.index;
    }
    return -1;
}

/**
 * hover 状态机：`state = {visible, deadline}`，`event` 是
 * `pin-enter | pin-leave | bar-enter | bar-leave | tick`，`now` 是毫秒。
 * 返回 `{state, action}`，action ∈ `show | hide | none`。
 */
function hoverPlan(state, event, now) {
    const visible = !!state.visible;
    const deadline = state.deadline || 0;
    switch (event) {
    case 'pin-enter':
    case 'bar-enter':
        return {state: {visible: true, deadline: 0}, action: 'show'};
    case 'pin-leave':
    case 'bar-leave':
        // **不立刻 hide**：起算宽限，鼠标在这段时间里进工具条就取消
        return {state: {visible, deadline: now + HOVER_GRACE_MS}, action: 'none'};
    case 'tick':
        if (visible && deadline && now >= deadline)
            return {state: {visible: false, deadline: 0}, action: 'hide'};
        return {state: {visible, deadline}, action: 'none'};
    default:
        return {state: {visible, deadline}, action: 'none'};
    }
}

/** 抓帧期间"藏哪些、还哪些"：只动**当时可见**的那些，其余不动（不扰乱贴图自身状态）。 */
function pinVisibilityPlan(wasVisible) {
    return {hide: wasVisible.map((v, i) => (v ? i : -1)).filter(i => i >= 0),
            restore: wasVisible.map((v, i) => (v ? i : -1)).filter(i => i >= 0)};
}

function buildPinToolbar(pin) {
    const bar = new St.BoxLayout({style_class: 'capsplus-pin-toolbar', reactive: true,
                                  track_hover: true, visible: false});
    for (const [label, cb] of [['复制', () => copyPin(pin)],
                               ['存盘', () => savePin(pin)],
                               ['×', () => destroyPin(pin)]]) {
        const btn = new St.Button({label, style_class: 'capsplus-pin-tool', can_focus: false});
        btn.connect('clicked', cb);
        bar.add_child(btn);
    }
    // 工具条是**贴图的子 actor**，而且画在贴图**内部右上角**（lead 的修法 ①）：
    // 之前画在 `pin.height + 6`（贴图外面），鼠标离开贴图那一步就够不着按钮了。
    const BAR_H = 34, BAR_W = 34 * 3 + 4;
    const geo = toolbarGeometry(pin.width, pin.height, BAR_W, BAR_H, pin.x, pin.y,
                                global.stage.width, global.stage.height);
    bar.set_size(BAR_W, BAR_H);
    bar.set_position(geo.x, geo.y);
    pin.toolbarGeometry = geo;
    pin.buttons = toolbarButtons(geo, BAR_H, 3);
    // 按钮点击区够大 + hover 高亮（样式类在 stylesheet 里）
    let i = 0;
    for (const child of bar.get_children())
        child.set_size(34, BAR_H);
    pinLog('toolbar', `x=${geo.x} y=${geo.y} overlaps=${geo.overlaps}`);
    // hover 宽限（lead 的修法 ②）：进工具条取消隐藏、离开重新起算
    const arm = () => {
        const r = hoverPlan(pin.hover || {visible: true, deadline: 0}, 'bar-leave',
                            GLib.get_monotonic_time() / 1000);
        pin.hover = r.state;
        if (pin.hoverTimer) {
            GLib.source_remove(pin.hoverTimer);
            pin.hoverTimer = 0;
        }
        pin.hoverTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, HOVER_GRACE_MS, () => {
            pin.hoverTimer = 0;
            const t2 = hoverPlan(pin.hover, 'tick', GLib.get_monotonic_time() / 1000);
            pin.hover = t2.state;
            if (t2.action === 'hide') {
                bar.hide();
                const owner = pin.actor;
                owner.set_scale(pin.scale, pin.scale);   // 不动贴图状态，只收工具条
            }
            return GLib.SOURCE_REMOVE;
        });
    };
    const disarm = () => {
        if (pin.hoverTimer) {
            GLib.source_remove(pin.hoverTimer);
            pin.hoverTimer = 0;
        }
        const r = hoverPlan(pin.hover || {visible: true, deadline: 0}, 'bar-enter',
                            GLib.get_monotonic_time() / 1000);
        pin.hover = r.state;
    };
    bar.connect('enter-event', () => {
        disarm();
        bar.show();
        return Clutter.EVENT_PROPAGATE;
    });
    bar.connect('leave-event', () => {
        arm();
        return Clutter.EVENT_PROPAGATE;
    });
    return bar;
}

function openPinMenu(pin) {
    if (pin.menu) {
        try {
            pin.menu.destroy();
        } catch (e) {
            pin.menu = null;
        }
    }
    // 签名实测自 shell 自己的 `backgroundMenu.js`：`new PopupMenu.PopupMenu(sourceActor, 0, St.Side.TOP)`
    // + `layoutManager.uiGroup.add_child(menu.actor)` + `menu.open()`。
    const menu = new PopupMenu.PopupMenu(pin.actor, 0, St.Side.TOP);
    const add = (label, cb) => {
        const item = new PopupMenu.PopupMenuItem(label);
        item.connect('activate', cb);
        menu.addMenuItem(item);
    };
    add('关闭这张（Esc）', () => destroyPin(pin));
    add('复制到剪贴板', () => copyPin(pin));
    add('存盘到图片目录', () => savePin(pin));
    menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
    add('更不透明（Ctrl+滚轮）', () => setPinOpacity(pin, pin.opacity + PIN_OPACITY_STEP));
    add('更透明（Ctrl+滚轮）', () => setPinOpacity(pin, pin.opacity - PIN_OPACITY_STEP));
    add('清空所有贴图', () => clearPins(''));
    Main.layoutManager.uiGroup.add_child(menu.actor);
    menu.open();
    pin.menu = menu;
    pinLog('menu-open', `left=${_pins.length}`);
    return menu;
}

/** 这次点击在不在贴图（含工具条）身上 —— 抓了模态抓不到别人，得自己算。 */
function isOutsidePin(pin, event) {
    const [ex, ey] = event.get_coords();
    const [ax, ay] = pin.actor.get_transformed_position();
    const [, aw, ah] = [0, pin.width, pin.height];
    const slack = 40;                                   // 工具条在下方 40px 内也算"贴图上"
    return ex < ax || ey < ay || ex > ax + aw || ey > ay + ah + slack;
}

/**
 * 把一张**已经是 RGBA 原图**的文件钉成置顶图层 actor。
 * 为什么传原图而不是 PNG：shell 这边不做解码（GJS 里没有现成的 PNG→像素），解码由 capsplus
 * 侧用 PIL 做完（`capsplus/shot_pin.py`），少一整条会出错的路。PNG 路径单独带进来，只给
 * "复制/存盘"两个按钮用（见 `copyPin`）。
 *
 * 交互（2026-10-19 按 Snipaste 对齐）：
 *   * 左键拖动移动、滚轮缩放（0.2×–4×）、**Ctrl+滚轮调透明度**（0.2–1.0）；
 *   * 点击/拖动 → 拿键盘焦点（模态），**Esc 关掉这张**；点窗外交还焦点；
 *   * hover 出工具条（复制 / 存盘 / ×）；右键出菜单（关闭 / 复制 / 存盘 / 透明度 / 清空全部）；
 *   * 双击关闭、点右上角 × 关闭、`Caps+Esc` 全局清空（走 D-Bus 的 ClearPins）。
 * 五条路都归到 `destroyPin` 这一个出口 —— 焦点配对纪律全靠这一点。
 * 成功写 ack 文件（内容含 left= 张数）—— 与抓帧"等产物"同一条口径：没有回复通道，
 * 所以"成没成"只认文件。
 */
function pinImage(pngPath, rawPath, width, height, x, y, ackPath) {
    const t0 = GLib.get_monotonic_time();
    const ms = () => Math.round((GLib.get_monotonic_time() - t0) / 1000);
    try {
        const [ok, raw] = GLib.file_get_contents(rawPath);
        if (!ok)
            throw new Error(`读不到 ${rawPath}`);
        if (raw.length < width * height * 4)
            throw new Error(`原图太小：${raw.length} < ${width * height * 4}`);
        const coglContext = global.stage.context.get_backend().get_cogl_context();
        const content = St.ImageContent.new_with_preferred_size(width, height);
        // RGBA_8888 = 每像素 4 字节（shell 自己的 qrCode.js 也是这么算 stride 的）
        if (!content.set_bytes(coglContext, GLib.Bytes.new(raw), Cogl.PixelFormat.RGBA_8888,
                               width, height, width * 4))
            throw new Error('St.ImageContent.set_bytes 返回 false');

        const holder = new St.Widget({style_class: 'capsplus-pin', reactive: true,
                                      track_hover: true});
        holder.set_content(content);
        holder.set_size(width, height);

        const actor = new St.Widget({style_class: 'capsplus-pin-box', reactive: true,
                                     track_hover: true});
        actor.add_child(holder);
        actor.set_size(width, height);
        actor.set_position(x, y);
        actor.set_pivot_point(0.5, 0.5);      // 缩放绕中心，不然会"跑"

        const pin = {actor, pngPath: pngPath || '', rawPath, width, height, x, y,
                     scale: 1.0, opacity: 1.0, dragging: null, lastClickMs: 0, grab: null,
                     menu: null};
        _pins.push(pin);

        const toolbar = buildPinToolbar(pin);
        actor.add_child(toolbar);
        pin.toolbar = toolbar;
        actor.connect('enter-event', () => {
            if (pin.hoverTimer) {
                GLib.source_remove(pin.hoverTimer);
                pin.hoverTimer = 0;
            }
            const r = hoverPlan(pin.hover || {visible: true, deadline: 0}, 'pin-enter',
                                GLib.get_monotonic_time() / 1000);
            pin.hover = r.state;
            toolbar.show();
            return Clutter.EVENT_PROPAGATE;
        });
        actor.connect('leave-event', () => {
            // **不立刻 hide**：给 250ms 宽限，鼠标能走到工具条上（2026-10-19 修的第二条）
            const r = hoverPlan(pin.hover || {visible: true, deadline: 0}, 'pin-leave',
                                GLib.get_monotonic_time() / 1000);
            pin.hover = r.state;
            if (pin.hoverTimer) {
                GLib.source_remove(pin.hoverTimer);
                pin.hoverTimer = 0;
            }
            pin.hoverTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, HOVER_GRACE_MS, () => {
                pin.hoverTimer = 0;
                const t2 = hoverPlan(pin.hover, 'tick', GLib.get_monotonic_time() / 1000);
                pin.hover = t2.state;
                if (t2.action === 'hide')
                    toolbar.hide();
                return GLib.SOURCE_REMOVE;
            });
            return Clutter.EVENT_PROPAGATE;
        });

        // Esc 关掉光标下这张 —— 只在拿到模态焦点时到得了（没焦点时不误伤别人）
        actor.connect('key-press-event', (a, event) => {
            if (event.get_key_symbol() !== Clutter.KEY_Escape)
                return Clutter.EVENT_PROPAGATE;
            pinLog('esc', `left=${Math.max(_pins.length - 1, 0)}`);
            destroyPin(pin);
            return Clutter.EVENT_STOP;
        });

        actor.connect('button-press-event', (a, event) => {
            const button = event.get_button();
            if (button === 3) {                        // 右键：菜单（Snipaste 也是右键出菜单）
                grabPinFocus(pin);
                openPinMenu(pin);
                return Clutter.EVENT_STOP;
            }
            if (button !== 1)
                return Clutter.EVENT_PROPAGATE;
            if (pin.grab && isOutsidePin(pin, event)) {
                // 模态抓着键盘+鼠标：点到窗外要**主动交还**，不然下一个窗口像"点不动"
                releasePinFocus(pin);
                return Clutter.EVENT_PROPAGATE;
            }
            const now = GLib.get_monotonic_time() / 1000;
            if (now - pin.lastClickMs < PIN_DOUBLE_CLICK_MS) {
                destroyPin(pin);
                return Clutter.EVENT_STOP;
            }
            pin.lastClickMs = now;
            grabPinFocus(pin);                          // 点/拖 = 键盘归它
            const [px, py] = event.get_coords();
            pin.dragging = [px - a.x, py - a.y];
            return Clutter.EVENT_STOP;
        });
        actor.connect('motion-event', (a, event) => {
            if (!pin.dragging)
                return Clutter.EVENT_PROPAGATE;
            const [px, py] = event.get_coords();
            a.set_position(px - pin.dragging[0], py - pin.dragging[1]);
            pin.x = a.x;
            pin.y = a.y;
            return Clutter.EVENT_STOP;
        });
        actor.connect('button-release-event', () => {
            pin.dragging = null;
            return Clutter.EVENT_STOP;
        });
        actor.connect('scroll-event', (a, event) => {
            const state = event.get_state();
            const dir = event.get_scroll_direction();
            const up = dir === Clutter.ScrollDirection.UP;
            const down = dir === Clutter.ScrollDirection.DOWN;
            if (!up && !down)
                return Clutter.EVENT_PROPAGATE;    // 触控板的 SMOOTH 先不管，别乱缩/乱透
            if (state & Clutter.ModifierType.CONTROL_MASK) {
                setPinOpacity(pin, pin.opacity + (up ? PIN_OPACITY_STEP : -PIN_OPACITY_STEP));
                return Clutter.EVENT_STOP;
            }
            let next = up ? pin.scale * 1.1 : pin.scale / 1.1;
            next = Math.min(PIN_MAX_SCALE, Math.max(PIN_MIN_SCALE, next));
            pin.scale = next;
            a.set_scale(next, next);
            return Clutter.EVENT_STOP;
        });

        Main.layoutManager.addTopChrome(actor);
        if (ackPath)
            GLib.file_set_contents(ackPath, `count=${_pins.length}\nx=${Math.round(x)}\n` +
                `y=${Math.round(y)}\nw=${width}\nh=${height}\n`);
        pinLog('ok', `path=${rawPath} at=${Math.round(x)},${Math.round(y)} ` +
            `w=${width} h=${height} left=${_pins.length} ms=${ms()}`);
    } catch (e) {
        pinLog('fail', `path=${rawPath} why=${e.message} ms=${ms()}`);
    }
}

export default class RaiseBrowserExtension {
    enable() {
        // 决策分支两边都要留话：扩展在 shell 进程里加载，成败只在 journal 露一行。
        // 2026-10-16 因导出形态不对（缺 `export default class`）静默不加载，
        // 现场只能靠"现象对不对"反推，白让用户注销重登了一次。
        try {
            this._enableInternal();
            console.log(`[raise-browser] 已启用 iface=${IFACE} path=${OBJ_PATH}`);
        } catch (e) {
            console.error(`[raise-browser] 启用失败：${e.message}`);
            throw e;
        }
    }

    _enableInternal() {
        _ownId = Gio.DBus.session.own_name(BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
        // 7 个实参：sender, interface, member, object_path, arg0, user_data, callback（回调在最后）
        _subRaise = Gio.DBus.session.signal_subscribe(null, IFACE, M_RAISE, null, null, null,
            () => raiseBrowser());
        _subProbe = Gio.DBus.session.signal_subscribe(null, IFACE, M_PROBE, null, null, null,
            () => {
                // 只写 journal，不试图回 D-Bus 包：实测在 shell 进程里 `Gio.DBus.session.emit_signal`
                // 不会把包送上总线（外部能收到 Probe、监视器抓 0 条 ProbeReport，且没有任何 JS ERROR）
                // ⇒ 判活与取窗口数一律读 journal（2026-10-16）。
                console.log(`[raise-browser] probe ${report('')}`);
            });
        _subCapture = Gio.DBus.session.signal_subscribe(null, IFACE, M_CAPTURE, null, null, null,
            (_c, _sender, _path, _iface, _member, params) => {
                // 参数从 tuple variant 里取：本机 gjs 实测 `unpack()` 对元组返回 5 个空对象（没用），
                // `deepUnpack()` 才是 [x, y, w, h, outPath]；取不到就整条丢掉并留一行原因。
                let args;
                try {
                    args = params.deepUnpack();
                } catch (e) {
                    console.log(`[raise-browser] shot result=fail why=bad-params:${e.message}`);
                    return;
                }
                if (!args || args.length < 5) {
                    console.log(`[raise-browser] shot result=fail why=bad-params:n=${args ? args.length : 0}`);
                    return;
                }
                const [x, y, w, h, outPath] = args;
                if (!(w > 0 && h > 0) || !outPath) {
                    console.log(`[raise-browser] shot result=fail why=bad-rect w=${w} h=${h} path=${outPath}`);
                    return;
                }
                if (duplicateRequest('capture', String(outPath)))
                    return;
                captureArea(x, y, w, h, String(outPath));
            });
        // 这行是**增量标记**：改完扩展后 `gnome-extensions disable/enable` 到底有没有重新 import，
        // 就看 journal 里有没有它（旧代码只会打 `ready default=…`）——见 docs/SHOT-TOOL.md 的实测。
        _subPin = Gio.DBus.session.signal_subscribe(null, IFACE, M_PIN, null, null, null,
            (_c, _sender, _path, _iface, _member, params) => {
                let args;
                try {
                    args = params.deepUnpack();
                } catch (e) {
                    pinLog('fail', `why=bad-params:${e.message}`);
                    return;
                }
                if (!args || args.length < 7) {
                    pinLog('fail', `why=bad-params:n=${args ? args.length : 0}`);
                    return;
                }
                // (ssiiiis) = pngPath, rawPath, w, h, x, y, ackPath —— 与 capsplus 侧
                // `shot_frame.PIN_SIG` 逐字对应（判据 `test_shot_extension` 在钉这件事）。
                const [pngPath, rawPath, w, h, x, y, ackPath] = args;
                if (!(w > 0 && h > 0) || !rawPath) {
                    pinLog('fail', `why=bad-image w=${w} h=${h} path=${rawPath}`);
                    return;
                }
                if (duplicateRequest('pin', String(rawPath)))
                    return;
                pinImage(pngPath ? String(pngPath) : '', String(rawPath), w, h, x, y,
                    ackPath ? String(ackPath) : '');
            });
        // 全局兜底：`Caps+Esc` 清空所有贴图（keyd 侧的热键是主代理的活儿）。
        // **没有贴图时也安全**：只是清 0 张 + 写 ack 一行 cleared=0（判据会钉这条）。
        _subClear = Gio.DBus.session.signal_subscribe(null, IFACE, M_CLEAR, null, null, null,
            (_c, _sender, _path, _iface, _member, params) => {
                let args;
                try {
                    args = params.deepUnpack();
                } catch (e) {
                    pinLog('clear-fail', `why=bad-params:${e.message}`);
                    return;
                }
                const ackPath = (args && args.length >= 1 && args[0]) ? String(args[0]) : '';
                if (duplicateRequest('clear', ackPath || 'default'))
                    return;
                clearPins(ackPath);
            });
        console.log(`[raise-browser] ready capture=${_subCapture ? 'on' : 'off'} ` +
            `pin=${_subPin ? 'on' : 'off'} clear=${_subClear ? 'on' : 'off'} ` +
            `toolbar+menu+modal=on promisified=${_areaPromisified} ${report('')}`);
    }

    disable() {
        console.log('[raise-browser] 停用');
        restoreAllPins();       // 先把 hover 定时器收拾掉：不留任何等着"恢复贴图"的悬挂回调
        destroyAllPins();       // 自己关掉自己的东西：不许把 actor 留在屏幕上（会泄漏）
        // signal_remove 在本机是否存在要运行时看（这版 GJS 摘过好几个便捷函数）：
        // 摘不到就留着，shell 重启时自然会清；不能因为它不存在就把 disable 抛断。
        const conn = Gio.DBus.session;
        if (typeof conn.signal_remove === 'function') {
            if (_subRaise) {
                conn.signal_remove(_subRaise);
                _subRaise = 0;
            }
            if (_subProbe) {
                conn.signal_remove(_subProbe);
                _subProbe = 0;
            }
            if (_subCapture) {
                conn.signal_remove(_subCapture);
                _subCapture = 0;
            }
            if (_subPin) {
                conn.signal_remove(_subPin);
                _subPin = 0;
            }
            if (_subClear) {
                conn.signal_remove(_subClear);
                _subClear = 0;
            }
        } else {
            console.log('[raise-browser] 这版 GJS 没有 signal_remove，留到 shell 重启再清');
        }
        if (_ownId) {
            conn.unown_name(_ownId);
            _ownId = 0;
        }
    }
}
