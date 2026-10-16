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
 */
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

const BUS_NAME = 'io.github.capsplus.RaiseBrowser';
const OBJ_PATH = '/io/github/capsplus/RaiseBrowser';
const IFACE = 'io.github.capsplus.RaiseBrowser';
const M_RAISE = 'RaiseBrowser';
const M_PROBE = 'Probe';
const M_REPORT = 'ProbeReport';

let _ownId = 0;
let _subRaise = 0;
let _subProbe = 0;

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
        console.log(`[raise-browser] ready ${report('')}`);
    }

    disable() {
        console.log('[raise-browser] 停用');
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
        } else {
            console.log('[raise-browser] 这版 GJS 没有 signal_remove，留到 shell 重启再清');
        }
        if (_ownId) {
            conn.unown_name(_ownId);
            _ownId = 0;
        }
    }
}
