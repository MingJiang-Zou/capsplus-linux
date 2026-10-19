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

const BUS_NAME = 'io.github.capsplus.RaiseBrowser';
const OBJ_PATH = '/io/github/capsplus/RaiseBrowser';
const IFACE = 'io.github.capsplus.RaiseBrowser';
const M_RAISE = 'RaiseBrowser';
const M_PROBE = 'Probe';
const M_REPORT = 'ProbeReport';
const M_CAPTURE = 'CaptureArea';
const M_PIN = 'PinImage';

let _ownId = 0;
let _subRaise = 0;
let _subProbe = 0;
let _subCapture = 0;
let _subPin = 0;

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
function captureArea(x, y, width, height, outPath) {
    const t0 = GLib.get_monotonic_time();
    const ms = () => Math.round((GLib.get_monotonic_time() - t0) / 1000);
    const scale = stageScale();
    const sx = Math.round(x * scale), sy = Math.round(y * scale);
    const sw = Math.round(width * scale), sh = Math.round(height * scale);
    const rect = `rect=${sx},${sy},${sw},${sh} scale=${scale}`;
    const part = Gio.File.new_for_path(`${outPath}.part`);
    const out = Gio.File.new_for_path(outPath);
    console.log(`[raise-browser] shot result=started path=${outPath} ${rect}`);

    let stream = null;
    try {
        stream = part.replace(null, false, Gio.FileCreateFlags.NONE, null);
    } catch (e) {
        console.log(`[raise-browser] shot result=fail path=${outPath} why=open:${e.message} ms=${ms()}`);
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
            return;
        }
        try {
            stream.close(null);
        } catch (e) {
            cleanup();
            console.log(`[raise-browser] shot result=fail path=${outPath} why=close:${e.message} ms=${ms()}`);
            return;
        }
        try {
            if (out.query_exists(null))
                out.delete(null);
            part.move(out, Gio.FileCopyFlags.OVERWRITE, null, null);
        } catch (e) {
            cleanup();
            console.log(`[raise-browser] shot result=fail path=${outPath} why=rename:${e.message} ms=${ms()}`);
            return;
        }
        console.log(`[raise-browser] shot result=ok path=${outPath} ${rect} ms=${ms()}`);
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

let _pins = [];                        // 多张并存：每项 = {actor, scale, dragging, lastClickMs}

function pinLog(result, extra) {
    console.log(`[raise-browser] pin result=${result}${extra ? ' ' + extra : ''}`);
}

function destroyPin(pin) {
    const idx = _pins.indexOf(pin);
    if (idx !== -1)
        _pins.splice(idx, 1);
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

/**
 * 把一张**已经是 RGBA 原图**的文件钉成置顶图层 actor。
 * 为什么传原图而不是 PNG：shell 这边不做解码（GJS 里没有现成的 PNG→像素），解码由 capsplus
 * 侧用 PIL 做完（`capsplus/shot_pin.py`），少一整条会出错的路。
 *
 * 交互：左键拖动、滚轮缩放（0.2×–4×）、双击或点右上角 × 关闭、**不抢键盘焦点**
 * （× 按钮显式 `can_focus: false`，全文件不调 `grab_key_focus`）、可多张并存。
 * 成功写 ack 文件（内容含 left= 张数）—— 与抓帧"等产物"是同一条口径：没有回复通道，
 * 所以"成没成"只认文件。
 */
function pinImage(rawPath, width, height, x, y, ackPath) {
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
        const close = new St.Button({label: '×', style_class: 'capsplus-pin-close',
                                     can_focus: false});   // ← 不抢键盘焦点的关键
        close.set_position(Math.max(width - 24, 0), 0);

        const actor = new St.Widget({style_class: 'capsplus-pin-box', reactive: true});
        actor.add_child(holder);
        actor.add_child(close);
        actor.set_size(width, height);
        actor.set_position(x, y);
        actor.set_pivot_point(0.5, 0.5);      // 缩放绕中心，不然会"跑"

        const pin = {actor, scale: 1.0, dragging: null, lastClickMs: 0};
        _pins.push(pin);

        close.connect('clicked', () => destroyPin(pin));
        actor.connect('button-press-event', (a, event) => {
            if (event.get_button() !== 1)
                return Clutter.EVENT_PROPAGATE;
            const now = GLib.get_monotonic_time() / 1000;
            if (now - pin.lastClickMs < PIN_DOUBLE_CLICK_MS) {
                destroyPin(pin);
                return Clutter.EVENT_STOP;
            }
            pin.lastClickMs = now;
            const [px, py] = event.get_coords();
            pin.dragging = [px - a.x, py - a.y];
            return Clutter.EVENT_STOP;
        });
        actor.connect('motion-event', (a, event) => {
            if (!pin.dragging)
                return Clutter.EVENT_PROPAGATE;
            const [px, py] = event.get_coords();
            a.set_position(px - pin.dragging[0], py - pin.dragging[1]);
            return Clutter.EVENT_STOP;
        });
        actor.connect('button-release-event', () => {
            pin.dragging = null;
            return Clutter.EVENT_STOP;
        });
        actor.connect('scroll-event', (a, event) => {
            const dir = event.get_scroll_direction();
            let next = pin.scale;
            if (dir === Clutter.ScrollDirection.UP)
                next = pin.scale * 1.1;
            else if (dir === Clutter.ScrollDirection.DOWN)
                next = pin.scale / 1.1;
            else
                return Clutter.EVENT_PROPAGATE;    // 触控板的 SMOOTH 事件先不管，别乱缩
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
                if (!args || args.length < 6) {
                    pinLog('fail', `why=bad-params:n=${args ? args.length : 0}`);
                    return;
                }
                const [rawPath, w, h, x, y, ackPath] = args;
                if (!(w > 0 && h > 0) || !rawPath) {
                    pinLog('fail', `why=bad-image w=${w} h=${h} path=${rawPath}`);
                    return;
                }
                pinImage(String(rawPath), w, h, x, y, ackPath ? String(ackPath) : '');
            });
        console.log(`[raise-browser] ready capture=${_subCapture ? 'on' : 'off'} ` +
            `pin=${_subPin ? 'on' : 'off'} promisified=${_areaPromisified} ${report('')}`);
    }

    disable() {
        console.log('[raise-browser] 停用');
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
        } else {
            console.log('[raise-browser] 这版 GJS 没有 signal_remove，留到 shell 重启再清');
        }
        if (_ownId) {
            conn.unown_name(_ownId);
            _ownId = 0;
        }
    }
}
