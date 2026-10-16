// SPDX-License-Identifier: GPL-2.0-only
// Copyright (c) 2026 capsplus-linux contributors
// 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
//   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
/*
 * raise-browser：给 Qbar 一个 D-Bus 入口，先把默认浏览器的窗口激活，再让它去开网址。
 *
 * 为什么需要这么个东西（2026-10-16 实测定位）：
 *   GTK3 里根本没有 Wayland 激活令牌的通道（libgdk-3 的字符串里 `XDG_ACTIVATION_TOKEN` 出现 0 次），
 *   所以 Qbar 起浏览器时递不出令牌；mutter 因此不让浏览器把已有窗口拉到前台，浏览器为了让你看得见
 *   只能**新画一扇窗**。焦点本来就在浏览器时不需要令牌，于是行为"看起来正常"。
 *   扩展跑在 mutter 进程里，`Meta.Window.activate()` 是它自己的特权动作，不需要令牌 —— 我们借它一步。
 *
 * 只激活**当前工作区**的窗口：不把人从别的工作区硬拽过来；那种情况直接返回 false，
 * 由浏览器自己决定（维持现状，行为不变）。扩展没装/没应答时 Qbar 照样开网址，功能不退化。
 */
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

const BUS_NAME = 'io.github.capsplus.RaiseBrowser';
const OBJ_PATH = '/io/github/capsplus/RaiseBrowser';
const IFACE = 'io.github.capsplus.RaiseBrowser';
const INTROSPECTION = `
<node>
  <interface name="${IFACE}">
    <!-- 激活默认浏览器的窗口；返回 (是否成功, 说明) -->
    <method name="RaiseBrowser">
      <arg name="raised" type="b" direction="out"/>
      <arg name="detail" type="s" direction="out"/>
    </method>
    <!-- 只读探针：默认浏览器是谁、它现在有几扇窗、其中几扇在当前工作区 -->
    <method name="Probe">
      <arg name="report" type="s" direction="out"/>
    </method>
  </interface>
</node>`;

let _ownId = 0;
let _exportId = 0;

function defaultBrowserDesktopId() {
    const info = Gio.AppInfo.get_default_for_type('x-scheme-handler/https', false);
    return info ? (info.get_id() || null) : null;
}

/** 浏览器现有窗口 + 匹配来源（app 还是 wm_class 兜底），供 Raise 与 Probe 共用。 */
function browserWindows() {
    const id = defaultBrowserDesktopId();
    const out = [];
    if (id) {
        const app = Shell.AppSystem.get_default().lookup_app(id);
        if (app) {
            for (const w of app.get_windows(false)) {
                out.push({ w, why: `app=${id}` });
            }
        }
    }
    if (out.length === 0 && id) {
        // 兜底：.desktop 的 StartupWMClass 与 Wayland app_id 未必一致，按子串再找一遍
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

function raiseBrowser() {
    const all = browserWindows();
    if (!all.length) {
        return [false, 'no-browser-window'];
    }
    const ws = activeWorkspace();
    const here = ws ? all.filter(c => c.w.get_workspace() === ws) : all;
    if (!here.length) {
        return [false, `off-current-workspace(${all.length})`];   // 不硬拽工作区
    }
    const w = here[0].w;
    try {
        if (w.minimized) {
            w.unminimize();
        }
        w.activate(global.get_current_time());
    } catch (e) {
        return [false, `error:${e.message}`];
    }
    return [true, here[0].why];
}

function probe() {
    const id = defaultBrowserDesktopId();
    const all = browserWindows();
    const ws = activeWorkspace();
    const here = ws ? all.filter(c => c.w.get_workspace() === ws).length : all.length;
    const titles = all.slice(0, 5).map(c => String(c.w.get_title ? c.w.get_title() : '')).join(' | ');
    return `default=${id} windows=${all.length} on-current=${here} titles=[${titles}]`;
}

function _handleMethod(conn, _sender, _path, _iface, method, _params, invocation) {
    try {
        if (method === 'RaiseBrowser') {
            const [raised, detail] = raiseBrowser();
            conn.return_value(invocation, new GLib.Variant('(bs)', [raised, detail]), null);
        } else if (method === 'Probe') {
            conn.return_value(invocation, new GLib.Variant('(s)', [probe()]), null);
        } else {
            conn.return_error_literal(invocation, Gio.DBusError.UNKNOWN_METHOD, method);
        }
    } catch (e) {
        conn.return_error_literal(invocation, Gio.DBusError.FAILED, String(e.message));
    }
}

export function enable() {
    const info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION);
    // export_object_path 在 GJS 里标了 deprecated 但仍是扩展的主流写法（GNOME 45→50 实测可用）；
    // 哪天它真被拿掉，要换成 register_object 的 vtable 写法。
    _exportId = Gio.DBus.session.export_object_path(OBJ_PATH, info.interfaces[0], _handleMethod, null);
    _ownId = Gio.DBus.session.own_name(BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
}

export function disable() {
    if (_ownId) {
        Gio.DBus.session.unown_name(_ownId);
        _ownId = 0;
    }
    if (_exportId) {
        Gio.DBus.session.unexport_object_path(OBJ_PATH);
        _exportId = 0;
    }
}
